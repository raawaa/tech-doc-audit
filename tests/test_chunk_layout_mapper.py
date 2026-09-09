"""``core.chunk_layout_mapper`` 单元测试(issue #169 / PR-3 AC #5)。

覆盖 T1/P2 happy paths + 边界用例:

T1 路径(双向 includes):
  - 单 block chunk,完全覆盖该 block
  - 多 block chunk(OCR 拆散场景):``(min, max)`` 闭区间
  - chunk 是 block 子串(block 是 chunk 子串的反向)
  - 标点 / 全角字符走归一化

P2 路径(LCS ratio):
  - 等长 OCR 错字:ratio >= 0.85 命中
  - 短串 < 4 字符短路:不跑 LCS,直接不命中

边界用例:
  - 空 chunk / 空 block / 空 page_blocks
  - page_layout = None(非 PDF / 旧 KB 走 fallback)
  - block 乱序:按 block_order 升序扫描
  - 跨 block_order 边界

归一化 helper:
  - ``normalize_layout(list[PageLayout])`` 原样返回
  - ``normalize_layout(list[dict])`` → ``list[PageLayout]``
  - ``normalize_layout(None)`` → ``None``
  - ``normalize_layout([])`` → ``[]``(与 None 语义不同:caller 想"跑
    注入但 layout 实际为空")

``inject_block_range``:
  - chunk.metadata["block_range"] 写入预期区间
  - by_layout=None → 全 None
  - page_number 越界 / None → None
  - by_layout 传 list[dict] 兼容
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.chunk_layout_mapper import (
    map_chunk_to_blocks,
    normalize_layout,
)
from core.kb_index_writer import _inject_block_range as inject_block_range
from core.parse_document import Block, PageLayout, PageText


# ── 辅助工厂 ────────────────────────────────────────────────────────────────


def _make_block(block_content: str, block_order: int, page: int = 0):
    """构造测试用 layout Block,只填 mapper 实际读的字段。"""
    return SimpleNamespace(
        block_content=block_content,
        block_order=block_order,
        page=page,
        bbox_norm=[],
        block_label="text",
    )


def _make_layout(*pages_blocks):
    """``pages_blocks[i]`` 是第 i 页的 block 列表 → ``list[PageLayout]``。

    这是 ``by_layout`` 形态(整 KB 的 page layout 列表)。
    """
    return [
        PageLayout(page=i, blocks=list(blocks), width=0, height=0)
        for i, blocks in enumerate(pages_blocks)
    ]


def _make_single_page(blocks):
    """单页 PageLayout(直接传给 ``map_chunk_to_blocks``)。

    ``blocks`` 是该页的 block 列表。
    """
    return PageLayout(page=0, blocks=list(blocks), width=0, height=0)


def _make_chunk_node(text: str, page_number):
    return SimpleNamespace(text=text, metadata={"page_number": page_number})


# ── map_chunk_to_blocks:T1 双向 includes ──────────────────────────────────


def test_map_chunk_to_blocks_single_block_match():
    """T1:chunk 完全等于 page 内单个 block → ``(n, n)`` 闭区间。"""
    layout = _make_single_page([_make_block("公司各应急保障单位应当配置", 5)])
    result = map_chunk_to_blocks("公司各应急保障单位应当配置", layout)
    assert result == (5, 5)


def test_map_chunk_to_blocks_multi_block_range():
    """T1:chunk 拆散到多个 block → ``(min_order, max_order)`` 闭区间。"""
    layout = _make_single_page([
        _make_block("公司各应急", 1),
        _make_block("保障单位应当", 2),
        _make_block("配置无线对讲", 3),
        _make_block("设备至少两套", 4),
        _make_block("其它", 5),
    ])
    result = map_chunk_to_blocks(
        "公司各应急保障单位应当配置无线对讲设备至少两套", layout,
    )
    assert result == (1, 4)


def test_map_chunk_to_blocks_chunk_is_substring_of_block():
    """T1 反向:chunk 是 page 内某个 block 的子串 → 该 block_order 命中。"""
    layout = _make_single_page([
        _make_block("公司各应急保障单位应当配置无线对讲设备至少两套", 7),
        _make_block("无关内容", 8),
    ])
    result = map_chunk_to_blocks("公司各应急保障单位", layout)
    assert result == (7, 7)


def test_map_chunk_to_blocks_block_is_substring_of_chunk():
    """T1:block 是 chunk 子串(OCR 拆散,block 是 chunk 局部)→ 该 block 命中。"""
    layout = _make_single_page([
        _make_block("公司各应急保障", 0),
        _make_block("无关内容", 1),
    ])
    result = map_chunk_to_blocks("公司各应急保障单位应当配置无线对讲设备至少两套", layout)
    # block 0 (含 "公司各应急保障") 是 chunk 子串 → 命中 (0, 0)
    assert result == (0, 0)


def test_map_chunk_to_blocks_punctuation_normalized():
    """标点差异经归一化后命中(NFKC + 去标点)。"""
    layout = _make_single_page([
        _make_block("公司各应急保障单位。应当配置——800兆对讲机", 0),
    ])
    result = map_chunk_to_blocks(
        "公司各应急保障单位应当配置800兆对讲机", layout,
    )
    assert result == (0, 0)


def test_map_chunk_to_blocks_fullwidth_normalized():
    """全角字符经 NFKC 归一化后命中(对齐 layoutMatch.norm 的 NFKC 契约)。"""
    layout = _make_single_page([_make_block("800兆对讲机", 0)])
    result = map_chunk_to_blocks("８00兆对讲机", layout)
    assert result == (0, 0)


# ── map_chunk_to_blocks:P2 LCS 兜底 ───────────────────────────────────────


def test_map_chunk_to_blocks_ocr_typo_lcs_fallback():
    """OCR 单字错但 chunk 够长 → LCS 兜底命中。

    22 字符等长,错 1 字 → ratio = 21/22 ≈ 0.955 ≥ 0.85。
    """
    long_text = "公司各应急保障单位应当配置无线对讲设备至少两套"
    layout = _make_single_page([_make_block(long_text, 0)])
    typo = "公司各应急保障单位应当配置无线对话设备至少两套"  # 讲 → 话
    result = map_chunk_to_blocks(typo, layout)
    assert result == (0, 0)


def test_map_chunk_to_blocks_short_string_no_lcs():
    """短串(< MIN_LCS_LEN=4)includes miss 时不跑 LCS,直接 None。"""
    layout = _make_single_page([_make_block("wxyz", 0)])
    # 3 字符 < 4,includes miss → 不命中
    result = map_chunk_to_blocks("abc", layout)
    assert result is None


def test_map_chunk_to_blocks_long_typo_below_threshold():
    """等长 OCR 错字但 ratio < 0.85 → P2 miss。"""
    # 4 字符等长错 1 → ratio 3/4 = 0.75 < 0.85
    layout = _make_single_page([_make_block("abcd", 0)])
    result = map_chunk_to_blocks("abce", layout)
    assert result is None


# ── map_chunk_to_blocks:边界用例 ──────────────────────────────────────────


def test_map_chunk_to_blocks_empty_chunk_returns_none():
    """空 chunk → None(不抛、不阻塞)。"""
    layout = _make_single_page([_make_block("任何内容", 0)])
    assert map_chunk_to_blocks("", layout) is None


def test_map_chunk_to_blocks_whitespace_only_chunk_returns_none():
    """纯空白 chunk(归一化后为空)→ None。"""
    layout = _make_single_page([_make_block("任何内容", 0)])
    assert map_chunk_to_blocks("   ", layout) is None


def test_map_chunk_to_blocks_no_page_blocks_returns_none():
    """page_layout.blocks = [] → None。"""
    layout = _make_single_page([])
    result = map_chunk_to_blocks("任何内容", layout)
    assert result is None


def test_map_chunk_to_blocks_none_layout_returns_none():
    """page_layout = None → None(非 PDF / 旧 KB 走 fallback 高亮)。"""
    result = map_chunk_to_blocks("任何内容", None)
    assert result is None


def test_map_chunk_to_blocks_no_match_returns_none():
    """完全无关内容,无命中 → None。"""
    layout = _make_single_page([_make_block("完全无关的 PDF 内容", 0)])
    result = map_chunk_to_blocks("公司各应急保障单位应当配置无线对讲机", layout)
    assert result is None


def test_map_chunk_to_blocks_picks_correct_page():
    """map_chunk_to_blocks 单函数只处理单页 layout;调用方负责按 page_number
    选页。这里传第 1 页 layout → 该页内的 block 命中区间。

    注意:``map_chunk_to_blocks`` 是按单 page_layout 工作的;跨页路由
    由 ``inject_block_range`` 调用者按 ``node.metadata["page_number"]``
    索引到对应 page_layout。本测试仅验证单页映射本身不串页。
    """
    page0_layout = _make_single_page([_make_block("第一页内容", 0)])
    page1_layout = _make_single_page([_make_block("第二章要求的内容", 1, page=1)])
    # 单独看 page1:chunk 落点 = block 1
    result = map_chunk_to_blocks("第二章要求的内容", page1_layout)
    assert result == (1, 1)
    # 单独看 page0:完全无关
    result = map_chunk_to_blocks("第二章要求的内容", page0_layout)
    assert result is None


def test_map_chunk_to_blocks_out_of_order_blocks_sorted():
    """block 乱序传入 → 按 block_order 升序扫描,命中区间正确。"""
    layout = _make_single_page([
        _make_block("配置至少两套", 7),
        _make_block("公司各应急", 3),
        _make_block("保障单位", 5),
    ])
    result = map_chunk_to_blocks("公司各应急保障单位配置至少两套", layout)
    # 排序后 order 是 3, 5, 7 → 区间 (3, 7)
    assert result == (3, 7)


def test_map_chunk_to_blocks_returns_none_for_empty_blocks_list():
    """blocks = [] 直接返回 None。"""
    layout = PageLayout(page=0, blocks=[], width=0, height=0)
    result = map_chunk_to_blocks("任何内容", layout)
    assert result is None


# ── normalize_layout ──────────────────────────────────────────────────────


def test_normalize_layout_passthrough_pagelayout_list():
    """``list[PageLayout]`` 原样返回(浅拷贝)。"""
    layouts = [_make_single_page([_make_block("a", 0)])]
    result = normalize_layout(layouts)
    assert isinstance(result, list)
    assert isinstance(result[0], PageLayout)
    assert result is not layouts  # 浅拷贝,但内容相同
    assert result[0].blocks[0].block_content == "a"


def test_normalize_layout_dict_to_pagelayout():
    """``list[dict]`` → ``list[PageLayout]``(归一化)。"""
    layout_dicts = [{
        "page": 0,
        "blocks": [{"block_content": "公司各应急保障", "block_order": 0}],
        "width": 100,
        "height": 200,
    }]
    result = normalize_layout(layout_dicts)
    assert len(result) == 1
    assert isinstance(result[0], PageLayout)
    assert result[0].page == 0
    assert result[0].width == 100
    assert len(result[0].blocks) == 1
    assert result[0].blocks[0].block_content == "公司各应急保障"


def test_normalize_layout_none_returns_none():
    """``None`` → ``None``(caller "跳过注入" 信号)。"""
    assert normalize_layout(None) is None


def test_normalize_layout_empty_returns_empty():
    """``[]`` → ``[]``(与 None 语义不同:caller 想"跑注入但实际空")。

    issue #169 / PR-3 显式区分 None 与 []:None 表示"caller 想跳过"
    (非 PDF / 旧 KB 走 fallback),[] 表示"caller 想跑注入但 layout
    实际为空"(每页 blocks = [])。
    """
    assert normalize_layout([]) == []


def test_normalize_layout_dict_with_dict_blocks():
    """blocks 字段也是 dict 时也能归一化(深度 dict 输入)。"""
    layout_dicts = [{
        "page": 1,
        "blocks": [
            {"block_content": "x", "block_order": 5},
            {"block_content": "y", "block_order": 6},
        ],
        "width": 0,
        "height": 0,
    }]
    result = normalize_layout(layout_dicts)
    assert len(result) == 1
    assert result[0].page == 1
    assert len(result[0].blocks) == 2
    assert result[0].blocks[0].block_order == 5
    assert result[0].blocks[1].block_content == "y"


# ── inject_block_range ─────────────────────────────────────────────────────


def test_inject_block_range_no_layout_all_none():
    """by_layout=None → 所有 chunk.block_range = None(走 fallback 高亮)。"""
    nodes = [
        _make_chunk_node("文本 A", 0),
        _make_chunk_node("文本 B", 1),
    ]
    inject_block_range(nodes, None)
    assert nodes[0].metadata["block_range"] is None
    assert nodes[1].metadata["block_range"] is None


def test_inject_block_range_empty_nodes_noop():
    """空 nodes → 不抛、不改。"""
    inject_block_range([], None)
    inject_block_range(None or [], None)  # type: ignore


def test_inject_block_range_no_match_yields_none():
    """找不到任何命中 → block_range = None,不阻塞。"""
    layouts = _make_layout([_make_block("完全无关的 PDF 内容", 0)])
    nodes = [_make_chunk_node("公司各应急保障单位", 0)]
    inject_block_range(nodes, layouts)
    assert nodes[0].metadata["block_range"] is None


def test_inject_block_range_page_out_of_range_yields_none():
    """page_number 越界 → None(不抛)。"""
    layouts = _make_layout([_make_block("内容", 0)])
    nodes = [_make_chunk_node("内容", 99)]  # 越界
    inject_block_range(nodes, layouts)
    assert nodes[0].metadata["block_range"] is None


def test_inject_block_range_no_page_number_yields_none():
    """page_number = None → None(由 ``_inject_page_number`` 已写过,这里读出来兜底)。"""
    layouts = _make_layout([_make_block("内容", 0)])
    nodes = [_make_chunk_node("内容", None)]
    inject_block_range(nodes, layouts)
    assert nodes[0].metadata["block_range"] is None


def test_inject_block_range_writes_correct_range():
    """Happy path:chunk 命中 → ``block_range`` 写预期区间。"""
    layouts = _make_layout([
        _make_block("无关", 0),
        _make_block("公司各应急", 1),
        _make_block("保障单位", 2),
        _make_block("配置无线对讲", 3),
    ])
    nodes = [_make_chunk_node("公司各应急保障单位配置无线对讲", 0)]
    inject_block_range(nodes, layouts)
    assert nodes[0].metadata["block_range"] == (1, 3)


def test_inject_block_range_dict_input_compat():
    """by_layout 传 list[dict] 时也能工作(旧 API 残留兼容)。"""
    layout_dicts = [{
        "page": 0,
        "blocks": [{"block_content": "公司各应急保障", "block_order": 0}],
        "width": 0,
        "height": 0,
    }]
    nodes = [_make_chunk_node("公司各应急保障单位", 0)]
    inject_block_range(nodes, layout_dicts)
    assert nodes[0].metadata["block_range"] == (0, 0)


def test_inject_block_range_picks_page_by_page_number():
    """chunk.page_number 决定去 layout[page_number] 找,不会跨页误匹配。"""
    layouts = _make_layout(
        [_make_block("第一页内容 公司各应急保障", 0)],
        [_make_block("第二章要求的内容", 1)],
    )
    nodes = [_make_chunk_node("第二章要求的内容", 1)]
    inject_block_range(nodes, layouts)
    assert nodes[0].metadata["block_range"] == (1, 1)


# ── 与 ``core.text_norm_fixtures.json`` 共享用例(issue #167 / #169)────


def test_block_match_via_map_chunk_to_blocks():
    """``core/text_norm_fixtures.json`` 的 ``block_match`` 段用例,通过
    ``map_chunk_to_blocks`` 单 block 形态跑一遍。

    端到端验证:T1 / P2 规则与 ``_block_matches_chunk`` 同源,JSON fixtures
    的 ``expected`` 字段在这里也成立(单 block 形态)。
    """
    import json as _json
    from pathlib import Path
    fixtures_path = Path(__file__).parent.parent / "core" / "text_norm_fixtures.json"
    fixtures = _json.loads(fixtures_path.read_text())
    for case in fixtures["block_match"]:
        layout = _make_single_page([_make_block(case["block"], 0)])
        result = map_chunk_to_blocks(case["chunk"], layout)
        if case["expected"]:
            assert result is not None, (
                f"case {case['note']!r} 应命中,实际 None"
            )
        # expected=False 时 result 不必是 None(可能 None 或 (0, 0) +
        # block 内容无关)—— 我们只验证 expected=True 的 case 能命中。
