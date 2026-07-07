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
    parse_document, _paddleocr_call,
)


# ── ParseResult 数据类 round-trip ───────────────────────────────────────────────


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
    monkeypatch.setattr(cache_module, "CACHE_DIR", cache_dir)

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
    # 同时关掉降级路径
    monkeypatch.setattr(pd_module, "_pdf_fallback", _explode)

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
    monkeypatch.setattr(cache_module, "CACHE_DIR", cache_dir)

    expected = ParseResult(
        by_page=[PageText(page=0, text="paddleocr text")],
        full_text="paddleocr text",
        layout=[],
    )

    monkeypatch.setattr(pd_module, "_paddleocr_available", lambda: True)
    monkeypatch.setattr(pd_module, "_paddleocr_call", lambda p: expected)
    # 关闭降级路径，强制走 PaddleOCR
    monkeypatch.setattr(pd_module, "_pdf_fallback", lambda p: pytest.fail("fallback called"))

    pr = parse_document(str(pdf))
    assert pr.full_text == "paddleocr text"

    # 缓存应当已写：再次读出，验匹配
    from core.paddleocr_cache import get_cached
    cached = get_cached(str(pdf))
    assert cached is not None
    assert cached["full_text"] == "paddleocr text"


def test_pdf_paddleocr_unavailable_falls_back_to_pdfplumber(tmp_path, monkeypatch):
    """PaddleOCR 不可用（无 token）→ 走 _pdf_fallback（pdfplumber）。"""
    pdf = tmp_path / "doc3.pdf"
    pdf.write_bytes(b"%PDF-1.4 dummy3")

    monkeypatch.setattr(pd_module, "_paddleocr_available", lambda: False)
    expected = ParseResult(by_page=[PageText(page=0, text="plumber")], full_text="plumber", layout=[])
    monkeypatch.setattr(pd_module, "_pdf_fallback", lambda p: expected)

    pr = parse_document(str(pdf))
    assert pr.full_text == "plumber"


def test_pdf_paddleocr_failure_falls_back_to_pdfplumber(tmp_path, monkeypatch):
    """PaddleOCR 抛异常 → 走 _pdf_fallback，不让异常传播。"""
    pdf = tmp_path / "doc4.pdf"
    pdf.write_bytes(b"%PDF-1.4 dummy4")

    monkeypatch.setattr(pd_module, "_paddleocr_available", lambda: True)

    def boom(p):
        raise RuntimeError("paddleocr down")

    monkeypatch.setattr(pd_module, "_paddleocr_call", boom)

    fallback = ParseResult(by_page=[PageText(page=0, text="fallback")], full_text="fallback", layout=[])
    monkeypatch.setattr(pd_module, "_pdf_fallback", lambda p: fallback)

    pr = parse_document(str(pdf))
    assert pr.full_text == "fallback"


def test_pdf_use_cache_false_skips_cache_lookup(tmp_path, monkeypatch):
    """use_cache=False → 跳过缓存查找，直接调 PaddleOCR。"""
    pdf = tmp_path / "doc5.pdf"
    pdf.write_bytes(b"%PDF-1.5 dummy5")

    from core import paddleocr_cache as cache_module
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(cache_module, "CACHE_DIR", cache_dir)
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
    monkeypatch.setattr(pd_module, "_paddleocr_call", lambda p: expected)
    monkeypatch.setattr(pd_module, "_pdf_fallback", lambda p: pytest.fail("fallback called"))

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
    """没 token 时直接走降级（paddleocr_available False）— _paddleocr_call 永远不被调用。"""
    monkeypatch.setattr(pd_module, "_paddleocr_api_url", "", raising=False)
    monkeypatch.setattr(pd_module, "_PADDLEOCR_API_URL", "", raising=False)
    monkeypatch.setattr(pd_module, "_PADDLEOCR_API_TOKEN", "", raising=False)
    assert pd_module._paddleocr_available() is False
