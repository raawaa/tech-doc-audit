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
#
# #150 后 KB 终态走 ``KbIndexStatusWriter.finish(failed=[...])``，字段 ``index_current_doc``
# 的字面格式从 ``"reparse 错误: <err>"`` 收归到 writer 的
# ``"批量重新解析失败 N/M 篇（name: outcome）"`` —— 单篇 ``N=M=1`` 同样适用。
# 因此断言只检"failed 状态 + 错误串里能搜到原文 + doc_id"，不再断言旧前缀。


def _run_reparse_async_with_writer(kb_id, *, parse, kb_writer):
    """同步跑 ``_reparse_async(kb_id, doc_id, kb_writer)`` 并返回 spy mock。

    返回 ``(fake_kb, fake_doc, mock_update)``：
    - ``fake_kb`` / ``fake_doc`` 是 MagicMock，函数与 writer 都写它们，断言可读；
    - ``mock_update`` 是 ``storage.kb_repo.update`` 的 MagicMock，记录调用次数。
    """
    from unittest.mock import MagicMock, patch
    import storage.kb_repo

    fake_kb = MagicMock()
    fake_kb.index_status = "searchable"
    fake_kb.index_progress = 1.0
    fake_kb.index_current_doc = ""
    fake_doc = MagicMock()
    fake_doc.embedding_status = "pending_index"
    fake_doc.original_name = "doc_x"

    patch_parse = (
        patch("services.reparse_service.parse_document", side_effect=parse)
        if callable(parse)
        else patch("services.reparse_service.parse_document", return_value=parse)
    )

    # ``services.reparse_service.kb_repo`` 与 ``core.kb_index_status.kb_repo``
    # 都只是 ``storage.kb_repo`` 的 alias —— 直接 patch 底下的 ``get`` / ``update``
    # 同时影响两边。
    with patch_parse, \
         patch("services.reparse_service.save_pages"), \
         patch("services.reparse_service.remove_document"), \
         patch("services.reparse_service.index_document"), \
         patch.object(storage.kb_repo, "get") as mock_get, \
         patch.object(storage.kb_repo, "update") as mock_update, \
         patch("services.reparse_service.doc_repo") as mock_doc_repo:
        mock_get.return_value = fake_kb
        mock_doc_repo.get_doc.return_value = fake_doc

        from services.reparse_service import _reparse_async
        _reparse_async(kb_id, "doc_x", kb_writer)

    return fake_kb, fake_doc, mock_update


def test_layout_empty_guard_raises_and_marks_failed(reparse_guard_kb):
    """#94 指纹：``full_text ≥ 20 chars`` 但 ``layout=[]`` → reparse 必须走 failed。

    这正是 #94 假成功指纹的根因——文本够多被误判"成功"，但无 layout → 无
    ``block_range`` → 前端 chip 显示 "未解析"。守卫拦截 → ``_mark_failed``
    → ``doc.embedding_status="failed"`` + ``kb.index_status="failed"``。
    """
    kb, doc = reparse_guard_kb

    bad = ParseResult(
        by_page=[PageText(page=0, text="x" * 50)],
        full_text="x" * 50,
        layout=[],
    )

    from core.kb_index_status import KbIndexStatusWriter
    writer = KbIndexStatusWriter(kb.id, total=1)
    fake_kb, fake_doc, _mock_kb_repo = _run_reparse_async_with_writer(
        kb.id, parse=bad, kb_writer=writer,
    )

    assert fake_doc.embedding_status == "failed", (
        f"layout 守卫应让 doc 落 embedding_status=failed，实际 {fake_doc.embedding_status}"
    )
    assert fake_kb.index_status == "failed"
    # writer.finish(failed=[(name, err)]) 把 doc_name 与 err 都收进 current_doc
    assert "empty layout" in fake_kb.index_current_doc


