"""``core.pdf_splitter`` 单元测试 (issue #173 / #176)。

覆盖 spec §B 的 11 条 AC（acceptance criteria），主 seam 是
:func:`core.parse_document.parse_document`。Fake ``_paddleocr_call`` 的语义:

- 输入是子 PDF（``chunk_{i:03d}_{start}-{end}.pdf``）;按 ``start`` 解析为
  "global page offset",产生 ``by_page[j].text = f"page-{start+j:03d}"``。
- 主源 PDF（非 chunk 前缀）→ 同模式,``start=0``。
- 这样 99 页源直接解析与 247 页源走 splitter 的"前 99 页"产出逐字节一致
  → 直接断言 AC#4。

PyMuPDF 是 splitter 与 chunk 写盘的唯一依赖;无 wheel 整文件 skip。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import pytest


# ── PyMuPDF 探测 ──────────────────────────────────────────────────────────────


try:
    import pymupdf  # type: ignore[import-not-found]
    _HAVE_PYMUPDF = True
except Exception:
    pymupdf = None  # type: ignore[assignment]
    _HAVE_PYMUPDF = False


pytestmark = [
    pytest.mark.skipif(
        not _HAVE_PYMUPDF,
        reason="pymupdf wheel not installed (issue #99 / #176)",
    ),
]


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "requires_pymupdf: 需要 pymupdf wheel 才跑",
    )


# ── SUT ──────────────────────────────────────────────────────────────────────


from core import parse_document as pd_module  # noqa: E402
from core import pdf_splitter as splitter  # noqa: E402
from core.parse_document import (  # noqa: E402
    ParseResult, PageText, PageLayout, Block,
    parse_document,
)
from core.settings import PADDLEOCR_PAGE_LIMIT, PDF_SPLIT_CHUNK_PAGES  # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_blank_pdf(path: Path, page_count: int) -> Path:
    """生成 ``page_count`` 页空白 PDF（不插文字 → 文字层空 → 走 PaddleOCR 路径）。

    与 ``test_pymupdf_parse._make_text_only`` 的差别:这里不要 insert_text,
    让 ``_is_text_layer_pdf`` 返回 False,以触发扫描件 → PaddleOCR 路径。
    该 helper 同时承担"扫描件 PDF"的语义 —— 类名 ``TestSplitPages`` /
    ``TestScratchCleanup`` 的命名已足够传达意图,不需要单独的
    ``_make_scanned_split_pdf`` rename-only alias。
    """
    doc = pymupdf.open()
    for _ in range(page_count):
        doc.new_page(width=595, height=842)
    doc.save(str(path))
    doc.close()
    return path


def _chunk_offset_from_path(file_path: str) -> int:
    """从 chunk 文件名 ``chunk_{i:03d}_{start}-{end}.pdf`` 解析出 ``start``。

    主源 PDF(非 chunk 前缀)→ 返回 0;测试 mock 用此函数让 99 页源与
    247 页拆分第一块产出**逐字节相同**的 page 文本(AC#4)。
    """
    name = Path(file_path).name
    if not name.startswith("chunk_"):
        return 0
    # chunk_000_099-197.pdf
    parts = name.replace(".pdf", "").split("_")
    start_end = parts[2]  # "099-197"
    return int(start_end.split("-")[0])


def _fake_paddleocr_call(file_path: str, orientation_classify: bool = False):
    """测试 fake:``_paddleocr_call`` 按"global page offset"产出确定性 page 文本。

    - chunk 文件名 → 解析 ``start``;主源 PDF → start=0
    - by_page[j].text = ``f"page-{start+j:03d}"`` (每个 page 长度足够让
      full_text 满足 :data:`MIN_FULL_TEXT_CHARS` ≥ 20 的"非空重试"门槛;
      1-页 chunk 也能避免触发 ``orientation_classify=True`` 重试。
    - full_text = 按上述文本 join
    - layout = 空 PageLayout 列表

    配合 :func:`_make_blank_pdf`,文字层空 → 触发 PaddleOCR 路径;产出文本
    完全可预测 → 可在 AC#4 / AC#5 直接断言。
    """
    start = _chunk_offset_from_path(file_path)
    with pymupdf.open(file_path) as d:
        n = d.page_count
    by_page = [PageText(page=j, text=f"page-{start+j:03d} text body") for j in range(n)]
    full_text = "\n\n".join(f"page-{start+j:03d} text body" for j in range(n))
    layout = [PageLayout(page=j, width=595, height=842) for j in range(n)]
    return ParseResult(by_page=by_page, full_text=full_text, layout=layout)


@pytest.fixture
def fake_paddleocr(monkeypatch):
    """把 ``_paddleocr_call`` 替换为 :func:`_fake_paddleocr_call`,并允许 PaddleOCR 路径。

    同时强制 ``_paddleocr_available`` 为 True(否则路由层在扫描件上抛
    "PaddleOCR API not configured",根本不进 splitter)。
    """
    monkeypatch.setattr(pd_module, "_paddleocr_call", _fake_paddleocr_call)
    monkeypatch.setattr(pd_module, "_paddleocr_available", lambda: True)
    return _fake_paddleocr_call


# ── chunk_ranges: pure function ──────────────────────────────────────────────


class TestChunkRanges:
    """纯函数 ``chunk_ranges`` —— bulk 预检与 splitter 共用的唯一实现(AC#1 工具)。"""

    def test_zero_pages_returns_empty(self):
        assert splitter.chunk_ranges(0) == []

    def test_negative_pages_returns_empty(self):
        assert splitter.chunk_ranges(-5) == []

    def test_pages_below_limit_returns_single_chunk(self):
        # 99 页以下(含边界减一)→ 单块 (0, n-1)
        assert splitter.chunk_ranges(99) == [(0, 98)]
        assert splitter.chunk_ranges(1) == [(0, 0)]
        assert splitter.chunk_ranges(50) == [(0, 49)]

    def test_pages_above_limit_splits_into_equal_chunks(self):
        # 247 = 99 + 99 + 49 → 三块;前两块满,最后一块余数
        assert splitter.chunk_ranges(247) == [(0, 98), (99, 197), (198, 246)]

    def test_pages_exactly_multiple_of_chunk(self):
        # 198 = 99 * 2 → 两块满
        assert splitter.chunk_ranges(198) == [(0, 98), (99, 197)]

    def test_pages_one_over_chunk(self):
        # 100 = 99 + 1 → 两块 (0..98) + (99..99)
        assert splitter.chunk_ranges(100) == [(0, 98), (99, 99)]

    def test_one_page(self):
        assert splitter.chunk_ranges(1) == [(0, 0)]

    def test_uses_PDF_SPLIT_CHUNK_PAGES(self):
        # 与 PDF_SPLIT_CHUNK_PAGES 解耦:**不**硬写 99,改 env 也能对齐
        # (env 在 import 时锁定,所以这里只断言二者关系)。
        chunk = PDF_SPLIT_CHUNK_PAGES
        n = chunk * 3 + 7
        ranges = splitter.chunk_ranges(n)
        assert len(ranges) == 4
        assert ranges[-1] == (chunk * 3, n - 1)


# ── AC#1: 247 pages scanned → by_page len / page numbers / layout len ────────


class TestSplitPages:
    """AC#1 — 247 页扫描件走拆分路径后 by_page / layout 形状正确。"""

    def test_247_page_scanned_pdf_produces_full_by_page(self, tmp_path, fake_paddleocr):
        pdf = _make_blank_pdf(tmp_path / "big.pdf", 247)
        pr = parse_document(str(pdf), use_cache=False)
        assert len(pr.by_page) == 247
        assert [p.page for p in pr.by_page] == list(range(247))
        assert len(pr.layout) == 247
        assert [pl.page for pl in pr.layout] == list(range(247))

    def test_split_page_numbers_match_chunk_ranges(self, tmp_path, fake_paddleocr):
        """每块的缝合页号 = 该块在源 PDF 的起始页号 + 块内偏移。"""
        pdf = _make_blank_pdf(tmp_path / "big.pdf", 247)
        pr = parse_document(str(pdf), use_cache=False)
        # 块 0: 0..98, 块 1: 99..197, 块 2: 198..246
        # 每块的 by_page[j].page == start + j
        assert [p.page for p in pr.by_page[0:99]] == list(range(0, 99))
        assert [p.page for p in pr.by_page[99:198]] == list(range(99, 198))
        assert [p.page for p in pr.by_page[198:247]] == list(range(198, 247))


# ── AC#2: cache write after split (source=paddleocr_split) ───────────────────


class TestSplitCache:
    """AC#2 — 拆分解析跑完缓存命中,``source == 'paddleocr_split'``。"""

    def test_split_result_cached_under_source_pdf_hash(
        self, tmp_path, fake_paddleocr, monkeypatch,
    ):
        from core import paddleocr_cache as cache_module

        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        monkeypatch.setattr(cache_module, "get_cache_dir", lambda: cache_dir)

        pdf = _make_blank_pdf(tmp_path / "big.pdf", 247)
        parse_document(str(pdf), use_cache=True)

        # get_cached 返 ``result`` 字段(dict),source 在外层 entry。
        cached_result = cache_module.get_cached(str(pdf))
        assert cached_result is not None
        assert len(cached_result["by_page"]) == 247

        # 整个 entry 含 source 字段,断言 == 'paddleocr_split'
        entries = list(cache_dir.glob("*.json"))
        assert entries
        raw = json.loads(entries[0].read_text(encoding="utf-8"))
        assert raw["source"] == "paddleocr_split"


# ── AC#3: 二次调用命中缓存,不再调 _paddleocr_call ───────────────────────────


class TestCacheHit:
    """AC#3 — 同一文档第二次解析走缓存,``_paddleocr_call`` 不再被调。"""

    def test_second_call_does_not_invoke_paddleocr(self, tmp_path, monkeypatch):
        from core import paddleocr_cache as cache_module

        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        monkeypatch.setattr(cache_module, "get_cache_dir", lambda: cache_dir)

        call_count = [0]

        def _counting_call(file_path, orientation_classify=False):
            call_count[0] += 1
            return _fake_paddleocr_call(file_path, orientation_classify)

        monkeypatch.setattr(pd_module, "_paddleocr_call", _counting_call)
        monkeypatch.setattr(pd_module, "_paddleocr_available", lambda: True)

        pdf = _make_blank_pdf(tmp_path / "big.pdf", 247)

        # 首次:走 splitter → 3 块 → 3 次 _paddleocr_call
        pr1 = parse_document(str(pdf), use_cache=True)
        assert call_count[0] == 3

        # 二次:缓存命中,不应该再调
        pr2 = parse_document(str(pdf), use_cache=True)
        assert call_count[0] == 3, (
            f"second call should hit cache, but _paddleocr_call was "
            f"invoked {call_count[0]} times total"
        )
        assert pr2.full_text == pr1.full_text


# ── AC#4: full_text 等价(拆分 vs 不拆) ───────────────────────────────────────


class TestFullTextEquivalence:
    """AC#4 — 同一原文的 99 页(不拆)与 247 页前 99 页走拆分 → full_text 逐字节相等。"""

    def test_first_chunk_full_text_matches_unsplit_99_page(
        self, tmp_path, fake_paddleocr,
    ):
        # 99 页源:直接 parse_document,full_text = "page-000\n\n...\n\npage-098"
        pdf_99 = _make_blank_pdf(tmp_path / "doc_99.pdf", 99)
        pr_99 = parse_document(str(pdf_99), use_cache=False)

        # 247 页源:走 splitter
        pdf_247 = _make_blank_pdf(tmp_path / "doc_247.pdf", 247)
        pr_247 = parse_document(str(pdf_247), use_cache=False)

        # chunk mock 按 global offset 标号 → 两份产出的前 99 页文本逐字节一致
        first_99 = "\n\n".join(p.text for p in pr_247.by_page[:99] if p.text)
        # _normalize_headings 介入;两份等价前提是头 99 页无 heading 修复差异。
        # 我们的 fake 文本为 "page-XXX",HeadingProcessor 不会改它。
        assert first_99 == pr_99.full_text


# ── AC#5: block_order 保持 per-page-local,不跨块重编号 ─────────────────────


class TestBlockOrderLocal:
    """AC#5 — 拆分缝合后 ``Block.block_order`` 不跨块重编号。"""

    def test_block_order_per_page_local_across_chunks(self, tmp_path, monkeypatch):
        """每个块内 block_order 从 0 起;跨块不连续。

        用带 layout 的 fake 让每页 emit 一个 block:
        """
        from core.paddleocr_cache import SOURCE_PADDLEOCR_SPLIT

        # 替换 fake 让 by_page / layout 含一个 block_order=N 的 block
        def _call_with_blocks(file_path, orientation_classify=False):
            start = _chunk_offset_from_path(file_path)
            with pymupdf.open(file_path) as d:
                n = d.page_count
            by_page = [PageText(page=j, text=f"page-{start+j:03d}") for j in range(n)]
            full_text = "\n\n".join(f"page-{start+j:03d}" for j in range(n))
            layout = [
                PageLayout(
                    page=j, width=595, height=842,
                    blocks=[Block(
                        block_label="text", block_content=f"x{start+j}",
                        bbox_norm=[0.1, 0.1, 0.9, 0.9],
                        polygon_norm=[], block_order=j,
                    )],
                )
                for j in range(n)
            ]
            return ParseResult(by_page=by_page, full_text=full_text, layout=layout)

        monkeypatch.setattr(pd_module, "_paddleocr_call", _call_with_blocks)
        monkeypatch.setattr(pd_module, "_paddleocr_available", lambda: True)

        pdf = _make_blank_pdf(tmp_path / "big.pdf", 247)
        pr = parse_document(str(pdf), use_cache=False)

        # 每页 block_order == 块内 j(per-page-local);跨块不连续(每块从 0 起)
        # 块 0: layout[0..98],每个 block_order == j
        # 块 1: layout[99..197],每个 block_order == j (j 从 0 起,不是 99)
        for i in range(0, 99):
            assert pr.layout[i].blocks[0].block_order == i
        for i in range(99, 198):
            local = i - 99
            assert pr.layout[i].blocks[0].block_order == local, (
                f"跨块 block_order 被重编号了:layout[{i}] = "
                f"{pr.layout[i].blocks[0].block_order},期望 {local}"
            )
        for i in range(198, 247):
            local = i - 198
            assert pr.layout[i].blocks[0].block_order == local


# ── AC#6: 100 页边界不拆分 ──────────────────────────────────────────────────


class TestBoundary:
    """AC#6 — 恰好 100 页(PADDLEOCR_PAGE_LIMIT)的扫描件**不**走 splitter。"""

    def test_exactly_100_pages_does_not_split(self, tmp_path, monkeypatch):
        """100 页 → 不拆分,_paddleocr_call 被调一次,输入是源文件本身。"""
        call_inputs = []

        def _record_call(file_path, orientation_classify=False):
            call_inputs.append(Path(file_path).resolve())
            return _fake_paddleocr_call(file_path, orientation_classify)

        monkeypatch.setattr(pd_module, "_paddleocr_call", _record_call)
        monkeypatch.setattr(pd_module, "_paddleocr_available", lambda: True)

        pdf = _make_blank_pdf(tmp_path / "boundary.pdf", 100)
        pr = parse_document(str(pdf), use_cache=False)

        # 100 == limit,不拆分(> 才拆分)
        assert len(pr.by_page) == 100
        # _paddleocr_call 应当被调用 1 次,且收到的是源 PDF 本身
        assert len(call_inputs) == 1
        assert call_inputs[0] == pdf.resolve()

    def test_just_over_limit_splits(self, tmp_path, monkeypatch):
        """101 页 → 拆成 2 块,_paddleocr_call 被调 2 次,输入是 chunk。"""
        call_inputs = []

        def _record_call(file_path, orientation_classify=False):
            call_inputs.append(Path(file_path).resolve())
            return _fake_paddleocr_call(file_path, orientation_classify)

        monkeypatch.setattr(pd_module, "_paddleocr_call", _record_call)
        monkeypatch.setattr(pd_module, "_paddleocr_available", lambda: True)

        pdf = _make_blank_pdf(tmp_path / "over.pdf", 101)
        pr = parse_document(str(pdf), use_cache=False)

        assert len(pr.by_page) == 101
        assert len(call_inputs) == 2
        # 两次调用都不是源 PDF(都是 chunk)
        for inp in call_inputs:
            assert inp != pdf.resolve()


# ── AC#7: 文字层 PDF 不调 PaddleOCR ──────────────────────────────────────────


class TestTextLayerUnaffected:
    """AC#7 — 500 页文字层 PDF 走 PyMuPDF 路径,_paddleocr_call / _paddleocr_parse 一次都不调。"""

    def test_500_page_text_layer_pdf_skips_paddleocr(self, tmp_path, monkeypatch):
        from core.paddleocr_cache import get_cache_dir

        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        monkeypatch.setattr(
            __import__("core.paddleocr_cache", fromlist=["get_cache_dir"]),
            "get_cache_dir",
            lambda: cache_dir,
        )

        def _explode(*a, **k):
            raise AssertionError("PaddleOCR must not be called on text-layer PDFs")

        monkeypatch.setattr(pd_module, "_paddleocr_call", _explode)
        monkeypatch.setattr(pd_module, "_paddleocr_parse", _explode)

        # 文字层 PDF:每页插一段文字 → _is_text_layer_pdf 返回 True
        doc = pymupdf.open()
        for i in range(500):
            page = doc.new_page(width=595, height=842)
            page.insert_text((50, 50), f"page {i + 1} content", fontsize=11)
        text_pdf = tmp_path / "text_500.pdf"
        doc.save(str(text_pdf))
        doc.close()

        pr = parse_document(str(text_pdf), use_cache=False)
        assert len(pr.by_page) == 500
        # 第二次 use_cache=True 才会写 cache;这条主要验"PaddleOCR 没被调"。
        parse_document(str(text_pdf), use_cache=True)
        from core.paddleocr_cache import get_cached
        cached = get_cached(str(text_pdf))
        assert cached is not None
        # PyMuPDF 路径的 cache source 是 "pymupdf"
        entries = list(cache_dir.glob("*.json"))
        raw = json.loads(entries[0].read_text(encoding="utf-8"))
        assert raw["source"] == "pymupdf"


# ── AC#8: 块页数对账失败 → RuntimeError + 缓存不写 ──────────────────────────


class TestReconciliationFailure:
    """AC#8 — 拆分中间某块解析产出的 by_page 数 != 该块物理页数 → RuntimeError,且缓存未写。"""

    def test_chunk_returns_wrong_page_count_raises_runtime_error(
        self, tmp_path, monkeypatch,
    ):
        from core import paddleocr_cache as cache_module

        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        monkeypatch.setattr(cache_module, "get_cache_dir", lambda: cache_dir)

        def _short_chunk(file_path, orientation_classify=False):
            """第 2 块(若存在)少返一页 → 触发页数对账失败。"""
            start = _chunk_offset_from_path(file_path)
            with pymupdf.open(file_path) as d:
                actual_n = d.page_count
            # 探测"自己是第几块":从 start 推断
            # 块 0: start=0;块 1: start=99;块 2: start=198
            # 247 页 → 块 1 触发(少 1 页 → 报 expected 99 got 98)
            n = actual_n
            if start == 99:
                n = actual_n - 1  # 故意少返 1 页
            by_page = [PageText(page=j, text=f"page-{start+j:03d}") for j in range(n)]
            full_text = "\n\n".join(f"page-{start+j:03d}" for j in range(n))
            layout = [PageLayout(page=j) for j in range(n)]
            return ParseResult(by_page=by_page, full_text=full_text, layout=layout)

        monkeypatch.setattr(pd_module, "_paddleocr_call", _short_chunk)
        monkeypatch.setattr(pd_module, "_paddleocr_available", lambda: True)

        pdf = _make_blank_pdf(tmp_path / "bad.pdf", 247)
        with pytest.raises(RuntimeError, match="page-count reconciliation"):
            parse_document(str(pdf), use_cache=True)

        # 缓存不写:get_cached 应该 None
        assert cache_module.get_cached(str(pdf)) is None


# ── AC#9: 临时目录清理 ──────────────────────────────────────────────────────


class TestScratchCleanup:
    """AC#9 — split 成功不留 .scratch/split_pdfs/ 残留;失败保留给 reaper。"""

    def _scratch_root(self) -> Path:
        from core.data_dir import get_data_dir
        return get_data_dir() / ".scratch" / "split_pdfs"

    def test_success_cleans_up_scratch_dir(self, tmp_path, monkeypatch, fake_paddleocr):
        # AUDIT_DATA_DIR 已经被 _per_test_data_dir 指向 tmp_path
        # (conftest autouse),直接 parse_document 即可
        pdf = _make_blank_pdf(tmp_path / "ok.pdf", 247)
        parse_document(str(pdf), use_cache=False)

        # 成功后 .scratch/split_pdfs/ 下不应有该 PDF 派生出的子目录
        root = self._scratch_root()
        assert not _has_doc_subdir(root, pdf)

    def test_failure_preserves_scratch_dir(self, tmp_path, monkeypatch):
        """失败时临时目录保留,以便 reaper / 运维介入。"""
        from core import paddleocr_cache as cache_module

        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        monkeypatch.setattr(cache_module, "get_cache_dir", lambda: cache_dir)

        def _bad_chunk(file_path, orientation_classify=False):
            start = _chunk_offset_from_path(file_path)
            with pymupdf.open(file_path) as d:
                actual_n = d.page_count
            n = actual_n - 1 if start == 99 else actual_n  # 块 1 触发对账失败
            by_page = [PageText(page=j, text=f"page-{start+j:03d}") for j in range(n)]
            return ParseResult(
                by_page=by_page,
                full_text="",
                layout=[PageLayout(page=j) for j in range(n)],
            )

        monkeypatch.setattr(pd_module, "_paddleocr_call", _bad_chunk)
        monkeypatch.setattr(pd_module, "_paddleocr_available", lambda: True)

        pdf = _make_blank_pdf(tmp_path / "fail.pdf", 247)
        with pytest.raises(RuntimeError):
            parse_document(str(pdf), use_cache=True)

        # 失败后 .scratch/split_pdfs/ 下应有该 PDF 派生出的子目录(供 reaper 接管)
        root = self._scratch_root()
        assert _has_doc_subdir(root, pdf), (
            f"失败保留临时目录,但 {root} 下没有 {pdf.name} 派生出的子目录"
        )


def _has_doc_subdir(scratch_root: Path, source_pdf: Path) -> bool:
    """scratch_root 下是否存在 ``source_pdf sha256[:12]`` 派生出的子目录。"""
    import hashlib as _h
    h = _h.sha256()
    with open(source_pdf, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    short = h.hexdigest()[:12]
    if not scratch_root.exists():
        return False
    for run_dir in scratch_root.iterdir():
        if not run_dir.is_dir():
            continue
        if (run_dir / short).exists():
            return True
    return False


# ── AC#10: use_cache=False 走完整解析,不读不写缓存 ──────────────────────────


class TestUseCacheFalse:
    """AC#10 — ``parse_document(file, use_cache=False)`` 不读不写缓存。"""

    def test_use_cache_false_does_not_read_or_write_cache(
        self, tmp_path, monkeypatch,
    ):
        from core import paddleocr_cache as cache_module

        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        monkeypatch.setattr(cache_module, "get_cache_dir", lambda: cache_dir)

        # 预置"假"缓存条目 —— 如果读了缓存,就会拿到 cached 字样而不是真解析
        from core.paddleocr_cache import save_cached

        pdf = _make_blank_pdf(tmp_path / "uncached.pdf", 247)
        save_cached(
            str(pdf),
            {"by_page": [{"page": 0, "text": "FROM CACHE"}], "full_text": "FROM CACHE", "layout": []},
            source="paddleocr",
        )

        # 替换:任何 get_cached 调用都应让测试失败(use_cache=False)
        def _must_not_read(*a, **k):
            raise AssertionError("get_cached must not be called when use_cache=False")

        monkeypatch.setattr(cache_module, "get_cached", _must_not_read)
        monkeypatch.setattr(pd_module, "_paddleocr_call", _fake_paddleocr_call)
        monkeypatch.setattr(pd_module, "_paddleocr_available", lambda: True)

        pr = parse_document(str(pdf), use_cache=False)

        # 走 splitter + fake → 247 页都被解析,**不**是"FROM CACHE"
        assert pr.full_text != "FROM CACHE"
        assert len(pr.by_page) == 247

        # 但也没写新缓存 —— 旧条目还在(覆盖式写入本来就会更新;这里确认
        # save_cached 未被调)
        # 通过检查 source 字段没变来验证:旧条目 source='paddleocr'
        raw = json.loads((cache_dir / _cache_filename(str(pdf))).read_text())
        assert raw["source"] == "paddleocr"


def _cache_filename(file_path: str) -> str:
    import hashlib as _h
    import os as _os
    h = _h.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return f"{h.hexdigest()}_{_os.environ.get('PADDLEOCR_MODEL', 'PaddleOCR-VL-1.6')}.json"


# ── AC#11: reap_scratch 删除超龄子树,保留未满龄 ──────────────────────────────


class TestReapScratch:
    """AC#11 — ``reap_scratch()`` 按 mtime 删除超龄 ``{run_uuid}`` 子树。"""

    def test_reap_deletes_old_run_uuid_subtree(self, tmp_path, monkeypatch):
        root = tmp_path / ".scratch" / "split_pdfs"
        root.mkdir(parents=True)

        old_run = root / "old_run_uuid"
        old_run.mkdir()
        (old_run / "doc_hash").mkdir()
        (old_run / "doc_hash" / "chunk.pdf").write_bytes(b"%PDF-1.4")

        fresh_run = root / "fresh_run_uuid"
        fresh_run.mkdir()
        (fresh_run / "doc_hash").mkdir()

        # 把 old_run 的 mtime 推到超过 TTL
        ttl_hours = splitter.PDF_SPLIT_SCRATCH_TTL_HOURS
        old_mtime = time.time() - (ttl_hours + 1) * 3600
        import os as _os
        _os.utime(old_run, (old_mtime, old_mtime))

        # AUDIT_DATA_DIR 已经被 _per_test_data_dir 指向 tmp_path
        removed = splitter.reap_scratch()

        assert removed == 1
        assert not old_run.exists(), "超龄 run_uuid 应被清掉"
        assert fresh_run.exists(), "未满龄 run_uuid 应保留"

    def test_reap_no_op_when_scratch_missing(self, tmp_path, monkeypatch):
        # AUDIT_DATA_DIR 指向 tmp_path,但 .scratch 不存在
        assert not (tmp_path / ".scratch" / "split_pdfs").exists()
        assert splitter.reap_scratch() == 0

    def test_split_scratch_root_under_data_dir(self, tmp_path):
        """``split_scratch_root()`` 跟着 ``AUDIT_DATA_DIR`` 走(每次重读)。"""
        from core.data_dir import get_data_dir
        from core.pdf_splitter import split_scratch_root

        assert split_scratch_root() == get_data_dir() / ".scratch" / "split_pdfs"


# ── pdf_page_count ──────────────────────────────────────────────────────────


class TestPdfPageCount:
    """``parse_document.pdf_page_count`` —— 分发前预读源 PDF 页数。"""

    def test_returns_page_count_for_real_pdf(self, tmp_path):
        pdf = _make_blank_pdf(tmp_path / "x.pdf", 17)
        assert pd_module.pdf_page_count(str(pdf)) == 17

    def test_returns_none_for_corrupt_pdf(self, tmp_path):
        corrupt = tmp_path / "bad.pdf"
        corrupt.write_bytes(b"not a pdf")
        assert pd_module.pdf_page_count(str(corrupt)) is None

    def test_returns_none_for_non_pdf(self, tmp_path):
        txt = tmp_path / "x.txt"
        txt.write_text("hello")
        # suffix 不是 .pdf → 早退,返回 None
        assert pd_module.pdf_page_count(str(txt)) is None
