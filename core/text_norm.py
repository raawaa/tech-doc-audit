"""文本归一化与字符级 LCS —— 后端与前端 ``layoutMatch.norm()`` 对齐。

为什么单独成模块:
- ``core.index_manager._inject_block_range`` 在 KB 索引阶段需要把 chunk
  文本归一化后与 OCR layout blocks 匹配（V8 PRD #49）。
- 匹配规则必须与前端 ``frontend/src/lib/layoutMatch.ts:norm()`` 一致——
  否则后端写出的 ``block_range`` 与前端 fallback 字符串匹配的高亮位置
  会偏移。
- 集中放本模块后,任何需要做"chunk ↔ block"匹配的后端路径都能复用,
  避免算法在前/后端双份定义后漂移。

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
