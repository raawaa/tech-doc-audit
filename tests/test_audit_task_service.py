"""audit_task_service 测试 — run_audit agentic 编排。

mock 掉 agentic_audit.run_agentic_audit、audit_doc_svc.parse_document、
structure_svc.analyze_document_structure、doc_repo.get_doc，
避免触发 LLM / GPU / 真实文档解析。
repo.get_task / repo.save_task 走真实文件系统（AUDIT_DATA_DIR 隔离）。
"""

import os
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import services.audit_task_service as ats
import storage.audit_task_repo as repo
from models.audit_task import AuditTask, AuditIssue, IssueLocation, AuditResult, ResultSummary
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


def _patch_dependencies(monkeypatch, doc, issues=None):
    """统一 mock 掉 run_audit 的外部依赖。"""
    monkeypatch.setattr(ats.doc_repo, "get_doc", lambda doc_id: doc)
    monkeypatch.setattr(ats.audit_doc_svc, "parse_document", lambda doc_id: doc)
    monkeypatch.setattr(ats.structure_svc, "analyze_document_structure", lambda doc_id: doc)

    if issues is None:
        issues = [
            AuditIssue(
                id=1,
                type="compliance",
                location=IssueLocation(original_text="原文"),
                description="测试问题",
                severity="medium",
            )
        ]

    mock_result = AuditResult(
        task_id="mock",
        document_id=doc.id,
        document_name=doc.name,
        summary=ResultSummary(),
        issues=issues,
        raw_analysis="Agentic 审核完成",
    )
    monkeypatch.setattr("services.agentic_audit.run_agentic_audit", lambda **kw: mock_result)


# ── 编排 ───────────────────────────────────────────────────────────────────────


def test_run_audit_agentic_success(monkeypatch):
    """Agentic 审核成功 → status=completed，progress=1.0，issues 齐全。"""
    doc = _make_doc()
    _patch_dependencies(monkeypatch, doc)
    task = _seed_task()

    result = ats.run_audit(task.id)

    assert result.status == "completed"
    assert result.progress == 1.0
    assert result.result is not None
    assert len(result.result.issues) == 1
    assert result.result.summary.issues_count == 1


def test_run_audit_doc_not_found(monkeypatch):
    """文档不存在 → status=failed。"""
    monkeypatch.setattr(ats.doc_repo, "get_doc", lambda doc_id: None)
    task = _seed_task()

    result = ats.run_audit(task.id)

    assert result.status == "failed"
    assert result.error_message is not None


def test_run_audit_agentic_failure(monkeypatch):
    """Agentic 审核异常 → status=failed，error_message 有值。"""
    doc = _make_doc()
    monkeypatch.setattr(ats.doc_repo, "get_doc", lambda doc_id: doc)
    monkeypatch.setattr(ats.audit_doc_svc, "parse_document", lambda doc_id: doc)
    monkeypatch.setattr(ats.structure_svc, "analyze_document_structure", lambda doc_id: doc)
    monkeypatch.setattr(
        "services.agentic_audit.run_agentic_audit",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("LLM unavailable")),
    )
    task = _seed_task()

    result = ats.run_audit(task.id)

    assert result.status == "failed"
    assert "LLM unavailable" in result.error_message


def test_run_audit_async_starts_thread(monkeypatch):
    """run_audit_async 立即返回 task_id，daemon 线程后台执行。"""
    from unittest.mock import patch

    doc = _make_doc()
    _patch_dependencies(monkeypatch, doc)
    task = _seed_task()

    with patch("services.agentic_audit.run_agentic_audit") as mock_agentic:
        from models.audit_task import AuditResult, ResultSummary
        mock_result = AuditResult(
            task_id=task.id,
            document_id=doc.id,
            document_name=doc.name,
            summary=ResultSummary(),
            issues=[],
            raw_analysis="done",
        )
        mock_agentic.return_value = mock_result

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
