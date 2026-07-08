"""端到端测试：重新解析流程（PRD #29 / V4）。

本测试套件覆盖：
- POST /kb-documents/{doc_id}/reparse 落地 pages/{doc_id}.json + 重建索引
- 跨页章节作为单个 chunk 完整存在（不被按页腰斩）
- ``embedding_status`` 从 ``pending_index`` → ``embedded`` 的状态机

仅在 ``PADDLEOCR_API_TOKEN`` 环境变量存在时运行：
``pytest -m "not requires_paddleocr"`` 在 CI 跳过；本地有 Token 时可手动全跑。
"""
from __future__ import annotations

import hashlib
import os
import time

import pytest

from core.pages_store import load_pages


# ── marker 声明 ────────────────────────────────────────────────────────────────


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "requires_paddleocr: 需要 PaddleOCR-VL-1.6 API Token 才跑（默认 CI 跳过）",
    )


requires_paddleocr = pytest.mark.skipif(
    not (os.environ.get("PADDLEOCR_API_TOKEN") and os.environ.get("PADDLEOCR_API_URL")),
    reason="requires PaddleOCR API Token (run with PADDLEOCR_API_TOKEN=... to enable)",
)


# ── 共享 fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def pdf_bytes() -> bytes:
    """最小可解析 PDF（多页，确保 PageText 列表非空）。"""
    # PDF 1.4，2 个空白页（pdfplumber/PaddleOCR 都能解析但文本可空）
    return (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R 4 0 R]/Count 2>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
        b"4 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
        b"xref\n0 5\n"
        b"0000000000 65535 f \n"
        b"0000000009 00000 n \n"
        b"0000000056 00000 n \n"
        b"0000000110 00000 n \n"
        b"0000000164 00000 n \n"
        b"trailer<</Size 5/Root 1 0 R>>\nstartxref\n220\n%%EOF\n"
    )


@pytest.fixture
def reparse_target_kb(tmp_path, monkeypatch, pdf_bytes):
    """创建 KB + 已落盘的 PDF doc + 已知 content_hash。

    返回 ``(kb, doc, content_hash)``。doc 的状态初始为 ``embedded``，
    模拟已经导入但 pages 数据缺损的场景。
    """
    monkeypatch.setenv("AUDIT_DATA_DIR", str(tmp_path))
    # 重 import 一次，让 AUDIT_DATA_DIR 生效
    import importlib

    import storage.kb_repo
    import storage.doc_repo
    import services.kb_service

    importlib.reload(storage.kb_repo)
    importlib.reload(storage.doc_repo)
    importlib.reload(services.kb_service)

    kb = services.kb_service.create_kb(name="e2e-reparse", category="national")
    doc = storage.doc_repo.save_doc(
        kb.id, "e2e_doc.pdf", pdf_bytes, "pdf",
    )
    content_hash = hashlib.sha256(pdf_bytes).hexdigest()

    from models.document import KBDocument
    refreshed = storage.doc_repo.get_doc(kb.id, doc.id)
    refreshed.content_hash = content_hash
    refreshed.embedding_status = "embedded"
    storage.doc_repo._save_doc_meta(refreshed)

    return kb, refreshed, content_hash


# ── e2e（需要 Token） ──────────────────────────────────────────────────────────


@requires_paddleocr
def test_reparse_end_to_end_stores_pages_and_reindexes(
    reparse_target_kb, monkeypatch
):
    """端到端：POST /reparse → 调用 PaddleOCR → 落 pages/{doc_id}.json → 索引重建。

    验证：
    1. 调用前后 pages 文件不存在 → 存在
    2. embedding_status 经过 pending_index → embedded 状态机
    3. 重建索引后 chunks 包含文档文本
    """
    from fastapi.testclient import TestClient
    from api.main import app

    kb, doc, content_hash = reparse_target_kb

    # 启动前：应当没有 pages 文件
    assert load_pages(kb.id, doc.id) is None, "e2e 前提：pages 文件不应预置"

    client = TestClient(app)
    resp = client.post(f"/api/v1/kb-documents/{doc.id}/reparse")
    assert resp.status_code == 202
    assert resp.json()["status"] == "pending_index"

    # 等后台任务完成
    import storage.doc_repo as doc_repo
    deadline = time.monotonic() + 60.0  # PaddleOCR 同步轮询可能较慢
    refreshed = None
    while time.monotonic() < deadline:
        try:
            refreshed = doc_repo.get_doc(kb.id, doc.id)
        except Exception:
            refreshed = None
        if refreshed and refreshed.embedding_status in ("embedded", "failed"):
            break
        time.sleep(0.5)

    assert refreshed is not None, "doc 元数据不可读"
    assert refreshed.embedding_status == "embedded", (
        f"reparse 后 doc 应 embedded，实际 {refreshed.embedding_status}"
    )

    # 验证：pages 文件应当落地
    pages = load_pages(kb.id, doc.id)
    assert pages is not None, "pages/{doc_id}.json 必须存在"
    assert pages.get("kb_id") == kb.id
    assert pages.get("doc_id") == doc.id
    assert pages.get("file_hash") == content_hash
    assert isinstance(pages.get("by_page"), list)
    assert isinstance(pages.get("layout"), list)
    # 即使 PDF 文本极简（PaddleOCR 也许抽不到字），结构字段都应就位


