"""``core.text_norm`` 契约测试 —— 用例来自 ``core/text_norm_fixtures.json``。

P0 契约:KB 索引阶段写出的 ``block_range`` 与前端 fallback 字符串匹配的
高亮位置不能漂移。归一化 + T1/P2 规则必须与 ``frontend/src/lib/layoutMatch.ts``
一致。

用例不写在本文件里 —— 它们与 ``frontend/src/lib/layoutMatch.test.ts`` 共享
同一份 JSON fixtures(issue #167)。任一端实现漂移,两端测试同时红。
新增用例只加进 JSON,两侧自动接住。
"""
import json
from pathlib import Path

import pytest

from core.text_norm import (
    _LCS_RATIO_THRESHOLD,
    _MIN_LCS_LEN,
    _block_matches_chunk,
    lcs_len,
    norm,
)

_FIXTURES = json.loads(
    (Path(__file__).resolve().parents[1] / "core" / "text_norm_fixtures.json")
    .read_text(encoding="utf-8")
)


def _ids(cases: list[dict]) -> list[str]:
    return [c["note"] for c in cases]


@pytest.mark.parametrize(
    "case", _FIXTURES["norm"], ids=_ids(_FIXTURES["norm"])
)
def test_norm(case):
    """NFKC + casefold + 去空白 + 去中英标点。"""
    assert norm(case["chunk"]) == case["expected"]


@pytest.mark.parametrize(
    "case", _FIXTURES["lcs_len"], ids=_ids(_FIXTURES["lcs_len"])
)
def test_lcs_len(case):
    """字符级 LCS 长度(不归一化,由调用方决定)。"""
    assert lcs_len(case["chunk"], case["block"]) == case["expected"]


@pytest.mark.parametrize(
    "case", _FIXTURES["block_match"], ids=_ids(_FIXTURES["block_match"])
)
def test_block_matches_chunk(case):
    """T1 双向 includes + P2 LCS 兜底。

    ``_block_matches_chunk`` 收归一化后的串(调用方 ``_find_chunk_block_range``
    先 ``norm``);fixtures 存原始串,本处补 ``norm`` 这一步 —— 与前端
    ``blockMatchesHighlight(block, highlight)`` 内部先 norm 的行为对齐。
    """
    assert (
        _block_matches_chunk(norm(case["chunk"]), norm(case["block"]))
        is case["expected"]
    )


@pytest.mark.parametrize(
    "case",
    _FIXTURES["known_divergences"],
    ids=_ids(_FIXTURES["known_divergences"]),
)
def test_known_divergence_from_frontend(case):
    """把 Python/TypeScript 已知不一致的判定钉住(见 fixtures 的 note)。

    这不是"期望的行为",是"当前的行为"。哪天两端对齐了,本断言与前端对应
    断言会同时红 —— 那时删掉 fixtures 里的这一条、把它提升进 ``block_match``。
    """
    assert (
        _block_matches_chunk(norm(case["chunk"]), norm(case["block"]))
        is case["python"]
    )


def test_threshold_constants_are_the_documented_values():
    """阈值常量是 T1/P2 规则的一部分,与前端 ``MIN_LCS_LEN`` /
    ``LCS_RATIO_THRESHOLD`` 字面对齐;改动必须两端同步。"""
    assert _MIN_LCS_LEN == 4
    assert _LCS_RATIO_THRESHOLD == 0.85
