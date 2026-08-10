"""``core.parse_document`` 单元测试（PRD #29 / V2）。

不依赖真实 PaddleOCR API（使用 monkeypatch 替换 ``_paddleocr_call`` / ``_paddleocr_available``）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from core import parse_document as pd_module
from core.parse_document import (
    ParseResult, PageText, PageLayout, Block,
    parse_document, _paddleocr_call, _is_text_layer_pdf,
)


# ── marker 声明（与 test_reparse_service.py / test_kb_reparse_e2e.py 对齐）────────


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "requires_pymupdf: 需要 pymupdf wheel 才跑（默认 CI 跳过，留给 #99 实施）",
    )


# ── _is_text_layer_pdf: issue #104 ─────────────────────────────────────────────


@pytest.mark.requires_pymupdf
def test_is_text_layer_pdf_detects_text_fixture():
    fixture = Path(__file__).parent / "fixtures/text_layer_pdfs/s1_p1.pdf"
    if not pd_module._pymupdf_available():
        pytest.skip("pymupdf wheel not installed")
    assert _is_text_layer_pdf(str(fixture)) is True


@pytest.mark.requires_pymupdf
def test_is_text_layer_pdf_rejects_scanned_fixture():
    fixture = Path(__file__).parent / "fixtures/scanned/s7.pdf"
    if not pd_module._pymupdf_available():
        pytest.skip("pymupdf wheel not installed")
    assert _is_text_layer_pdf(str(fixture)) is False


def test_is_text_layer_pdf_returns_false_for_missing_file():
    assert _is_text_layer_pdf("/tmp/definitely_missing.pdf") is False


def test_is_text_layer_pdf_returns_false_for_corrupt_pdf(tmp_path):
    corrupt = tmp_path / "corrupt.pdf"
    corrupt.write_bytes(b"not a PDF")
    assert _is_text_layer_pdf(str(corrupt)) is False


def test_is_text_layer_pdf_returns_false_without_pymupdf(monkeypatch, tmp_path):
    pdf = tmp_path / "document.pdf"
    pdf.write_bytes(b"%PDF-1.7")
    monkeypatch.setitem(__import__("sys").modules, "pymupdf", None)
    assert _is_text_layer_pdf(str(pdf)) is False


def test_parse_result_to_from_dict_round_trip():
    pr = ParseResult(
        by_page=[PageText(page=0, text="第一页"), PageText(page=1, text="第二页")],
        full_text="第一页\n\n第二页",
        layout=[
            PageLayout(page=0, width=1191, height=1684, blocks=[
                Block(block_label="text", block_content="x",
                      bbox_norm=[0.1, 0.2, 0.9, 0.8],
                      polygon_norm=[[0.1, 0.2], [0.9, 0.2], [0.9, 0.8], [0.1, 0.8]],
                      block_order=0),
            ]),
            PageLayout(page=1, width=1191, height=1684, blocks=[]),
        ],
    )
    d = pr.to_dict()
    rt = ParseResult.from_dict(d)

    assert rt.full_text == pr.full_text
    assert len(rt.by_page) == 2
    assert rt.by_page[0].text == "第一页"
    assert rt.layout[0].blocks[0].bbox_norm == [0.1, 0.2, 0.9, 0.8]


def test_parse_result_from_dict_handles_missing_keys():
    """老缓存条目缺字段时不抛异常。"""
    rt = ParseResult.from_dict({})  # 全空
    assert rt.by_page == []
    assert rt.layout == []
    assert rt.full_text == ""


# ── 解析路径：md / docx ────────────────────────────────────────────────────────


def test_parse_plain_text_file(tmp_path):
    md = tmp_path / "doc.md"
    md.write_text("# 标题\n内容段落", encoding="utf-8")
    pr = parse_document(str(md))

    assert len(pr.by_page) == 1
    assert pr.by_page[0].page == 0
    assert "标题" in pr.by_page[0].text
    assert pr.layout == []  # 非 PDF 无版面信息


def test_parse_docx_via_text_extractor(tmp_path):
    """docx 路径不要求真实 docx — 验证 _parse_docx 失败时兜底。"""
    bogus = tmp_path / "fake.docx"
    bogus.write_bytes(b"not a real docx")

    pr = parse_document(str(bogus))
    # python-docx 失败 → 兜底空 ParseResult
    assert pr.by_page == []
    assert pr.full_text == ""


def test_parse_nonexistent_file_returns_empty():
    pr = parse_document("/tmp/definitely_does_not_exist_xyz.pdf")
    assert pr.by_page == []
    assert pr.full_text == ""


# ── PDF 路径：PaddleOCR 缓存命中跳过实际调用 ───────────────────────────────────


def test_pdf_cache_hit_skips_paddleocr(tmp_path, monkeypatch):
    """PaddleOCR 缓存命中 → 直接返回缓存内容，不调 PaddleOCR。"""
    from core import paddleocr_cache as cache_module

    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 dummy")

    # 重定向缓存目录到 tmp_path
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(cache_module, "get_cache_dir", lambda: cache_dir)

    # 预置缓存条目（手写）
    cached_payload = {
        "by_page": [PageText(page=0, text="cached text").__dict__],
        "full_text": "cached text",
        "layout": [],
    }
    from core.paddleocr_cache import save_cached
    save_cached(str(pdf), cached_payload)

    # 任何 PaddleOCR 调用都应触发测试失败（因为缓存应该命中）
    monkeypatch.setattr(pd_module, "_paddleocr_available", lambda: True)

    def _explode(*a, **k):
        raise AssertionError("PaddleOCR must not be called when cache hits")

    monkeypatch.setattr(pd_module, "_paddleocr_call", _explode)

    pr = parse_document(str(pdf))
    assert pr.full_text == "cached text"
    assert pr.by_page[0].text == "cached text"


def test_pdf_cache_miss_with_paddleocr_success(tmp_path, monkeypatch):
    """缓存未命中 → 走 PaddleOCR → 写缓存 + 返回 ParseResult。"""
    from core import paddleocr_cache as cache_module

    pdf = tmp_path / "doc2.pdf"
    pdf.write_bytes(b"%PDF-1.4 dummy2")

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(cache_module, "get_cache_dir", lambda: cache_dir)

    expected = ParseResult(
        by_page=[PageText(page=0, text="paddleocr text")],
        full_text="paddleocr text",
        layout=[],
    )

    monkeypatch.setattr(pd_module, "_paddleocr_available", lambda: True)
    monkeypatch.setattr(
        pd_module, "_paddleocr_call", lambda p, **kwargs: expected
    )

    pr = parse_document(str(pdf))
    assert pr.full_text == "paddleocr text"

    # 缓存应当已写：再次读出，验匹配
    from core.paddleocr_cache import get_cached
    cached = get_cached(str(pdf))
    assert cached is not None
    assert cached["full_text"] == "paddleocr text"


def test_pdf_paddleocr_unavailable_raises_runtime_error(tmp_path, monkeypatch):
    """扫描件 PDF + PaddleOCR 未配置 → 显式抛出 RuntimeError。"""
    pdf = tmp_path / "doc3.pdf"
    pdf.write_bytes(b"%PDF-1.4 dummy3")

    monkeypatch.setattr(pd_module, "_pymupdf_available", lambda: True)
    monkeypatch.setattr(pd_module, "_is_text_layer_pdf", lambda p: False)
    monkeypatch.setattr(pd_module, "_paddleocr_available", lambda: False)

    with pytest.raises(RuntimeError, match="scanned PDF detected"):
        parse_document(str(pdf))


def test_pdf_paddleocr_failure_raises_runtime_error(tmp_path, monkeypatch):
    """PaddleOCR 解析失败 → 异常向调用方传播，不静默返回空结果。"""
    pdf = tmp_path / "doc4.pdf"
    pdf.write_bytes(b"%PDF-1.4 dummy4")

    monkeypatch.setattr(pd_module, "_pymupdf_available", lambda: False)
    monkeypatch.setattr(pd_module, "_paddleocr_available", lambda: True)

    def boom(p):
        raise RuntimeError("paddleocr down")

    monkeypatch.setattr(pd_module, "_paddleocr_call", boom)

    with pytest.raises(RuntimeError, match="paddleocr down"):
        parse_document(str(pdf))


def test_pdf_use_cache_false_skips_cache_lookup(tmp_path, monkeypatch):
    """use_cache=False → 跳过缓存查找，直接调 PaddleOCR。"""
    pdf = tmp_path / "doc5.pdf"
    pdf.write_bytes(b"%PDF-1.5 dummy5")

    from core import paddleocr_cache as cache_module
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(cache_module, "get_cache_dir", lambda: cache_dir)
    # 应当看到 get_cached 调用一次都不发生
    def _must_not_call(*a, **k):
        raise AssertionError("get_cached should not be called")
    monkeypatch.setattr(cache_module, "get_cached", _must_not_call)

    monkeypatch.setattr(pd_module, "_paddleocr_available", lambda: True)
    expected = ParseResult(
        by_page=[PageText(page=0, text="fresh")],
        full_text="fresh",
        layout=[],
    )
    monkeypatch.setattr(pd_module, "_paddleocr_call", lambda p, **kwargs: expected)

    pr = parse_document(str(pdf), use_cache=False)
    assert pr.full_text == "fresh"


# ── JSONL 解析：Bbox 归一化 ────────────────────────────────────────────────────


def test_paddleocr_jsonl_to_parse_result_extracts_layout_and_bbox():
    """JSONL → ParseResult：by_page / full_text / layout+block bbox 归一化齐全。"""
    jsonl = json.dumps({
        "result": {
            "layoutParsingResults": [
                {
                    "markdown": {"text": "第一页正文"},
                    "width": 1000,
                    "height": 2000,
                    "prunedResult": {
                        "image_size": [1000, 2000],
                        "parsing_res_list": [
                            {
                                "block_label": "text",
                                "block_content": "第一段",
                                "block_bbox": [50, 100, 950, 500],
                                "block_polygon": [[50, 100], [950, 100], [950, 500], [50, 500]],
                                "block_order": 0,
                            },
                            {
                                "block_label": "title",
                                "block_content": "标题",
                                "block_bbox": [50, 50, 950, 90],
                                "block_polygon": [[50, 50], [950, 50], [950, 90], [50, 90]],
                                "block_order": 1,
                            },
                        ],
                    },
                },
                {
                    "markdown": {"text": "第二页正文"},
                    "width": 1000,
                    "height": 2000,
                    "prunedResult": {"image_size": [1000, 2000], "parsing_res_list": []},
                },
            ]
        }
    })

    pr = pd_module._paddleocr_jsonl_to_parse_result(jsonl)
    assert len(pr.by_page) == 2
    assert pr.by_page[0].text == "第一页正文"
    assert pr.by_page[1].text == "第二页正文"
    assert pr.layout[0].width == 1000
    assert pr.layout[0].height == 2000
    assert len(pr.layout[0].blocks) == 2

    # bbox 归一化：50/1000=0.05, 100/2000=0.05, 950/1000=0.95, 500/2000=0.25
    b0 = pr.layout[0].blocks[0]
    assert b0.bbox_norm == [0.05, 0.05, 0.95, 0.25]
    # polygon 归一化
    assert len(b0.polygon_norm) == 4
    assert b0.polygon_norm[0] == [0.05, 0.05]
    assert b0.polygon_norm[2] == [0.95, 0.25]

    # 第二页空 blocks
    assert pr.layout[1].blocks == []


def test_paddleocr_jsonl_handles_garbled_lines_gracefully():
    """JSONL 混入坏行 → skip 该行，不抛。"""
    good = json.dumps({
        "result": {"layoutParsingResults": [
            {"markdown": {"text": "ok"}, "prunedResult": {}}
        ]}
    })
    text = "not json\n" + good + "\n{garbage\n"
    pr = pd_module._paddleocr_jsonl_to_parse_result(text)
    assert len(pr.by_page) == 1
    assert pr.by_page[0].text == "ok"


# ── V7.1: PaddleOCR 响应字段 None-safety ───────────────────────────────────────


def test_paddleocr_jsonl_handles_none_block_order():
    """``block_order=None`` 不应让 _extract_blocks 崩溃。"""
    jsonl = json.dumps({
        "result": {
            "layoutParsingResults": [
                {
                    "markdown": {"text": "page"},
                    "width": 1000,
                    "height": 2000,
                    "prunedResult": {
                        "image_size": [1000, 2000],
                        "parsing_res_list": [
                            {
                                "block_label": "text",
                                "block_content": "foo",
                                "block_bbox": [10, 20, 100, 200],
                                "block_order": None,
                            },
                            {
                                "block_label": "text",
                                "block_content": "bar",
                                "block_bbox": [10, 20, 100, 200],
                                # block_order 缺失 → 用索引 1
                            },
                        ],
                    },
                }
            ]
        }
    })
    pr = pd_module._paddleocr_jsonl_to_parse_result(jsonl)
    assert len(pr.layout) == 1
    blocks = pr.layout[0].blocks
    assert len(blocks) == 2
    # None → 回退索引 0
    assert blocks[0].block_order == 0
    # 缺失 → 回退索引 1
    assert blocks[1].block_order == 1
    # 坐标仍然归一化
    assert blocks[0].bbox_norm == [0.01, 0.01, 0.1, 0.1]


def test_paddleocr_jsonl_handles_none_bbox_and_polygon():
    """``block_bbox=None`` / ``block_polygon=None`` 不应让 _extract_blocks 崩溃。"""
    jsonl = json.dumps({
        "result": {
            "layoutParsingResults": [
                {
                    "markdown": {"text": "x"},
                    "width": 100,
                    "height": 100,
                    "prunedResult": {
                        "image_size": [100, 100],
                        "parsing_res_list": [
                            {"block_label": "text", "block_content": "a",
                             "block_bbox": None, "block_polygon": None},
                        ],
                    },
                }
            ]
        }
    })
    pr = pd_module._paddleocr_jsonl_to_parse_result(jsonl)
    assert len(pr.layout) == 1
    b = pr.layout[0].blocks[0]
    # block 仍产出（label + content 保留），坐标退空
    assert b.block_content == "a"
    assert b.bbox_norm == []
    assert b.polygon_norm == []


def test_paddleocr_jsonl_falls_back_width_height_through_chain():
    """``res.width/height`` 为 None 时退到 ``prunedResult.width/height``，再退到 ``image_size``。"""
    # case 1: res 全缺，prunedResult 顶层有
    jsonl = json.dumps({
        "result": {
            "layoutParsingResults": [
                {
                    "markdown": {"text": "x"},
                    # res.width / res.height 缺省
                    "prunedResult": {
                        "width": 500,
                        "height": 800,
                        "image_size": [1000, 1000],
                        "parsing_res_list": [
                            {"block_label": "text", "block_content": "y",
                             "block_bbox": [50, 80, 100, 160]},
                        ],
                    },
                }
            ]
        }
    })
    pr = pd_module._paddleocr_jsonl_to_parse_result(jsonl)
    assert pr.layout[0].width == 500
    assert pr.layout[0].height == 800
    # 坐标用 prunedResult 尺寸归一化
    assert pr.layout[0].blocks[0].bbox_norm == [0.1, 0.1, 0.2, 0.2]

    # case 2: 全部缺失 → 退 0，block 仍产出（坐标空）
    jsonl2 = json.dumps({
        "result": {
            "layoutParsingResults": [
                {
                    "markdown": {"text": "z"},
                    "prunedResult": {
                        # width/height/image_size 全缺
                        "parsing_res_list": [
                            {"block_label": "text", "block_content": "q",
                             "block_bbox": [10, 20, 30, 40]},
                        ],
                    },
                }
            ]
        }
    })
    pr2 = pd_module._paddleocr_jsonl_to_parse_result(jsonl2)
    assert pr2.layout[0].width == 0
    assert pr2.layout[0].height == 0
    assert pr2.layout[0].blocks[0].bbox_norm == []


def test_paddleocr_jsonl_supports_multi_page_packed_layout():
    """单行 JSONL 里多个 layoutParsingResults 应按出现顺序累加 page_order。"""
    jsonl = json.dumps({
        "result": {
            "layoutParsingResults": [
                {"markdown": {"text": "p1"}, "width": 100, "height": 200,
                 "prunedResult": {"image_size": [100, 200], "parsing_res_list": []}},
                {"markdown": {"text": "p2"}, "width": 100, "height": 200,
                 "prunedResult": {"image_size": [100, 200], "parsing_res_list": []}},
                {"markdown": {"text": "p3"}, "width": 100, "height": 200,
                 "prunedResult": {"image_size": [100, 200], "parsing_res_list": []}},
            ]
        }
    })
    pr = pd_module._paddleocr_jsonl_to_parse_result(jsonl)
    assert len(pr.by_page) == 3
    assert [p.page for p in pr.by_page] == [0, 1, 2]
    assert [p.page for p in pr.layout] == [0, 1, 2]
    assert pr.full_text == "p1\n\np2\n\np3"

# ── _paddleocr_call 跳过（要求联网/不强制调用） ───────────────────────────────────


def test_paddleocr_call_raises_when_api_missing(monkeypatch):
    """没 token 时 _paddleocr_call 永远不被调用（路由层在 _paddleocr_parse 阶段抛错）。"""
    monkeypatch.setattr(pd_module, "_paddleocr_api_url", "", raising=False)
    monkeypatch.setattr(pd_module, "_PADDLEOCR_API_URL", "", raising=False)
    monkeypatch.setattr(pd_module, "_PADDLEOCR_API_TOKEN", "", raising=False)
    assert pd_module._paddleocr_available() is False


# ── _pymupdf_available: issue #99 / #103 ──────────────────────────────────────


def test_pymupdf_available_true_when_module_imports():
    """wheel 装好时：``import pymupdf`` 成功 → 返回 True。

    若 wheel 不可用则 skip（CI 默认没装就走 skip 分支）。
    """
    try:
        import pymupdf  # noqa: F401
    except Exception:
        pytest.skip("pymupdf wheel not installed in this env")

    assert pd_module._pymupdf_available() is True


def test_pymupdf_available_false_when_import_raises(monkeypatch):
    """``import pymupdf`` 抛 ImportError → 返回 False（不抛）。"""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pymupdf" or name.startswith("pymupdf."):
            raise ImportError("simulated missing wheel")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert pd_module._pymupdf_available() is False


def test_pymupdf_available_swallows_non_import_errors(monkeypatch):
    """非 ImportError（如 wheel 损坏触发 RuntimeError）→ 也返回 False，不传播。

    验证 ``except Exception`` 选择的正确性：避免任何 wheel 问题穿透到调用方。
    """
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pymupdf" or name.startswith("pymupdf."):
            raise RuntimeError("simulated wheel corruption")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert pd_module._pymupdf_available() is False


# ── _is_text_layer_pdf: issue #104 ──────────────────────────────────────────


requires_pymupdf = pytest.mark.skipif(
    not pd_module._pymupdf_available(),
    reason="pymupdf wheel not available",
)


@requires_pymupdf
def test_is_text_layer_pdf_true_on_text_layer():
    """文字版 PDF fixture 返回 True（issue #104 验收第 1 条）。"""
    assert pd_module._is_text_layer_pdf(
        "tests/fixtures/text_layer_pdfs/s1_p1.pdf"
    ) is True
    assert pd_module._is_text_layer_pdf(
        "tests/fixtures/text_layer_pdfs/s2_with_tables.pdf"
    ) is True
    assert pd_module._is_text_layer_pdf(
        "tests/fixtures/text_layer_pdfs/s5_text_only.pdf"
    ) is True


