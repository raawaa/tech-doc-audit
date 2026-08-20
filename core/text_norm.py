"""chunk ↔ layout block 匹配算法的唯一来源(归一化 / LCS / T1-P2 判定)。

为什么单独成模块:
- ``core.index_manager._inject_block_range`` 在 KB 索引阶段需要把 chunk
  文本归一化后与 OCR layout blocks 匹配（V8 PRD #49）。
- 匹配规则必须与前端 ``frontend/src/lib/layoutMatch.ts`` 一致——
  否则后端写出的 ``block_range`` 与前端 fallback 字符串匹配的高亮位置
  会偏移。两端共享 ``core/text_norm_fixtures.json`` 一份用例(issue #167),
  任一端漂移即两端测试同时红。
- 集中放本模块后,任何需要做"chunk ↔ block"匹配的后端路径都能复用,
  避免算法在前/后端双份定义后漂移;阈值常量也不再与 FAISS 建索引参数
  挤在同一个文件里。

约束(NFKC + casefold + 去空白 + 去中英标点):
- 先 NFKC 再 lowercase:某些 Unicode 字符在 casefold 前 NFKC,等价类不闭合
  (例如全角字母Ａ→a)。颠倒顺序会让 norm() 结果不收敛。
- 标点列表:与前端 ``PUNCT_RE`` 完全对齐(标点用 NFKC 之后的等价形式)
  —— 全角括号、书名号、破折号、间隔号等都要剥,避免 OCR 加标点/不加
  标点时匹配漂移。
"""
from __future__ import annotations

import re
import unicodedata


# 中英常见标点 + 控制字符类空白 —— 与前端 layoutMatch.PUNCT_RE 对齐
_PUNCT_RE = re.compile(
    r"[\s　 -‏ - ﻿"
    r"!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~"
    r"。！？、；：（）【】「」『』《》·…—–]"
)


def norm(s: str) -> str:
    """NFKC + casefold + 去空白 + 去中英标点 → 归一化串。

    与 ``frontend/src/lib/layoutMatch.ts:norm()`` 一致。
    空输入返回空串,保证 ``norm(s).includes(norm(t))`` 不会因 None 报错。
    """
    if not s:
        return ""
    nfkc = unicodedata.normalize("NFKC", s)
    lower = nfkc.casefold()
    return _PUNCT_RE.sub("", lower)


def lcs_len(a: str, b: str) -> int:
    """字符级 LCS 长度(DP,O(n*m))。

    短串场景下与前端 ``layoutMatch.lcsLen`` 等价;长串(>数千字符)
    性能下降,但本模块只在 chunk↔block 匹配时调用,两端都是几十~几百
    字符,没有 hot-path 压力。
    """
    n, m = len(a), len(b)
    if not n or not m:
        return 0
    prev = [0] * (m + 1)
    curr = [0] * (m + 1)
    for i in range(1, n + 1):
        ai = a[i - 1]
        for j in range(1, m + 1):
            if ai == b[j - 1]:
                curr[j] = prev[j - 1] + 1
            else:
                left = curr[j - 1]
                top = prev[j]
                curr[j] = left if left >= top else top
        prev, curr = curr, [0] * (m + 1)
    return prev[m]


# ── T1/P2 chunk↔block 判定 ─────────────────────────────────────────────────────
#
# 不参与 LCS 兜底的最小串长（少于 4 字符噪声比太高且短到无意义）。
# 与 frontend/src/lib/layoutMatch.ts:MIN_LCS_LEN 对齐。
_MIN_LCS_LEN = 4
# LCS ratio 阈值：归一化后命中长度 / min(a, b) >= 此值才算命中。
# 与 frontend/src/lib/layoutMatch.ts:LCS_RATIO_THRESHOLD 对齐。
_LCS_RATIO_THRESHOLD = 0.85


def _block_matches_chunk(chunk_text_norm: str, block_content_norm: str) -> bool:
    """判断归一化后的 block_content 是否与归一化后的 chunk_text 命中。

    与 ``frontend/src/lib/layoutMatch.ts:matchHighlightToBlocks`` 的 T1/P2 规则
    对齐——保证 KB 索引阶段写出的 block_range 与前端 fallback 字符串匹配
    的高亮位置一致,不会因为后端阈值更严/更松导致两侧漂移。

    T1:双向 includes(block 是 chunk 子串也算——OCR 把同一段拆到多个 block
        时也能找到所有子块)。
    P2:短串 < MIN_LCS_LEN 时不跑 LCS,直接 false(短串噪声比太高)。

    输入是**已归一化**的串(调用方先过 ``norm()``),与前端
    ``blockMatchesHighlight`` 内部先归一化等价。

    已知漂移:P2 的 ratio 分母本函数取 ``min``,前端已改为 ``max``
    (见 ``core/text_norm_fixtures.json`` 的 ``known_divergences``)。两端在
    等长 / 包含场景下结论一致,长短悬殊且字符散落时本函数更宽松。
    """
    if not chunk_text_norm or not block_content_norm:
        return False
    # T1:双向 includes
    if chunk_text_norm in block_content_norm or block_content_norm in chunk_text_norm:
        return True
    # P2:LCS 兜底
    short_len = min(len(chunk_text_norm), len(block_content_norm))
    if short_len < _MIN_LCS_LEN:
        return False
    if len(block_content_norm) <= len(chunk_text_norm):
        ratio = lcs_len(block_content_norm, chunk_text_norm) / short_len
    else:
        ratio = lcs_len(chunk_text_norm, block_content_norm) / short_len
    return ratio >= _LCS_RATIO_THRESHOLD
