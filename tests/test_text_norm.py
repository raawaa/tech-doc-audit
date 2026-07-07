"""V8-S2 配套:``core.text_norm`` 与前端 ``layoutMatch.norm()`` 对齐测试。

P0 契约:KB 索引阶段写出的 ``block_range`` 与前端 fallback 字符串匹配的
高亮位置不能漂移。归一化规则必须与 frontend/src/lib/layoutMatch.ts 完全一致。
本测试的输入/期望值是 layoutMatch.test.ts 已有 case 的镜像。
"""
import pytest

from core.text_norm import norm, lcs_len


class TestNorm:
    """与 frontend/src/lib/layoutMatch.test.ts:describe('norm') 对齐。"""

    def test_nfkc_fullwidth_digit_to_halfwidth(self):
        """NFKC 全角数字归一为半角。"""
        assert norm("８00兆对讲机") == "800兆对讲机"

    def test_lowercase(self):
        """lowercase。"""
        assert norm("ABC Def") == "abcdef"

    def test_strip_whitespace(self):
        """去空白。"""
        assert norm("公 司 各 应 急") == "公司各应急"

    def test_strip_chinese_punctuation(self):
        """去常见中文标点。"""
        assert norm("公司、各应急单位应当配置。") == "公司各应急单位应当配置"

    def test_strip_english_punctuation(self):
        """去常见英文标点(NFKC 后统一为半角)。"""
        assert norm("(800) MHz radio, OK!") == "800mhzradiook"

    def test_fullwidth_letters_nfkc(self):
        """全角字母 NFKC 归一为半角。"""
        assert norm("（ＡＢＣ）") == "abc"

    def test_empty_string(self):
        """空串归一化结果为空串。"""
        assert norm("") == ""
        assert norm(None or "") == ""  # 防御 None


class TestLcsLen:
    """字符级 LCS 长度。"""

    def test_identical(self):
        assert lcs_len("hello", "hello") == 5

    def test_partial_overlap(self):
        assert lcs_len("abcd", "abce") == 3  # "abc"

    def test_empty(self):
        assert lcs_len("", "abc") == 0
        assert lcs_len("abc", "") == 0

    def test_no_overlap(self):
        assert lcs_len("abc", "xyz") == 0

    def test_ocr_single_char_diff(self):
        """OCR 单字错(讲 → 话):"800兆对讲机"(7 chars) vs "800兆对话机"(7 chars)
        → LCS = 6(7 - 1 差异字符)。"""
        assert lcs_len("800兆对讲机", "800兆对话机") == 6
        assert lcs_len("800兆对讲机", "800兆对话机") / 7 > 0.85