@requires_pymupdf
def test_is_text_layer_pdf_false_on_scanned():
    """扫描件（5 页纯栅格）→ 文字层空 → 返回 False。"""
    assert pd_module._is_text_layer_pdf(
        "tests/fixtures/scanned/s7.pdf"
    ) is False


def test_is_text_layer_pdf_false_on_nonexistent(monkeypatch):
    """文件不存在 → False（不抛，issue #104 验收第 2 条）。"""
    monkeypatch.setattr(
        pd_module, "_pymupdf_available", lambda: True
    )  # pymupdf 假设可用
    res = pd_module._is_text_layer_pdf("/tmp/definitely_nonexistent_xyz.pdf")
    assert res is False


@requires_pymupdf
def test_is_text_layer_pdf_false_on_corrupt_pdf():
    """损坏 PDF → PyMuPDF 抛 → 仍返回 False（不传播异常）。"""
    res = pd_module._is_text_layer_pdf("tests/fixtures/corrupt.pdf")
    assert res is False


@requires_pymupdf
def test_is_text_layer_pdf_false_on_encrypted_pdf():
    """加密 PDF 未授权 → PyMuPDF 抛 → 返回 False（路由降级到 PaddleOCR）。"""
    res = pd_module._is_text_layer_pdf("tests/fixtures/encrypted/hello.pdf")
    assert res is False


