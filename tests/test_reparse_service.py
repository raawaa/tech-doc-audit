"""``services.reparse_service`` 守卫回归测试（issue #101）。

防 #94 假成功指纹复发：``reparse_document`` 必须在
``parse_document`` 返回 ``layout=[]`` 或 ``by_page=[]`` 时显式抛错，
让 ``_mark_failed`` 走到 ``doc.embedding_status="failed"`` 而非误标
``embedded``（chip 预览 "未解析" 的根因类 bug）。

不依赖 PaddleOCR / PyMuPDF 真实 API；用 ``unittest.mock.patch`` 桩出
``parse_document`` / ``save_pages`` / ``remove_document`` / ``index_document``
+ ``kb_repo`` / ``doc_repo``，对 ``_reparse_async`` 做同步调用验证。

跑法：``pytest -m "not requires_paddleocr and not requires_pymupdf"``
"""
from __future__ import annotations

import os

import pytest

from core.parse_document import Block, PageLayout, PageText, ParseResult


# ── marker 声明（与 test_kb_reparse_e2e.py 对齐）────────────────────────────────


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "requires_pymupdf: 需要 PyMuPDF wheel 才跑（默认 CI 跳过，留给 #99 实施）",
    )


# ── 共享 fixture：建一个 KB + doc，monkeypatch AUDIT_DATA_DIR ──────────────────


@pytest.fixture
def reparse_guard_kb(tmp_path, monkeypatch):
    """造一个 KB + 已知 doc，复用 ``reparse_service`` 的真实 doc/kb repo。

    返回 ``(kb, doc)``；doc 的 ``file_path`` 指向真实存在的字节文件，
    因为 ``_reparse_async`` 通过 ``doc_repo.get_doc`` 读出。
    """
    monkeypatch.setenv("AUDIT_DATA_DIR", str(tmp_path))

    import importlib
    import storage.kb_repo
    import storage.doc_repo
    import services.kb_service
    importlib.reload(storage.kb_repo)
    importlib.reload(storage.doc_repo)
    importlib.reload(services.kb_service)

    kb = services.kb_service.create_kb(name="reparse-guard", category="national")
    # 写一个真实的 file_path（_reparse_async 不实际打开它，但需要非空）
    fake_bytes = b"%PDF-1.4 dummy for guard test"
    doc = storage.doc_repo.save_doc(kb.id, "guard.pdf", fake_bytes, "pdf")
    return kb, doc


# ── 守卫测试：3 个核心 case + 1 个补强 case ─────────────────────────────────────


def test_layout_empty_guard_raises_and_marks_failed(reparse_guard_kb, monkeypatch):
    """#94 指纹：``full_text ≥ 20 chars`` 但 ``layout=[]`` → reparse 必须走 failed。

    这正是 #94 假成功指纹的根因——文本够多被误判"成功"，但无 layout → 无
    ``block_range`` → 前端 chip 显示 "未解析"。守卫拦截 → ``_mark_failed``
    → ``doc.embedding_status="failed"`` + ``kb.index_status="failed"``。
    """
    from unittest.mock import MagicMock, patch

    kb, doc = reparse_guard_kb

    # 触发 layout-empty 守卫：by_page 非空、full_text ≥ 20、layout=空
    bad = ParseResult(
        by_page=[PageText(page=0, text="x" * 50)],
        full_text="x" * 50,
        layout=[],
    )
    fake_kb = MagicMock()
    fake_kb.index_status = "building"
    fake_kb.index_current_doc = doc.original_name
    fake_doc = MagicMock()
    fake_doc.embedding_status = "pending_index"
    fake_doc.original_name = doc.original_name

    with patch("services.reparse_service.parse_document", return_value=bad), \
         patch("services.reparse_service.save_pages"), \
         patch("services.reparse_service.remove_document"), \
         patch("services.reparse_service.index_document"), \
         patch("services.reparse_service.kb_repo") as mock_kb_repo, \
         patch("services.reparse_service.doc_repo") as mock_doc_repo:
        mock_kb_repo.get.return_value = fake_kb
        mock_doc_repo.get_doc.return_value = fake_doc

        from services.reparse_service import _reparse_async
        _reparse_async(kb.id, doc.id)

    # 守卫触发后 _mark_failed 应被调用：doc/kb 状态写入 failed
    assert fake_doc.embedding_status == "failed", (
        f"layout 守卫应让 doc 落 embedding_status=failed，实际 {fake_doc.embedding_status}"
    )
    assert fake_kb.index_status == "failed"
    # index_current_doc 应包含错误信息 + doc_id
    assert "reparse 错误" in fake_kb.index_current_doc
    assert doc.id in fake_kb.index_current_doc
    assert "empty layout" in fake_kb.index_current_doc


