"""端到端测试：重新解析流程（PRD #29 / V4）。

本测试套件覆盖：
- POST /kb-documents/{doc_id}/reparse 落地 pages/{doc_id}.json + 重建索引
- 跨页章节作为单个 chunk 完整存在（不被按页腰斩）
- ``embedding_status`` 从 ``pending_index`` → ``embedded`` 的状态机
- 空文本守卫（#100/#135）：空白 PDF → ``failed`` + KB 级失败标记

仅在 ``PADDLEOCR_API_TOKEN`` 环境变量存在时运行：
``pytest -m "not requires_paddleocr"`` 在 CI 跳过；本地有 Token 时可手动全跑。
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

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
def blank_pdf_bytes() -> bytes:
    """最小可解析 PDF（2 个空白页，pdfplumber/PaddleOCR 都能解析但文本可空）。

    #135：空白 PDF 专供空文本守卫用例（#100）——``full_text`` 必为空 →
    guard 抛错 → ``embedding_status=failed``。正路（``pending_index`` →
    ``embedded``）走 ``text_pdf_bytes``。
    """
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
def text_pdf_bytes() -> bytes:
    """真实文字层 PDF（``tests/fixtures/text_layer_pdfs/s1_p1.pdf``）——正路 e2e。

    #135：``_is_text_layer_pdf`` 判定通过 → PyMuPDF 解析（零 OCR 配额），
    ``full_text`` ≈600 字符 ≥ ``MIN_FULL_TEXT_CHARS``(20)，``layout`` /
    ``by_page`` 均非空 —— 过得了 #100 空文本守卫，走完整
    ``pending_index`` → ``embedded`` 路径。
    """
    path = Path(__file__).parent / "fixtures" / "text_layer_pdfs" / "s1_p1.pdf"
    return path.read_bytes()


def _make_reparse_target(pdf_bytes: bytes):
    """创建 KB + 已落盘的 PDF doc + 已知 content_hash。

    返回 ``(kb, doc, content_hash)``。doc 的状态初始为 ``embedded``，
    模拟已经导入但 pages 数据缺损的场景。

    ``AUDIT_DATA_DIR`` 已由 conftest per-test fixture 指到本用例的 tmp_path
    （issue #137），存储层 ``get_data_dir()`` 每次调用解析 env，无需再
    setenv / reload 模块。
    """
    import storage.kb_repo
    import storage.doc_repo
    import services.kb_service

    kb = services.kb_service.create_kb(name="e2e-reparse", category="national")
    doc = storage.doc_repo.save_doc(
        kb.id, "e2e_doc.pdf", pdf_bytes, "pdf",
    )
    content_hash = hashlib.sha256(pdf_bytes).hexdigest()

    refreshed = storage.doc_repo.get_doc(kb.id, doc.id)
    refreshed.content_hash = content_hash
    refreshed.embedding_status = "embedded"
    storage.doc_repo._save_doc_meta(refreshed)

    return kb, refreshed, content_hash


@pytest.fixture
def reparse_target_kb(blank_pdf_bytes):
    """空白 PDF 目标（空文本守卫用例，#135）。"""
    return _make_reparse_target(blank_pdf_bytes)


@pytest.fixture
def reparse_target_kb_text(text_pdf_bytes):
    """文字层 PDF 目标（正路 pending_index → embedded 用例，#135）。"""
    return _make_reparse_target(text_pdf_bytes)


# doc 状态机的终态（与 services/reparse_service 的写入一致）
TERMINAL_STATES = ("embedded", "failed")