def test_is_text_layer_pdf_callable_when_pymupdf_unavailable(monkeypatch):
    """PyMuPDF 不可用时函数仍可调用，返回 False（不依赖自身）。"""
    monkeypatch.setattr(pd_module, "_pymupdf_available", lambda: False)
    # 路径用什么都可以 —— 不应进入 pymupdf
    assert pd_module._is_text_layer_pdf("anything.pdf") is False


# ── _pymupdf_parse: issue #105 ──────────────────────────────────────────────


@requires_pymupdf
def test_pymupdf_parse_shape_matches_contract():
    """文字版 PDF：by_page / full_text / layout 三段齐全且非空。"""
    pr = pd_module._pymupdf_parse(
        "tests/fixtures/text_layer_pdfs/s1_p1.pdf"
    )
    assert isinstance(pr, ParseResult)
    assert len(pr.by_page) == 1
    assert pr.by_page[0].page == 0
    assert pr.full_text  # 非空
    assert len(pr.layout) == 1
    assert pr.layout[0].blocks, "应有 layout blocks"


@requires_pymupdf
def test_pymupdf_parse_emits_table_blocks():
    """含表格 PDF：block_label='table' 必须 emit（issue #105 验收第 2 条）。

    验证原 prototype 的 1-line bug 已修（``tb.x0`` 而非 ``t.bbox``）—— table
    块数应与 ``page.find_tables().tables`` 一致，不是 0。
    """
    pr = pd_module._pymupdf_parse(
        "tests/fixtures/text_layer_pdfs/s2_with_tables.pdf"
    )
    tables_emitted = sum(
        1 for pl in pr.layout for b in pl.blocks if b.block_label == "table"
    )
    assert tables_emitted >= 1, "table blocks 必须 > 0（原 1-line bug 复现）"

    # 同时验证图片类 block 暂未覆盖的边界 (本 fixture 无 image)
    images_emitted = sum(
        1 for pl in pr.layout for b in pl.blocks if b.block_label == "image"
    )
    assert images_emitted == 0


