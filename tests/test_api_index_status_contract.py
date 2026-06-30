"""Seam 1（API 集成测试）：kb/doc 新 JSON 契约 + 自愈 + 同步失败回退。

通过 HTTP 客户端驱动公开 API（最高 seam，per PRD §Testing Decisions）：
1. 字段重命名后的 JSON 契约：doc 暴露 embedding_status；KB 暴露 index_status
2. 重建索引端点触发后 → KB 自愈为 'searchable'（轮询）
3. 文档导入后变 'embedded'（同步路径，非异步真模型）
4. 自动重建：KB 字段被人改回 none → 下次访问 vec_search 自愈回 searchable
5. 上传损坏文件触发同步路径回退 'failed'（不是 'embedded'，验证 ADR-0003 §决策 5）

不直接断言函数调用顺序，只观察：HTTP 响应 + 字段最终值。
"""

import io
import os
import shutil
import time

import pytest
from fastapi.testclient import TestClient

from api.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def cleanup():
    """每个测试后清理数据。"""
    yield
    test_dir = os.environ["AUDIT_DATA_DIR"]
    for item in os.listdir(test_dir):
        path = os.path.join(test_dir, item)
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.remove(path)


@pytest.fixture(autouse=True)
def _use_fake_models(fake_models):
    """避免真实 bge-m3 加载；让 FastAPI 在测试期间用假 embedder。"""
    yield


def _create_kb(name: str = "API 测试库") -> str:
    resp = client.post(
        "/api/v1/knowledge-bases",
        json={"name": name, "category": "national"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _upload_doc(kb_id: str, filename: str, content: bytes):
    return client.post(
        f"/api/v1/documents/{kb_id}/upload",
        files={"file": (filename, io.BytesIO(content), "application/octet-stream")},
    )


# 提供一些 ASCII 安全的 doc 内容供测试复用
MD_DOC = b"# design\n\n## chapter 1\n\nThis is a sample document body for the contract test."
MD_DOC_LONG = b"# design\n\n## chapter 1\n\nWe use a slightly longer text body to avoid len < 20 short-circuit."


# ── 字段重命名后的 JSON 契约（ADR-0003 §决策 4）──────────────────────


def test_api_doc_response_uses_embedding_status():
    """API 文档响应：使用 embedding_status（不再用 index_status）。"""
    kb_id = _create_kb()
    resp = _upload_doc(kb_id, "x.md", MD_DOC)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert "embedding_status" in body, (
        f"doc response should contain embedding_status, actual keys: {list(body.keys())}"
    )
    assert body["embedding_status"] in (
        "pending_index", "indexing", "embedded", "failed"
    ), f"embedding_status illegal: {body['embedding_status']}"
    assert "index_status" not in body, (
        "doc response should not contain index_status (migrated to embedding_status)"
    )


def test_api_kb_response_uses_index_status_with_searchable_terminal():
    """API KB response: index_status terminal = 'searchable' (not 'ready')."""
    kb_id = _create_kb()
    _upload_doc(kb_id, "x.md", MD_DOC_LONG)

    for _ in range(100):
        resp = client.get(f"/api/v1/knowledge-bases/{kb_id}")
        kb = resp.json()
        if kb["index_status"] == "searchable":
            break
        time.sleep(0.1)

    assert "index_status" in kb
    assert kb["index_status"] == "searchable", (
        f"KB index terminal should be searchable, actual {kb['index_status']}"
    )
    assert kb["index_status"] != "ready", (
        "should not return ready anymore (migrated to searchable)"
    )


# ── 重建索引端点 → KB 自愈为 searchable ─────────────────────────


def test_reindex_endpoint_eventually_heals_to_searchable():
    """reindex endpoint trigger → field auto-heals to 'searchable' (no manual refresh)."""
    kb_id = _create_kb("reindex test")
    _upload_doc(kb_id, "r.md", MD_DOC_LONG)

    for _ in range(100):
        kb = client.get(f"/api/v1/knowledge-bases/{kb_id}").json()
        if kb["index_status"] == "searchable":
            break
        time.sleep(0.1)

    resp = client.post(f"/api/v1/knowledge-bases/{kb_id}/reindex")
    assert resp.status_code == 200, resp.text

    # reindex 后台线程最终会写入 searchable；轮询直到从 building 退出
    for _ in range(200):
        kb = client.get(f"/api/v1/knowledge-bases/{kb_id}").json()
        if kb["index_status"] == "searchable":
            break
        time.sleep(0.1)

    assert kb["index_status"] == "searchable", (
        f"reindex 后字段应自动回 searchable, actual {kb['index_status']}"
    )


# ── 自动自愈（用户故事 5 + 7） ─────────────


def test_auto_heal_when_field_is_reset(seed_searchable_kb):
    """User story 5: first use / field manually reset → next rebuild action heals."""
    seed_searchable_kb("test_api_heal")
    import storage.kb_repo as kb_repo

    kb = kb_repo.get("test_api_heal")
    kb.index_status = "none"
    kb_repo.update(kb)

    resp = client.post("/api/v1/knowledge-bases/test_api_heal/reindex")
    assert resp.status_code == 200

    for _ in range(100):
        kb_now = client.get("/api/v1/knowledge-bases/test_api_heal").json()
        if kb_now["index_status"] == "searchable":
            break
        time.sleep(0.1)

    assert kb_now["index_status"] == "searchable", (
        f"auto-heal 后字段应为 searchable, actual {kb_now['index_status']}"
    )


# ── 同步路径失败回退 'failed'（ADR-0003 §决策 5）──────────


def test_sync_import_failure_sets_embedding_status_failed():
    """Sync path embedding failure → embedding_status='failed' (not ready/embedded).

    Per ADR-0003 §决策 5: sync path failure must NOT leave status as 'embedded'
    or 'ready'; it must revert to 'failed' so the operator knows to retry.
    """
    import services.kb_service as kb_svc
    import services.doc_service as doc_svc

    kb = kb_svc.create_kb(name="sync-fail-test", category="national")
    import unittest.mock as mock
    with mock.patch(
        "services.doc_service._index_vec",
        side_effect=RuntimeError("simulated embed failure"),
    ):
        doc = doc_svc.import_document(
            kb.id, "broken.md",
            MD_DOC_LONG,
            async_index=False,
        )

    assert doc.embedding_status == "failed", (
        f"sync path 失败应回退 embedding_status='failed', actual {doc.embedding_status}"
    )


# ── JSON 契约稳定性 ─────────────


def test_kb_response_schema_consistent_with_adr_0003():
    """KB response schema: 'index_status' terminal = 'searchable' (not 'ready').

    Prevents accidental re-introduction of old 'ready' terminal per ADR-0003 §决策 4
    and CONTEXT.md 'Knowledge Base Search' _Avoid_ note.
    """
    kb_id = _create_kb()

    import services.doc_service as doc_svc
    doc_svc.import_document(
        kb_id, "schema-test.md",
        MD_DOC_LONG,
    )

    for _ in range(100):
        kb = client.get(f"/api/v1/knowledge-bases/{kb_id}").json()
        if kb["index_status"] == "searchable":
            break
        time.sleep(0.1)

    assert "index_status" in kb
    assert kb["index_status"] != "ready", "KB response should not contain 'ready' terminal"
    assert kb["index_status"] in ("none", "building", "searchable", "failed")