def _wait_reparse_terminal(kb_id: str, doc_id: str, timeout_s: float = 180.0):
    """轮询直到 doc 到终态（embedded/failed）且 KB 级状态落位。

    上限给 180s：正路在解析成功后会冷启动 bge-m3（~2GB，实测可达
    120s+）+ PaddleOCR 同步轮询 + 建索引，60s（旧守卫用例预算）不够；
    terminal 一旦出现立即返回，快路径不受拖累。

    doc 进入终态后**再等 KB 落位**（reparse_service 先写 doc 再写 KB，
    两笔写入之间有毫秒级窗口，紧跟着读 KB 会撞见 building）：再等至多
    15s，KB index_status 到 searchable/failed 才返回。

    Returns: 终态 doc。
    Raises: TimeoutError —— 超时仍未到终态（附最后观测到的状态）。
    """
    import storage.doc_repo as doc_repo
    import storage.kb_repo as kb_repo

    deadline = time.monotonic() + timeout_s
    refreshed = None
    while time.monotonic() < deadline:
        try:
            refreshed = doc_repo.get_doc(kb_id, doc_id)
        except Exception:
            refreshed = None
        if refreshed and refreshed.embedding_status in TERMINAL_STATES:
            break
        time.sleep(0.5)
    if not refreshed or refreshed.embedding_status not in TERMINAL_STATES:
        last = refreshed.embedding_status if refreshed else "doc 不可读"
        raise TimeoutError(
            f"reparse 未在 {timeout_s:.0f}s 内到达终态，最后 doc 状态 {last}"
        )

    kb_deadline = time.monotonic() + 15.0
    kb = None
    while time.monotonic() < kb_deadline:
        try:
            kb = kb_repo.get(kb_id)
        except Exception:
            kb = None
        if kb and kb.index_status in ("searchable", "failed"):
            return refreshed
        time.sleep(0.2)
    last_kb = kb.index_status if kb else "KB 不可读"
    raise TimeoutError(
        f"doc 已到 {refreshed.embedding_status}，但 KB index_status 15s 内"
        f"未落位，最后 {last_kb}"
    )


# ── e2e（需要 Token） ──────────────────────────────────────────────────────────


@requires_paddleocr
def test_reparse_end_to_end_stores_pages_and_reindexes(reparse_target_kb_text):
    """端到端：POST /reparse → 解析文字层 PDF → 落 pages/{doc_id}.json → 索引重建。

    #135 修复：原空白 PDF fixture 触发 #100 空文本守卫（必 failed），正路
    从未被覆盖；改用 ``text_layer_pdfs/s1_p1.pdf``（full_text ≈600 字符，
    layout/by_page 非空）后走到真实 ``pending_index`` → ``embedded``。

    验证：
    1. 调用前后 pages 文件不存在 → 存在
    2. embedding_status 经过 pending_index → embedded 状态机
    3. 重建索引后 chunks 包含文档文本
    """
    from fastapi.testclient import TestClient
    from api.main import app

    kb, doc, content_hash = reparse_target_kb_text

    # 启动前：应当没有 pages 文件
    assert load_pages(kb.id, doc.id) is None, "e2e 前提：pages 文件不应预置"

    client = TestClient(app)
    resp = client.post(f"/api/v1/kb-documents/{doc.id}/reparse")
    assert resp.status_code == 202
    assert resp.json()["status"] == "pending_index"

    # 等后台任务完成（超时/异常由 helper 抛 TimeoutError）
    refreshed = _wait_reparse_terminal(kb.id, doc.id)
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

    # 验证：重建索引后 chunks 包含文档文本（正路闭环，#135 补齐原断言缺口）
    from core.index_manager import search
    hits = search(
        [kb.id], "Safety Production Responsibility System",
        top_k=3, use_reranker=False,
    )
    assert any(
        h.get("doc_id") == doc.id
        and "Safety Production Responsibility" in (h.get("content") or "")
        for h in hits
    ), f"chunks 应包含文档文本，实际 hits={hits!r}"