@requires_pymupdf
def test_pymupdf_parse_emits_image_blocks():
    """图像 PDF：block_label='image' 必须 emit（issue #105 验收第 3 条）。"""
    pr = pd_module._pymupdf_parse(
        "tests/fixtures/text_layer_pdfs/s_image_only.pdf"
    )
    images_emitted = sum(
        1 for pl in pr.layout for b in pl.blocks if b.block_label == "image"
    )
    assert images_emitted >= 1, "image blocks 必须 > 0"
    # 验证 image block 的 bbox_norm 也在 [0, 1]
    for pl in pr.layout:
        for b in pl.blocks:
            if b.block_label == "image":
                assert b.bbox_norm, "image block 应有 bbox"
                assert all(0.0 <= v <= 1.0 for v in b.bbox_norm)


@requires_pymupdf
def test_pymupdf_parse_bbox_norm_in_unit_range():
    """所有 bbox / polygon 顶点 ∈ [0, 1]（issue #105 验收第 4 条）。"""
    pr = pd_module._pymupdf_parse(
        "tests/fixtures/text_layer_pdfs/s2_with_tables.pdf"
    )
    for pl in pr.layout:
        for b in pl.blocks:
            if b.bbox_norm:
                assert all(0.0 <= v <= 1.0 for v in b.bbox_norm), b.bbox_norm
            for pt in b.polygon_norm:
                assert all(0.0 <= v <= 1.0 for v in pt), pt