def test_by_page_empty_guard_raises_and_marks_failed(reparse_guard_kb):
    """边界 case：``by_page=[]`` + ``full_text ≥ 20`` + ``layout`` 非空 → 守卫拦截。

    模拟"零页解析成功"——layout 元数据存在但没有按页文本；
    走 layout 索引会因 by_page 缺失而下标越界 / chunk 错位。
    守卫必须在 _mark_failed 前显式抛错。
    """
    kb, doc = reparse_guard_kb

    bad = ParseResult(
        by_page=[],  # 触发 by_page 守卫
        full_text="x" * 50,  # 避开 full_text 守卫
        layout=[PageLayout(page=0, blocks=[Block(block_order=0)])],  # 避开 layout 守卫
    )

    from core.kb_index_status import KbIndexStatusWriter
    writer = KbIndexStatusWriter(kb.id, total=1)
    fake_kb, fake_doc, _mock_kb_repo = _run_reparse_async_with_writer(
        kb.id, parse=bad, kb_writer=writer,
    )

    assert fake_doc.embedding_status == "failed"
    assert fake_kb.index_status == "failed"
    assert "empty by_page" in fake_kb.index_current_doc


def test_all_empty_parse_result_raises_and_marks_failed(reparse_guard_kb):
    """Issue #101 acceptance test 2 原文：``by_page=[] / full_text="" / layout=[]``
    → reparse 抛错 → doc 落 failed。

    注意：现有 ``full_text`` 长度守卫会先于新守卫触发；本测试只验证
    "任意一种空状态都不会被误标 embedded" 的 defense-in-depth 目标。
    """
    kb, doc = reparse_guard_kb

    bad = ParseResult(by_page=[], full_text="", layout=[])

    from core.kb_index_status import KbIndexStatusWriter
    writer = KbIndexStatusWriter(kb.id, total=1)
    fake_kb, fake_doc, _mock_kb_repo = _run_reparse_async_with_writer(
        kb.id, parse=bad, kb_writer=writer,
    )

    assert fake_doc.embedding_status == "failed"
    assert fake_kb.index_status == "failed"


def test_parse_document_raises_still_reaches_mark_failed(reparse_guard_kb):
    """Issue #101 acceptance test 3：``parse_document`` 抛异常 → ``_mark_failed`` 仍被调用。

    守卫不能"吞掉"上层异常（即使守卫本身都通过）；任何意外异常都应走
    ``_mark_failed``，保持 doc.embedding_status="failed" 的契约。
    """
    kb, doc = reparse_guard_kb

    def _explode(*a, **k):
        raise RuntimeError("simulated parse_document outage")

    from core.kb_index_status import KbIndexStatusWriter
    writer = KbIndexStatusWriter(kb.id, total=1)
    fake_kb, fake_doc, _mock_kb_repo = _run_reparse_async_with_writer(
        kb.id, parse=_explode, kb_writer=writer,
    )

    assert fake_doc.embedding_status == "failed", (
        "parse_document 抛异常时 doc 仍应落 embedding_status=failed"
    )
    assert fake_kb.index_status == "failed"
    assert "simulated parse_document outage" in fake_kb.index_current_doc


# ── "调用方托管 KB 状态"模式（issue #109 → #150）───────────────────────────────
#
# 批量重新解析时，每篇完成都把 KB 写回 searchable 会让 KB 在整批期间
# 在 building ⇄ searchable 之间抖动上百次（#93 实测）。#109 引入
# ``caller_manages_kb_status`` 布尔让批量绕过单篇写终态；#150 把它
# 收归 ``KbIndexStatusWriter``：批量注入跨文档共享的 writer，单篇在自己
# 构造 ``total=1`` 的 writer 上走完整生命周期。两种情形都通过同一个
# ``_reparse_async`` 跑 —— 区别只在 writer 是谁造的、单篇 ``finish()`` 的
# 字面格式是否被压制（``_total > 1`` 时单篇失败只写 current_doc、不写终态）。
#
# 4 个 caller_manages_kb_status plumbing 测试塌成 2 个：
# ① 默认 = 自己造 total=1 writer；② 注入 = 用注入的 writer（不再断言 kwargs 透传）。