@requires_paddleocr
def test_reparse_blank_pdf_marks_failed(reparse_target_kb, monkeypatch):
    """空文本守卫（#100/#135）：空白 PDF → ``failed`` + KB 级失败标记。

    #135：空白 PDF 是守卫的靶心场景 —— PaddleOCR 返回空文本 → guard 抛
    ``RuntimeError("parse_document returned empty/sparse text")`` →
    ``_mark_failed``。本用例把守卫行为钉死，防止静默回归：
    1. doc.embedding_status == "failed"
    2. KB index_status == "failed"
    3. KB index_current_doc 记录守卫错误信息

    issue #136：不连真实 OCR —— 测试自己 opt-in：把 ``_paddleocr_call``
    （conftest 网络守卫替换掉的 HTTP seam）换成「总是返回空文本」的桩，模拟
    OCR 对空白页返回空文本，网络守卫保持在场但不会被触发。
    """
    import core.parse_document as pd_module
    from core.parse_document import PageText, ParseResult
    from services.reparse_service import reparse_document as _reparse

    def _empty_ocr_result(file_path, orientation_classify=False):
        return ParseResult(by_page=[PageText(page=0, text="")], full_text="", layout=[])

    monkeypatch.setattr(pd_module, "_paddleocr_call", _empty_ocr_result)

    kb, doc, _ = reparse_target_kb

    resp = _reparse(doc.id)
    assert resp["status"] == "pending_index"

    refreshed = _wait_reparse_terminal(kb.id, doc.id)
    assert refreshed.embedding_status == "failed", (
        f"空白 PDF 应被空文本守卫拦下（failed），实际 {refreshed.embedding_status}"
    )

    import storage.kb_repo as kb_repo
    fresh_kb = kb_repo.get(kb.id)
    assert fresh_kb is not None
    assert fresh_kb.index_status == "failed", (
        f"KB index_status 应为 failed，实际 {fresh_kb.index_status}"
    )
    assert "parse_document returned empty/sparse text" in (fresh_kb.index_current_doc or ""), (
        f"index_current_doc 应记录守卫错误，实际 {fresh_kb.index_current_doc!r}"
    )


@requires_paddleocr
def test_reparse_idempotent_when_cached(reparse_target_kb_text, monkeypatch):
    """缓存命中：再次 reparse 不会重新解析（第二轮 _pymupdf_parse 计数为 0）。

    #135 修复：原空白 PDF 每次 reparse 都解析失败且写不进缓存，第二轮的
    ``_paddleocr_call`` 断言必挂（该名字在 reparse_service 里也不存在，
    直接 AttributeError）；改用文字层 PDF 后首轮把解析结果写入 cache
    （source=pymupdf），第二轮命中缓存直接返回 —— 用 ``_pymupdf_parse``
    计数器证明没有重新解析。
    """
    import core.parse_document as pd_module
    from core import paddleocr_cache as cache_module

    kb, doc, content_hash = reparse_target_kb_text

    # 第一轮：解析 + 写缓存
    from services.reparse_service import reparse_document as _reparse
    _reparse(doc.id)
    _wait_reparse_terminal(kb.id, doc.id)

    # 缓存应已写入（第二轮命中它的前提）
    assert cache_module.cache_state_by_hash(content_hash) == cache_module.CACHE_STATE_HIT, \
        "首轮后缓存条目应存在"

    # 第二轮：缓存应命中，_pymupdf_parse 不应被调用
    called = {"count": 0}

    def _count_calls(*a, **k):
        called["count"] += 1
        raise RuntimeError("should not be called")

    monkeypatch.setattr(pd_module, "_pymupdf_parse", _count_calls)

    _reparse(doc.id)
    _wait_reparse_terminal(kb.id, doc.id)  # 第二轮也完整跑完，断言才非空转
    assert called["count"] == 0, f"重新解析被调 {called['count']} 次（缓存应当命中）"


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
    from core.parse_document import Block, PageLayout, PageText, ParseResult

    kb, doc, _ = reparse_target_kb

    # 准备一份带 layout 的 mock parse_result
    fake_layout = [PageLayout(page=0, blocks=[Block(block_order=0), Block(block_order=1)])]
    fake_parse_result = ParseResult(
        by_page=[PageText(page=0, text="x" * 50)],  # 满足 #100 by_page 守卫
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

        # #150：_reparse_async 现在显式接受 kb_writer；这里造一个 total=1 的实例，
        # 让它走完整的 begin / note_in_flight / finish 生命周期。
        from core.kb_index_status import KbIndexStatusWriter
        kb_writer = KbIndexStatusWriter(kb.id, total=1)

        from services.reparse_service import _reparse_async
        _reparse_async(kb.id, doc.id, kb_writer)

    mock_idx.assert_called_once()
    call = mock_idx.call_args
    # 位置/关键字参数: by_layout 必须在 kwargs 里
    assert call.kwargs.get("by_layout") is fake_layout, (
        f"reparse 必须把 parse_result.layout 传给 index_document.by_layout，"
        f"否则 _inject_block_range 永远拿不到 layout, block_range 永远 None。"
        f"实际 call={call!r}"
    )