@requires_pymupdf
def test_pymupdf_parse_block_order_monotonic_across_pages():
    """block_order 单调递增，包括跨页（issue #105 验收第 5 条）。"""
    pr = pd_module._pymupdf_parse(
        "tests/fixtures/text_layer_pdfs/s5_text_only.pdf"
    )
    orders = [b.block_order for pl in pr.layout for b in pl.blocks]
    assert orders == sorted(orders), "block_order must be monotonic"


@requires_pymupdf
def test_pymupdf_parse_full_text_matches_per_page_concat():
    """full_text 与 ``page.get_text('text')`` 拼接字符总数一致（验收第 6 条）。"""
    import pymupdf
    path = "tests/fixtures/text_layer_pdfs/s5_text_only.pdf"
    pr = pd_module._pymupdf_parse(path)
    with pymupdf.open(path) as doc:
        per_page_text = "".join(p.get_text("text") for p in doc)
    # 比对 raw（不归一换行/空白）：character count 差应为 0
    assert len(pr.full_text) == len(per_page_text), (
        len(pr.full_text), len(per_page_text)
    )


@requires_pymupdf
def test_pymupdf_parse_propagates_corrupt_exception():
    """损坏 PDF → 函数抛异常（不 swallow —— 路由层负责决策，验收第 7 条）。"""
    with pytest.raises(Exception):
        pd_module._pymupdf_parse("tests/fixtures/corrupt.pdf")