def _good_parse_result() -> ParseResult:
    """一份能走完整条成功路径的解析结果（三道守卫全过）。"""
    return ParseResult(
        by_page=[PageText(page=0, text="x" * 50)],
        full_text="x" * 50,
        layout=[PageLayout(page=0, blocks=[Block(block_order=0)])],
    )


def test_default_mode_writes_kb_status(reparse_guard_kb):
    """默认（``kb_writer=None``）：函数自己造一个 ``KbIndexStatusWriter(total=1)``
    并调它的 ``begin / note_in_flight / finish``，KB 终态由它写。

    这是 per-doc API 端点与单篇 UI 按钮走的那条路径 —— #150 后默认行为
    不变，"自己管 KB 状态" 的语义从"分支判断"变成"无脑调 writer API"。
    """
    from unittest.mock import patch
    from core.kb_index_status import KbIndexStatusWriter

    kb, doc = reparse_guard_kb

    # spy on KbIndexStatusWriter 构造：函数应在自己构造一个 total=1 的实例。
    constructed: list[tuple[str, int]] = []
    real_init = KbIndexStatusWriter.__init__

    def spy_init(self, kb_id_arg, total=1):
        constructed.append((kb_id_arg, total))
        real_init(self, kb_id_arg, total=total)

    with patch.object(KbIndexStatusWriter, "__init__", spy_init), \
         patch("services.reparse_service.threading.Thread"):
        from services.reparse_service import reparse_document
        reparse_document(doc.id)

    assert any(kb_id_arg == kb.id and total == 1 for kb_id_arg, total in constructed), (
        f"默认模式下函数必须自己构造 KbIndexStatusWriter(kb_id={kb.id}, total=1)；"
        f"实际构造记录: {constructed}"
    )


def test_provided_writer_takes_over_kb_status(reparse_guard_kb):
    """``kb_writer`` 注入：函数用注入的实例，不再自己造新 writer，
    stub 的 ``begin / note_in_flight / finish`` 被按预期调用。

    这是批量重新解析走的那条路径 —— 编排层持有跨文档共享的 writer，
    单篇入口只往里写自己那一格。断言从"kwargs 透传"转向"writer 接口被调"，
    是 #150 把 seam 从布尔收到 writer 的语义落点。
    """
    from unittest.mock import MagicMock, patch
    from core.kb_index_status import KbIndexStatusWriter

    kb, doc = reparse_guard_kb

    stub_writer = MagicMock(spec=KbIndexStatusWriter)

    # 如果函数自己构造 KbIndexStatusWriter，spy 会记录；这里要求"一次都不构造"。
    def fail_on_new_writer(self, *args, **kwargs):
        raise AssertionError(
            f"传入 kb_writer 时函数不应再构造新 writer（args={args}, kwargs={kwargs}）"
        )

    fake_doc = MagicMock()
    fake_doc.embedding_status = "pending_index"
    fake_doc.original_name = doc.original_name

    with patch.object(KbIndexStatusWriter, "__init__", fail_on_new_writer), \
         patch("services.reparse_service.parse_document", return_value=_good_parse_result()), \
         patch("services.reparse_service.save_pages"), \
         patch("services.reparse_service.remove_document"), \
         patch("services.reparse_service.index_document"), \
         patch("services.reparse_service.doc_repo") as mock_doc_repo:
        mock_doc_repo.get_doc.return_value = fake_doc

        from services.reparse_service import _reparse_async
        _reparse_async(kb.id, "doc_x", stub_writer)

    # stub writer 应按生命周期被调用，参数按约定传
    stub_writer.begin.assert_called_once_with()
    stub_writer.note_in_flight.assert_called_once_with(doc.original_name)
    stub_writer.finish.assert_called_once_with()
