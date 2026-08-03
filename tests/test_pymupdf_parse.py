"""``core.parse_document._pymupdf_parse`` unit tests (issue #99 / #105).

Acceptance criteria (issue #105):
- 文字版 PDF fixture:``by_page`` / ``full_text`` / ``layout`` 三段齐全且非空
- 含表格 PDF fixture:``block_label == "table"`` 的 block 数量与 PyMuPDF
  ``find_tables()`` 输出一致(**不是 0** —— 1-line bug 修了)
- 图像 PDF fixture:``block_label == "image"`` 的 block 有 emit
- ``bbox_norm`` 全部在 [0, 1];``polygon_norm`` 顶点也在 [0, 1]
- ``block_order`` 单调递增(PDF 物理页内、跨页)
- ``full_text`` 与 ``page.get_text("text")`` 拼接结果一致
- 损坏 / 加密 PDF 时函数抛异常(**不 swallow**)

Fixture 生成:为避免将二进制 PDF 提交到仓库,所有 fixture PDF 用 PyMuPDF
在 ``tmp_path`` 下即时生成(每个 < 50 ms),通过 ``text_layer_pdfs`` pytest
fixture 暴露给测试。
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import Optional

import pytest


# ── PyMuPDF 探测(无 wheel 即 skip 整文件)─────────────────────────────────────


def pytest_configure(config):
    """注册 ``requires_pymupdf`` marker(与 test_parse_document.py 协同):

    本文件 load 时复盖任何其他 conftest 注册相同的 marker——``addinivalue_line``
    是幂等的,只发出 'already registered' 警告而不破坏行为。
    """
    config.addinivalue_line(
        "markers",
        "requires_pymupdf: 需要 pymupdf wheel 才跑(默认 CI 跳过)",
    )


try:
    import pymupdf  # type: ignore[import-not-found]
    _HAVE_PYMUPDF = True
except Exception:
    pymupdf = None  # type: ignore[assignment]
    _HAVE_PYMUPDF = False


pytestmark = [
    pytest.mark.skipif(
        not _HAVE_PYMUPDF,
        reason="pymupdf wheel not installed (issue #99 / #105)",
    ),
    pytest.mark.requires_pymupdf,
]


# ── Fixture 生成器(用 PyMuPDF 现写现用,不 commit 二进制)────────────────────────


class TextLayerFixtureSet:
    """Bundle of fixture PDFs (path only)."""

    def __init__(
        self,
        *,
        s2_with_tables: Path,
        text_only: Path,
        image_only: Path,
        corrupt: Path,
        encrypted: Optional[Path],
    ) -> None:
        self.s2_with_tables = s2_with_tables
        self.text_only = text_only
        self.image_only = image_only
        self.corrupt = corrupt
        self.encrypted = encrypted


@pytest.fixture
def text_layer_pdfs(tmp_path: Path) -> TextLayerFixtureSet:
    """在 ``tmp_path/text_layer_pdfs/`` 下即时生成 fixture PDF 集。

    几何:每页 A4 (595 x 842),保证 PyMuPDF ``rect.width/height`` 干净 → 归一化可预测。

    整个模块已通过 ``pytestmark = skipif(not _HAVE_PYMUPDF)`` 在 import 时 skip,
    这里不再二次 skip。
    """
    out = tmp_path / "text_layer_pdfs"
    out.mkdir()

    s2 = _make_s2_with_tables(out / "s2_with_tables.pdf")
    text_only = _make_text_only(out / "text_only.pdf")
    image_only = _make_image_only(out / "image_only.pdf")
    corrupt = _make_corrupt(out / "corrupt.pdf")
    encrypted = _make_encrypted(out / "encrypted.pdf")  # may be None

    return TextLayerFixtureSet(
        s2_with_tables=s2,
        text_only=text_only,
        image_only=image_only,
        corrupt=corrupt,
        encrypted=encrypted,
    )


def _make_s2_with_tables(path: Path) -> Path:
    """3 页文档:每页一张 2x3 表格 + 段落文字。

    PyMuPDF 的 ``find_tables()`` 需要明显的网格线才检测得到——所以用
    ``draw_line`` 画 2x3 网格,然后在每格内插一段文字。
    """
    doc = pymupdf.open()
    for page_idx in range(3):
        page = doc.new_page(width=595, height=842)
        xs = [50, 250, 450]
        ys = [50, 150, 250, 350]
        for x in xs:
            page.draw_line((x, ys[0]), (x, ys[-1]))
        for y in ys:
            page.draw_line((xs[0], y), (xs[-1], y))
        page.insert_text(
            (50, 30),
            f"Science Archive Management - page {page_idx + 1}",
            fontsize=12,
        )
        page.insert_text((50, 800), "End of section.", fontsize=12)
        page.insert_text((60, 100), f"Header A{page_idx}", fontsize=11)
        page.insert_text((260, 100), f"Header B{page_idx}", fontsize=11)
        page.insert_text((60, 200), f"Cell A1.{page_idx}", fontsize=11)
        page.insert_text((260, 200), f"Cell B1.{page_idx}", fontsize=11)
        page.insert_text((60, 300), f"Cell A2.{page_idx}", fontsize=11)
        page.insert_text((260, 300), f"Cell B2.{page_idx}", fontsize=11)

    doc.save(str(path))
    doc.close()
    return path


def _make_text_only(path: Path) -> Path:
    """3 页纯文本 PDF(无表格、无图像)。"""
    doc = pymupdf.open()
    for page_idx in range(3):
        page = doc.new_page(width=595, height=842)
        for line_idx in range(20):
            page.insert_text(
                (50, 50 + line_idx * 18),
                f"Line {line_idx + 1} on page {page_idx + 1}",
                fontsize=11,
            )
    doc.save(str(path))
    doc.close()
    return path


def _png_pixel(r: int, g: int, b: int, a: int = 255) -> bytes:
    """Build a 1x1 RGBA PNG,return raw bytes (避免 commit 二进制 fixture)."""

    def _chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data)
        return (
            struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", crc & 0xFFFFFFFF)
        )

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
    raw = b"\x00" + bytes([r, g, b, a])
    idat = _chunk(b"IDAT", zlib.compress(raw, 9))
    iend = _chunk(b"IEND", b"")
    return sig + ihdr + idat + iend


def _make_image_only(path: Path) -> Path:
    """1 页嵌入一张 1x1 PNG + caption 文字。"""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 50), "Caption above image", fontsize=11)
    img_bytes = _png_pixel(255, 0, 0)
    page.insert_image((50, 100, 250, 300), stream=img_bytes)
    doc.save(str(path))
    doc.close()
    return path


def _make_corrupt(path: Path) -> Path:
    """Not-a-PDF 字节——``pymupdf.open()`` 应抛``FileDataError``或类似异常。"""
    path.write_bytes(b"this is not a valid pdf document garbage bytes")
    return path


def _make_encrypted(path: Path) -> Optional[Path]:
    """AES-256 加密 PDF——``pymupdf.open()`` 在没 password 时访问内容抛异常。

    PyMuPDF 1.28.0 的``open()`` 对加密文件可能不立刻抛——但访问
    ``.get_text()`` / 拿 page 内容时一定会抛。``_pymupdf_parse`` 在
    ``open()`` 之后立刻 ``page.get_text('text')``,所以这里用同样的访问模式
    自检就足够。

    Returns:
        path if generated successfully, None otherwise.
    """
    try:
        doc = pymupdf.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((50, 50), "encrypted content")
        doc.save(
            str(path),
            encryption=pymupdf.PDF_ENCRYPT_AES_256,
            owner_pw="owner",
            user_pw="user",
        )
        doc.close()
        # 自检:无密码访问 page 内容必须抛异常
        try:
            with pymupdf.open(str(path)) as d2:
                d2[0].get_text("text")
        except Exception:
            return path
        # encrypt 没生效 — 生成失败
        return None
    except Exception:
        return None


# ── 导入 SUT ───────────────────────────────────────────────────────────────────


from core.parse_document import _pymupdf_parse, ParseResult  # noqa: E402


# ── 文字版 fixture ────────────────────────────────────────────────────────────


def test_text_only_returns_three_segments(text_layer_pdfs):
    """AC: 文字 PDF 上 by_page / full_text / layout 三段齐全且非空。"""
    result = _pymupdf_parse(str(text_layer_pdfs.text_only))

    assert isinstance(result, ParseResult)
    assert len(result.by_page) == 3
    assert all(p.text for p in result.by_page), "文字 PDF by_page 应非空"
    assert result.full_text, "文字 PDF full_text 应非空"
    assert len(result.layout) == 3
    assert all(pl.blocks for pl in result.layout), "layout 每页应有 blocks"


def test_text_only_text_blocks_have_valid_block_order(text_layer_pdfs):
    """AC: block_order 单调递增(物理页内、跨页)。"""
    result = _pymupdf_parse(str(text_layer_pdfs.text_only))

    orders = [b.block_order for pl in result.layout for b in pl.blocks]
    assert orders == sorted(orders), f"block_order 不是单调递增:{orders}"
    # 跨页继续递增:第 N+1 页第一个 block_order >= 第 N 页最后一个
    for i in range(1, len(result.layout)):
        prev_max = max(
            (b.block_order for b in result.layout[i - 1].blocks), default=-1
        )
        first_cur = (
            result.layout[i].blocks[0].block_order
            if result.layout[i].blocks else prev_max + 1
        )
        assert first_cur >= prev_max, f"跨页 block_order 不连续:page {i}"


def test_text_only_bbox_in_unit_interval(text_layer_pdfs):
    """AC: bbox_norm 全部在 [0, 1]。"""
    result = _pymupdf_parse(str(text_layer_pdfs.text_only))
    for pl in result.layout:
        for b in pl.blocks:
            assert len(b.bbox_norm) == 4
            for v in b.bbox_norm:
                assert 0.0 <= v <= 1.0, f"bbox_norm 越界:{b.bbox_norm}"
            for pt in b.polygon_norm:
                for v in pt:
                    assert 0.0 <= v <= 1.0, f"polygon_norm 越界:{b.polygon_norm}"


def test_text_only_full_text_matches_page_concat(text_layer_pdfs):
    """AC: full_text 与 page.get_text('text') 拼接结果一致(by_page 层面)。"""
    expected_pages: list[str] = []
    with pymupdf.open(str(text_layer_pdfs.text_only)) as doc:
        for page in doc:
            expected_pages.append(page.get_text("text") or "")

    result = _pymupdf_parse(str(text_layer_pdfs.text_only))
    actual_concat = "\n\n".join(p.text for p in result.by_page if p.text)
    expected_concat = "\n\n".join(p for p in expected_pages if p)
    assert actual_concat == expected_concat
    # 同时 by_page 文本不丢字符:逐页 == page.get_text('text')
    for pt, raw in zip(result.by_page, expected_pages):
        assert pt.text == raw


# ── 含表格 fixture ────────────────────────────────────────────────────────────


def test_s2_with_tables_emits_table_blocks_matching_find_tables(text_layer_pdfs):
    """AC: ``block_label == 'table'`` 数量 == PyMuPDF ``find_tables().tables`` 数量
    (**不是 0** —— 1-line bug 修了)。
    """
    result = _pymupdf_parse(str(text_layer_pdfs.s2_with_tables))

    table_count_emitted = sum(
        1 for pl in result.layout for b in pl.blocks if b.block_label == "table"
    )

    expected_table_count = 0
    with pymupdf.open(str(text_layer_pdfs.s2_with_tables)) as doc:
        for page in doc:
            expected_table_count += len(page.find_tables().tables)

    assert table_count_emitted == expected_table_count
    assert expected_table_count > 0, "fixture 设计预期至少检出 1 张表"


def test_s2_with_tables_layout_has_three_pages(text_layer_pdfs):
    """AC: layout 长度 == PDF 物理页数。"""
    result = _pymupdf_parse(str(text_layer_pdfs.s2_with_tables))

    with pymupdf.open(str(text_layer_pdfs.s2_with_tables)) as doc:
        assert len(result.layout) == doc.page_count
        assert doc.page_count == 3


def test_s2_with_tables_table_block_has_cells_in_content(text_layer_pdfs):
    """table block 的 ``block_content`` 应包含 cells 拼接(空 content 是退化)。"""
    result = _pymupdf_parse(str(text_layer_pdfs.s2_with_tables))

    table_blocks = [
        b for pl in result.layout for b in pl.blocks if b.block_label == "table"
    ]
    assert table_blocks
    assert any(b.block_content.strip() for b in table_blocks)


def test_s2_with_tables_table_bbox_is_4tuple_normalized(text_layer_pdfs):
    """table block 的 bbox_norm 是 4 元素、全部在 [0, 1]。"""
    result = _pymupdf_parse(str(text_layer_pdfs.s2_with_tables))

    for pl in result.layout:
        for b in pl.blocks:
            if b.block_label == "table":
                assert len(b.bbox_norm) == 4, f"bbox 应为 4-tuple:{b.bbox_norm}"
                for v in b.bbox_norm:
                    assert 0.0 <= v <= 1.0


# ── 图像 PDF fixture ──────────────────────────────────────────────────────────


def test_image_only_emits_image_block(text_layer_pdfs):
    """AC: ``block_label == 'image'`` 的 block 有 emit。"""
    result = _pymupdf_parse(str(text_layer_pdfs.image_only))

    image_blocks = [
        b for pl in result.layout for b in pl.blocks if b.block_label == "image"
    ]
    assert image_blocks, "应在 image_only fixture 上 emit image block"


def test_image_only_image_bbox_is_4tuple_normalized(text_layer_pdfs):
    """image block 的 bbox_norm 是 4 元素、全在 [0, 1]。"""
    result = _pymupdf_parse(str(text_layer_pdfs.image_only))
    for pl in result.layout:
        for b in pl.blocks:
            if b.block_label == "image":
                assert len(b.bbox_norm) == 4
                for v in b.bbox_norm:
                    assert 0.0 <= v <= 1.0


# ── 损坏 / 加密 PDF ───────────────────────────────────────────────────────────


def test_corrupt_pdf_raises_does_not_silently_return_empty(text_layer_pdfs):
    """AC: 损坏 PDF 时抛异常(**不 swallow**)→ 不是 silently 返回空 ParseResult。"""
    with pytest.raises(Exception):
        _pymupdf_parse(str(text_layer_pdfs.corrupt))


def test_encrypted_pdf_raises_without_password(text_layer_pdfs):
    """AC: 加密 PDF 无密码时抛异常(**不 swallow**)。

    当 PyMuPDF 1.28.0 的 ``save(encryption=...)`` 路径不可用时(``fixture.encrypted
    is None``)skip。
    """
    if text_layer_pdfs.encrypted is None:
        pytest.skip("此环境 PyMuPDF 加密写盘路径不可用")

    with pytest.raises(Exception):
        _pymupdf_parse(str(text_layer_pdfs.encrypted))


# ── Round-trip / schema ───────────────────────────────────────────────────────


def test_s2_result_roundtrips_via_to_from_dict(text_layer_pdfs):
    """ParseResult ``to_dict() -> from_dict()`` round-trip,结构不丢。"""
    result = _pymupdf_parse(str(text_layer_pdfs.s2_with_tables))
    d = result.to_dict()
    rt = ParseResult.from_dict(d)

    assert len(rt.by_page) == len(result.by_page)
    assert len(rt.layout) == len(result.layout)
    assert rt.full_text == result.full_text
    for orig, rt_pl in zip(result.layout, rt.layout):
        assert len(orig.blocks) == len(rt_pl.blocks)
        for ob, rb in zip(orig.blocks, rt_pl.blocks):
            assert ob.bbox_norm == rb.bbox_norm
            assert ob.block_order == rb.block_order
            assert ob.block_label == rb.block_label


def test_text_only_result_roundtrips_via_to_from_dict(text_layer_pdfs):
    """text_only 同样 round-trip。"""
    result = _pymupdf_parse(str(text_layer_pdfs.text_only))
    rt = ParseResult.from_dict(result.to_dict())

    assert rt.full_text == result.full_text
    assert [p.text for p in rt.by_page] == [p.text for p in result.by_page]