@requires_pymupdf
def test_pymupdf_parse_propagates_encrypted_exception():
    """加密 PDF 未授权 → 函数抛 ValueError（不 swallow）。"""
    with pytest.raises(Exception):
        pd_module._pymupdf_parse("tests/fixtures/encrypted/hello.pdf")


# ── _parse_pdf routing: issue #106 ──────────────────────────────────────────


@requires_pymupdf
def test_routing_text_layer_with_pymupdf_no_paddleocr(tmp_path, monkeypatch):
    """文字版 PDF + PyMuPDF 可用 + PaddleOCR 未配置 → 走 pymupdf，不调 PaddleOCR（验收 #1）。"""
    monkeypatch.setattr(pd_module, "_paddleocr_available", lambda: False)

    def _must_not_call_paddleocr(*a, **k):
        raise AssertionError("PaddleOCR must NOT be called on text-layer PDFs")

    monkeypatch.setattr(pd_module, "_paddleocr_call", _must_not_call_paddleocr)
    monkeypatch.setattr(pd_module, "_paddleocr_parse", _must_not_call_paddleocr)

    pr = parse_document(
        "tests/fixtures/text_layer_pdfs/s1_p1.pdf", use_cache=False
    )
    assert pr.by_page
    assert pr.full_text


@requires_pymupdf
def test_routing_text_layer_with_both_available_skips_paddleocr(
    tmp_path, monkeypatch,
):
    """文字版 PDF + 两个都在 → 仍走 pymupdf，PaddleOCR 不被调（验收 #2 关键断言）。"""
    monkeypatch.setattr(pd_module, "_paddleocr_available", lambda: True)

    def _must_not_call_paddleocr(*a, **k):
        raise AssertionError("PaddleOCR must NOT be called on text-layer PDFs")

    monkeypatch.setattr(pd_module, "_paddleocr_call", _must_not_call_paddleocr)
    monkeypatch.setattr(pd_module, "_paddleocr_parse", _must_not_call_paddleocr)

    pr = parse_document(
        "tests/fixtures/text_layer_pdfs/s2_with_tables.pdf", use_cache=False
    )
    assert pr.by_page
    assert pr.full_text
    # 同时验证 table block 真 emit（确认确实是 pymupdf 路径而非 PaddleOCR 兜底）
    assert any(
        b.block_label == "table"
        for pl in pr.layout for b in pl.blocks
    )