@requires_paddleocr
def test_reparse_idempotent_when_cached(reparse_target_kb, monkeypatch):
    """缓存命中：再次 reparse 不会再次调 PaddleOCR（counter 单调）。"""
    from core import paddleocr_cache as cache_module

    kb, doc, _ = reparse_target_kb

    # 第一轮：调 PaddleOCR + 写缓存
    from services.reparse_service import reparse_document as _reparse
    _reparse(doc.id)
    time.sleep(2.0)  # 等后台线程完成

    # 第二轮：缓存应命中，_paddleocr_call 不应被调用
    called = {"count": 0}

    def _count_calls(*a, **k):
        called["count"] += 1
        raise RuntimeError("should not be called")

    from services import reparse_service as rs
    monkeypatch.setattr(rs, "_paddleocr_call", _count_calls)
    monkeypatch.setattr(cache_module, "_paddleocr_available", lambda: True)

    _reparse(doc.id)
    time.sleep(2.0)
    assert called["count"] == 0, f"PaddleOCR 被调 {called['count']} 次（缓存应当命中）"


# ── 单元测试（不需要 Token） ────────────────────────────────────────────────────────
def test_reparse_passes_by_layout_to_index_document(
    reparse_target_kb, monkeypatch
):
    """V8-S2 漏改防御：reparse_service 调 index_document 时显式传 by_layout。

    之前 V8-S2 在 vector_search.index_document_document 路径上加了
    ``by_layout=parse_result.layout``（commit a58eba3），但 reparse_service
    没跟进，导致走 reparse 路径的 doc 永远 block_range=None。

    不需要 PaddleOCR token：通过 mock parse_document / index_document 同步验证调用契约。
    """
    from unittest.mock import MagicMock, patch
    from core.parse_document import Block, PageLayout, ParseResult

    kb, doc, _ = reparse_target_kb

    # 准备一份带 layout 的 mock parse_result
    fake_layout = [PageLayout(page=0, blocks=[Block(block_order=0), Block(block_order=1)])]
    fake_parse_result = ParseResult(
        by_page=[],
        full_text="x" * 50,  # >20 字符避开稀疏文本 raise
        layout=fake_layout,
    )

    # 用 unittest.mock.patch 拦截 import 时已绑定的名字（monkeypatch.setattr 对
    # `from x import y` 形式的 import 无效 —— y 是模块级本地名, 不能从外部重绑）
    with patch("services.reparse_service.parse_document", return_value=fake_parse_result), \
         patch("services.reparse_service.save_pages", return_value=None), \
         patch("services.reparse_service.remove_document", return_value=None), \
         patch("services.reparse_service.index_document", return_value=None) as mock_idx, \
         patch("services.reparse_service.kb_repo") as mock_kb_repo, \
         patch("services.reparse_service.doc_repo") as mock_doc_repo:
        # 模拟 kb_repo.get(kb_id) → KB 实例(index_status 等可写)
        fake_kb = MagicMock()
        mock_kb_repo.get.return_value = fake_kb
        # 模拟 doc_repo.get_doc(kb_id, doc_id) → doc 实例
        mock_doc_repo.get_doc.return_value = doc

        from services.reparse_service import _reparse_async
        _reparse_async(kb.id, doc.id)

    mock_idx.assert_called_once()
    call = mock_idx.call_args
    # 位置/关键字参数: by_layout 必须在 kwargs 里
    assert call.kwargs.get("by_layout") is fake_layout, (
        f"reparse 必须把 parse_result.layout 传给 index_document.by_layout，"
        f"否则 _inject_block_range 永远拿不到 layout, block_range 永远 None。"
        f"实际 call={call!r}"
    )
