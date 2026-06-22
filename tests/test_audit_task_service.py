"""audit_task_service 测试 — run_audit 并行编排、失败隔离、主题降级。

mock 掉 topic_audit.audit_topic、agent_audit.determine_audit_topics、
audit_doc_svc.parse_document、structure_svc.analyze_document_structure、
doc_repo.get_doc，避免触发 LLM / GPU / 真实文档解析。
repo.get_task / repo.save_task 走真实文件系统（AUDIT_DATA_DIR 隔离）。
"""

import os
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import services.audit_task_service as ats
import storage.audit_task_repo as repo
from models.audit_task import AuditTask, AuditIssue, IssueLocation
from models.audit_document import AuditDocument


@pytest.fixture(autouse=True)
def cleanup():
    """每个测试后清理 audits 目录。"""
    yield
    audits = Path(os.environ["AUDIT_DATA_DIR"]) / "audits"
    if audits.exists():
        shutil.rmtree(audits)


def _make_doc(doc_id="doc1", parsed="招标文件内容，含增值税与保证金条款。"):
    doc = AuditDocument(
        id=doc_id,
        name="test.pdf",
        original_name="test.pdf",
        file_type="pdf",
        file_path="/tmp/test.pdf",
        status="parsed",
    )
    doc.parsed_content = parsed
    return doc


def _seed_task(doc_id="doc1", kb_ids=None):
    task = AuditTask(document_id=doc_id, document_name="test.pdf", kb_ids=kb_ids or ["kb1"])
    return repo.save_task(task)


def _patch_dependencies(monkeypatch, doc, topics, issues_per_topic=1):
    """统一 mock 掉 run_audit 的外部依赖。"""
    monkeypatch.setattr(ats.doc_repo, "get_doc", lambda doc_id: doc)
    monkeypatch.setattr(ats.audit_doc_svc, "parse_document", lambda doc_id: doc)
    monkeypatch.setattr(ats.structure_svc, "analyze_document_structure", lambda doc_id: doc)
    monkeypatch.setattr(ats.agent_audit, "determine_audit_topics", lambda content, kb_ids: topics)

    def fake_audit_topic(topic, doc_nav, kb_ids, topic_index, parsed_content):
        return [
            AuditIssue(
                id=topic_index * 1000 + i + 1,
                type="compliance",
                location=IssueLocation(original_text="原文"),
                description=f"主题{topic_index}问题{i}",
                severity="medium",
            )
            for i in range(issues_per_topic)
        ]

    monkeypatch.setattr(ats.topic_audit, "audit_topic", fake_audit_topic)


# ── 编排 ───────────────────────────────────────────────────────────────────────


def test_run_audit_parallel_orchestration(monkeypatch):
    """3 个主题 → ThreadPoolExecutor 并行 → status=completed，progress=1.0，issues 齐全。"""
    doc = _make_doc()
    topics = [
        {"id": f"t{i}", "name": f"主题{i}", "prompt": "...", "keywords": ["增值税"]}
        for i in range(3)
    ]
    _patch_dependencies(monkeypatch, doc, topics, issues_per_topic=1)
    task = _seed_task()

    result = ats.run_audit(task.id)

    assert result.status == "completed"
    assert result.progress == 1.0
    assert result.result is not None
    assert len(result.result.issues) == 3
    assert result.result.summary.issues_count == 3


def test_run_audit_topic_failure_isolated(monkeypatch):
    """单个主题抛错 → 该主题失败但不影响其他，task 仍 completed，部分 issues 保留。"""
    doc = _make_doc()
    topics = [
        {"id": "t0", "name": "主题0", "keywords": ["增值税"]},
        {"id": "t1", "name": "主题1", "keywords": ["保证金"]},
        {"id": "t2", "name": "主题2", "keywords": ["品牌"]},
    ]
    _patch_dependencies(monkeypatch, doc, topics)

    # 覆盖：让主题 1 抛错
    real_fake = ats.topic_audit.audit_topic

    def flaky_audit(topic, doc_nav, kb_ids, topic_index, parsed_content):
        if topic_index == 1:
            raise RuntimeError("topic 1 boom")
        return real_fake(topic, doc_nav, kb_ids, topic_index, parsed_content)

    monkeypatch.setattr(ats.topic_audit, "audit_topic", flaky_audit)
    task = _seed_task()

    result = ats.run_audit(task.id)

    assert result.status == "completed"  # 部分失败不致整体失败
    # 主题 0、2 各 1 个 issue；主题 1 失败
    assert len(result.result.issues) == 2


def test_run_audit_doc_not_found(monkeypatch):
    """文档不存在 → status=failed。"""
    monkeypatch.setattr(ats.doc_repo, "get_doc", lambda doc_id: None)
    task = _seed_task()

    result = ats.run_audit(task.id)

    assert result.status == "failed"
    assert result.error_message is not None


def test_run_audit_falls_back_to_fixed_topics(monkeypatch):
    """agent 返回空主题 → 回退到 topic_audit.AUDIT_TOPICS（8 个固定主题）。"""
    doc = _make_doc()
    _patch_dependencies(monkeypatch, doc, topics=[])  # agent 返回空
    task = _seed_task()

    result = ats.run_audit(task.id)

    assert result.status == "completed"
    # 回退到 AUDIT_TOPICS（8 个），每个 1 issue
    assert len(result.result.issues) == len(ats.topic_audit.AUDIT_TOPICS)


def test_run_audit_async_starts_thread(monkeypatch):
    """run_audit_async 立即返回 task_id，daemon 线程后台执行。"""
    doc = _make_doc()
    topics = [{"id": "t0", "name": "主题0", "keywords": ["增值税"]}]
    _patch_dependencies(monkeypatch, doc, topics)
    task = _seed_task()

    returned_id = ats.run_audit_async(task.id)
    assert returned_id == task.id

    # 等待 daemon 线程完成（轮询 task 状态）
    import time
    for _ in range(100):
        t = repo.get_task(task.id)
        if t and t.status in ("completed", "failed"):
            break
        time.sleep(0.1)

    final = repo.get_task(task.id)
    assert final.status == "completed"