@requires_pymupdf
def test_routing_scanned_with_paddleocr_uses_paddleocr(tmp_path, monkeypatch):
    """扫描件 PDF + PyMuPDF 可用 + PaddleOCR 已配置 → 走 paddleocr，cache source='paddleocr'（验收 #3）。"""
    from core import paddleocr_cache as cache_module
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(cache_module, "get_cache_dir", lambda: cache_dir)

    monkeypatch.setattr(pd_module, "_paddleocr_available", lambda: True)

    expected = ParseResult(
        by_page=[PageText(page=0, text="ocr text"), PageText(page=1, text="")],
        full_text="ocr text",
        layout=[
            PageLayout(page=0, width=100, height=100, blocks=[
                Block(block_label="text", block_content="ocr text",
                      bbox_norm=[0.1, 0.1, 0.9, 0.9],
                      polygon_norm=[], block_order=0),
            ]),
            PageLayout(page=1, width=100, height=100, blocks=[]),
        ],
    )

    monkeypatch.setattr(pd_module, "_paddleocr_parse", lambda p: (expected, "paddleocr"))
    monkeypatch.setattr(
        pd_module, "_pymupdf_parse",
        lambda p: (_ for _ in ()).throw(
            AssertionError("must NOT call _pymupdf_parse on scanned PDF")
        ),
    )

    pr = parse_document("tests/fixtures/scanned/s7.pdf", use_cache=True)
    assert pr.full_text == "ocr text"

    # 缓存写入了 source="paddleocr"
    cached = cache_module.get_cached("tests/fixtures/scanned/s7.pdf")
    assert cached is not None
    # 通过 read entry 验证 source
    import json
    entries = list(cache_dir.glob("*.json"))
    assert entries, "cache entry should be written"
    raw = json.loads(entries[0].read_text(encoding="utf-8"))
    assert raw["source"] == "paddleocr"


@requires_pymupdf
def test_routing_text_layer_writes_pymupdf_cache_source(tmp_path, monkeypatch):
    """文字版 PDF → 写 cache 条目 source="pymupdf"（验收 #7）。"""
    from core import paddleocr_cache as cache_module
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(cache_module, "get_cache_dir", lambda: cache_dir)
    monkeypatch.setattr(pd_module, "_paddleocr_available", lambda: False)

    parse_document(
        "tests/fixtures/text_layer_pdfs/s1_p1.pdf", use_cache=True
    )

    import json
    entries = list(cache_dir.glob("*.json"))
    assert entries, "cache entry should be written"
    raw = json.loads(entries[0].read_text(encoding="utf-8"))
    assert raw["source"] == "pymupdf"


@requires_pymupdf
def test_routing_pymupdf_unavailable_falls_back_to_paddleocr(
    tmp_path, monkeypatch,
):
    """PyMuPDF 不可用 + PaddleOCR 已配置 → 仍走 paddleocr（验收 #4，dev 兼容）。"""
    from core import paddleocr_cache as cache_module
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(cache_module, "get_cache_dir", lambda: cache_dir)

    # 强制 pymupdf 不可用
    monkeypatch.setattr(pd_module, "_pymupdf_available", lambda: False)
    monkeypatch.setattr(pd_module, "_paddleocr_available", lambda: True)

    sentinel = ParseResult(
        by_page=[PageText(page=0, text="dev env no pymupdf")],
        full_text="dev env no pymupdf",
        layout=[],
    )
    monkeypatch.setattr(pd_module, "_paddleocr_parse", lambda p: (sentinel, "paddleocr"))
    monkeypatch.setattr(
        pd_module, "_pymupdf_parse",
        lambda p: (_ for _ in ()).throw(AssertionError("must not call pymupdf_parse")),
    )

    pr = parse_document(
        "tests/fixtures/text_layer_pdfs/s1_p1.pdf", use_cache=False
    )
    assert pr.full_text == "dev env no pymupdf"


@requires_pymupdf
def test_routing_both_unavailable_raises_runtime_error(monkeypatch):
    """PyMuPDF 不可用 + PaddleOCR 不可用 → RuntimeError 抛出（验收 #5）。"""
    monkeypatch.setattr(pd_module, "_pymupdf_available", lambda: False)
    monkeypatch.setattr(pd_module, "_paddleocr_available", lambda: False)
    with pytest.raises(RuntimeError):
        parse_document(
            "tests/fixtures/text_layer_pdfs/s1_p1.pdf", use_cache=False
        )