def test_by_page_empty_guard_raises_and_marks_failed(reparse_guard_kb, monkeypatch):
    """边界 case：``by_page=[]`` + ``full_text ≥ 20`` + ``layout`` 非空 → 守卫拦截。

    模拟"零页解析成功"——layout 元数据存在但没有按页文本；
    走 layout 索引会因 by_page 缺失而下标越界 / chunk 错位。
    守卫必须在 _mark_failed 前显式抛错。
    """
    from unittest.mock import MagicMock, patch

    kb, doc = reparse_guard_kb

    bad = ParseResult(
        by_page=[],  # 触发 by_page 守卫
        full_text="x" * 50,  # 避开 full_text 守卫
        layout=[PageLayout(page=0, blocks=[Block(block_order=0)])],  # 避开 layout 守卫
    )
    fake_kb = MagicMock()
    fake_kb.index_status = "building"
    fake_kb.index_current_doc = doc.original_name
    fake_doc = MagicMock()
    fake_doc.embedding_status = "pending_index"
    fake_doc.original_name = doc.original_name

    with patch("services.reparse_service.parse_document", return_value=bad), \
         patch("services.reparse_service.save_pages"), \
         patch("services.reparse_service.remove_document"), \
         patch("services.reparse_service.index_document"), \
         patch("services.reparse_service.kb_repo") as mock_kb_repo, \
         patch("services.reparse_service.doc_repo") as mock_doc_repo:
        mock_kb_repo.get.return_value = fake_kb
        mock_doc_repo.get_doc.return_value = fake_doc

        from services.reparse_service import _reparse_async
        _reparse_async(kb.id, doc.id)

    assert fake_doc.embedding_status == "failed"
    assert fake_kb.index_status == "failed"
    assert doc.id in fake_kb.index_current_doc
    assert "empty by_page" in fake_kb.index_current_doc


def test_all_empty_parse_result_raises_and_marks_failed(reparse_guard_kb, monkeypatch):
    """Issue #101 acceptance test 2 原文：``by_page=[] / full_text="" / layout=[]``
    → reparse 抛错 → doc 落 failed。

    注意：现有 ``full_text`` 长度守卫会先于新守卫触发；本测试只验证
    "任意一种空状态都不会被误标 embedded" 的 defense-in-depth 目标。
    """
    from unittest.mock import MagicMock, patch

    kb, doc = reparse_guard_kb

    bad = ParseResult(by_page=[], full_text="", layout=[])
    fake_kb = MagicMock()
    fake_kb.index_status = "building"
    fake_kb.index_current_doc = doc.original_name
    fake_doc = MagicMock()
    fake_doc.embedding_status = "pending_index"
    fake_doc.original_name = doc.original_name

    with patch("services.reparse_service.parse_document", return_value=bad), \
         patch("services.reparse_service.save_pages"), \
         patch("services.reparse_service.remove_document"), \
         patch("services.reparse_service.index_document"), \
         patch("services.reparse_service.kb_repo") as mock_kb_repo, \
         patch("services.reparse_service.doc_repo") as mock_doc_repo:
        mock_kb_repo.get.return_value = fake_kb
        mock_doc_repo.get_doc.return_value = fake_doc

        from services.reparse_service import _reparse_async
        _reparse_async(kb.id, doc.id)

    assert fake_doc.embedding_status == "failed"
    assert fake_kb.index_status == "failed"
    assert "reparse 错误" in fake_kb.index_current_doc


def test_parse_document_raises_still_reaches_mark_failed(reparse_guard_kb, monkeypatch):
    """Issue #101 acceptance test 3：``parse_document`` 抛异常 → ``_mark_failed`` 仍被调用。

    守卫不能"吞掉"上层异常（即使守卫本身都通过）；任何意外异常都应走
    ``_mark_failed``，保持 doc.embedding_status="failed" 的契约。
    """
    from unittest.mock import MagicMock, patch

    kb, doc = reparse_guard_kb

    def _explode(*a, **k):
        raise RuntimeError("simulated parse_document outage")

    fake_kb = MagicMock()
    fake_kb.index_status = "building"
    fake_kb.index_current_doc = doc.original_name
    fake_doc = MagicMock()
    fake_doc.embedding_status = "pending_index"
    fake_doc.original_name = doc.original_name

    with patch("services.reparse_service.parse_document", side_effect=_explode), \
         patch("services.reparse_service.save_pages"), \
         patch("services.reparse_service.remove_document"), \
         patch("services.reparse_service.index_document"), \
         patch("services.reparse_service.kb_repo") as mock_kb_repo, \
         patch("services.reparse_service.doc_repo") as mock_doc_repo:
        mock_kb_repo.get.return_value = fake_kb
        mock_doc_repo.get_doc.return_value = fake_doc

        from services.reparse_service import _reparse_async
        # 异常应被 except 捕获 → 走 _mark_failed → 不应向上抛
        _reparse_async(kb.id, doc.id)

    assert fake_doc.embedding_status == "failed", (
        "parse_document 抛异常时 doc 仍应落 embedding_status=failed"
    )
    assert fake_kb.index_status == "failed"
    assert "simulated parse_document outage" in fake_kb.index_current_doc
    # doc_id 不在错误消息里（异常来自 parse_document 而非守卫），但应包含异常 msg