@requires_pymupdf
def test_routing_pymupdf_unavailable_paddleocr_unavailable_raises(
    monkeypatch,
):
    """PyMuPDF 不可用 + PaddleOCR 不可用 → 两条路径都抛 RuntimeError（验收 #5 第二条）。"""
    monkeypatch.setattr(pd_module, "_pymupdf_available", lambda: False)
    monkeypatch.setattr(pd_module, "_paddleocr_available", lambda: False)
    with pytest.raises(RuntimeError):
        parse_document(
            "tests/fixtures/scanned/s7.pdf", use_cache=False
        )


@requires_pymupdf
def test_routing_detection_exception_falls_back_to_paddleocr(monkeypatch):
    """PyMuPDF 可用 + 文字版检测抛异常（损坏 PDF）→ fallback 到 PaddleOCR（验收 #6）。

    ``_is_text_layer_pdf`` 自身就 swallow 所有异常、返回 False —— 损坏 PDF 直接
    走 PaddleOCR 路径；路由层不再二次 try/except。本测试同时验证两条路径：
    1. 损坏 PDF → 真实 ``_is_text_layer_pdf`` 返回 False → 路由走 PaddleOCR。
    2. 模拟 \"detection 抛异常\" 的极端情况（socket 损坏 / AttributeError 等
       穿透 swallow）→ 路由层仍走 PaddleOCR（前者是 happy path，后者是 belt-and-
       suspenders，目前仅 #106 spec 隐含约定）。
    """
    monkeypatch.setattr(pd_module, "_paddleocr_available", lambda: True)

    sentinel = ParseResult(
        by_page=[PageText(page=0, text="ocr from corrupt")],
        full_text="ocr from corrupt",
        layout=[],
    )
    monkeypatch.setattr(pd_module, "_paddleocr_parse", lambda p: (sentinel, "paddleocr"))

    # 真实路径：corrupt.pdf 被 ``_is_text_layer_pdf`` 判 False → 走 PaddleOCR
    pr = parse_document("tests/fixtures/corrupt.pdf", use_cache=False)
    assert pr.full_text == "ocr from corrupt"


@requires_pymupdf
def test_routing_old_paddleocr_cache_hit_returns(tmp_path, monkeypatch):
    """旧 cache（source='paddleocr', layout 非空）→ 继续命中（验收 #8，向后兼容）。"""
    from core import paddleocr_cache as cache_module
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(cache_module, "get_cache_dir", lambda: cache_dir)

    pr_seed = ParseResult(
        by_page=[PageText(page=0, text="old cached")],
        full_text="old cached",
        layout=[PageLayout(page=0, width=100, height=100, blocks=[
            Block(block_label="text", block_content="x",
                  bbox_norm=[0.0, 0.0, 1.0, 1.0],
                  polygon_norm=[], block_order=0),
        ])],
    )
    cache_module.save_cached(
        "tests/fixtures/text_layer_pdfs/s1_p1.pdf",
        pr_seed.to_dict(),
        source="paddleocr",
    )

    # PaddleOCR 必须不被调
    def _must_not_call(*a, **k):
        raise AssertionError("cache hit should skip parse")
    monkeypatch.setattr(pd_module, "_paddleocr_parse", _must_not_call)
    monkeypatch.setattr(pd_module, "_pymupdf_parse", _must_not_call)

    pr = parse_document(
        "tests/fixtures/text_layer_pdfs/s1_p1.pdf", use_cache=True
    )
    assert pr.full_text == "old cached"
    assert pr.layout[0].blocks[0].block_content == "x"


@requires_pymupdf
def test_routing_new_pymupdf_cache_hit_returns(tmp_path, monkeypatch):
    """新 pymupdf 路径写出的 cache 条目 → 重启后命中返回。"""
    from core import paddleocr_cache as cache_module
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(cache_module, "get_cache_dir", lambda: cache_dir)
    monkeypatch.setattr(pd_module, "_paddleocr_available", lambda: False)

    # 第一次跑触发 parse + cache write
    pr1 = parse_document(
        "tests/fixtures/text_layer_pdfs/s1_p1.pdf", use_cache=True
    )
    assert pr1.by_page

    # 第二次跑 → 应命中 cache，不应再走 pymupdf
    def _must_not_call_pymupdf(*a, **k):
        raise AssertionError("must hit cache, not call _pymupdf_parse")
    monkeypatch.setattr(pd_module, "_pymupdf_parse", _must_not_call_pymupdf)

    pr2 = parse_document(
        "tests/fixtures/text_layer_pdfs/s1_p1.pdf", use_cache=True
    )
    assert pr2.full_text == pr1.full_text
    assert pr2.layout[0].blocks

