"""chunk → KB layout block 映射规则的承载者(issue #169 / PR-3)。

为什么单独成模块(issue #165 PR-3):
- 历史上 ``_find_chunk_block_range`` / ``_normalize_layout`` 与 FAISS 建索引
  参数同挤在 ``core.index_manager`` 1163 行里;``block_range`` 出问题时,
  工程师要 scroll past 1000 行 FAISS plumbing 才能读到 T1/P2 判定代码。
- 把这段提到 ``ChunkLayoutMapper`` 后,code reviewer 读 1 个文件就能 verify
  T1/P2 阈值是否漂移;后续算法改动不再与"如何建 FAISS 索引"耦合。

公开 API:
- :func:`map_chunk_to_blocks(chunk_text, page_layout) -> tuple[int, int] | None`
- :func:`normalize_layout(by_layout)` —— 把 ``list[PageLayout]`` / ``list[dict]``
  归一为 ``list[PageLayout]``。**helper 函数**,提供给 ``KBIndexWriter``
  在 "for-all-nodes inject" 循环里调用。

前置依赖:
- ``core.text_norm`` 是 T1/P2 算法的**唯一来源**(归一化、LCS、阈值常量);
  本模块直接 ``from core.text_norm import _block_matches_chunk``,共享
  ``_MIN_LCS_LEN=4`` / ``_LCS_RATIO_THRESHOLD=0.85`` 常量。

不做的事:
- 不写 ``node.metadata`` —— 那段"for-all-nodes inject"循环是 metadata
  富化,职责归属 ``KBIndexWriter._inject_block_range``(issue #165 决策:
  "对所有 node 注入 block_range"循环留在 Writer,它写的是 metadata)。
- 不做跨页 — 跨页 chunk 只记录起始页(``page_number`` 字段已锚定),
  这是 MVP 限制(详见 :mod:`CONTEXT.md` §"Chunk → Layout 映射与高亮坐标")。
"""
from __future__ import annotations

from typing import Optional

from core.parse_document import PageLayout
from core.text_norm import _block_matches_chunk, norm


def map_chunk_to_blocks(
    chunk_text: str,
    page_layout: Optional[PageLayout],
) -> Optional[tuple[int, int]]:
    """把一段 chunk 文本映射到一页的 layout blocks 上,返回闭区间。

    按 ``block_order`` 升序遍历 ``page_layout.blocks``,对每个 block 用
    T1(双向 includes)→ P2(LCS ratio)判定;**至少一个命中**才返回
    ``(min_block_order, max_block_order)``,全部不命中返回 ``None``。

    与 ``core/text_norm._block_matches_chunk`` / ``frontend/src/lib/layoutMatch.ts:
    blockMatchesHighlight`` 同源同一阈值;两端共享
    ``core/text_norm_fixtures.json`` 的测试用例(任一端漂移 → 两端测试同时红)。

    Args:
        chunk_text: 一段 chunk 文本(原文本,**未归一化**——本函数内部先
            跑 ``norm()`` 再传给判定)。
        page_layout: 单页 layout(``PageLayout`` 实例,含 ``blocks`` 列表);
            ``None`` → 函数返 ``None``(非 PDF / 缺 layout 走 fallback 高亮)。

    Returns:
        - ``(start, end)``(闭区间,``start <= end``):至少一个 block 命中。
        - ``None``:chunk_text 空 / 归一化后空 / page_layout 缺 / 全无命中。

    已知分歧:P2 的 ratio 分母本函数取 ``min``,前端 ``blockMatchesHighlight``
    已改为 ``max``(详见 ``core/text_norm_fixtures.json:known_divergences``)。
    两端在等长 / 包含场景下结论一致;长 content + 短 highlight 且字符散落时
    本函数更宽松。issue #167 只搬模块不改行为,故此处保留旧语义。
    """
    if not chunk_text or page_layout is None:
        return None
    page_blocks = getattr(page_layout, "blocks", None) or []
    if not page_blocks:
        return None
    chunk_norm = norm(chunk_text)
    if not chunk_norm:
        return None

    sorted_blocks = sorted(
        page_blocks,
        key=lambda b: getattr(b, "block_order", 0) or 0,
    )
    matched_orders: list[int] = []
    for b in sorted_blocks:
        block_content = getattr(b, "block_content", "") or ""
        if _block_matches_chunk(chunk_norm, norm(block_content)):
            order = getattr(b, "block_order", 0) or 0
            matched_orders.append(int(order))
    if not matched_orders:
        return None
    return (min(matched_orders), max(matched_orders))


def normalize_layout(by_layout) -> Optional[list[PageLayout]]:
    """``list[PageLayout]`` / ``list[dict]`` / ``None`` → ``list[PageLayout] | None``。

    旧 API 残留:tests / 序列化路径可能传 ``list[dict]``。归一后下游只读
    ``PageLayout.blocks`` / ``PageLayout.page`` 属性,不再做类型判断。

    Returns:
        - ``None``:``by_layout is None``(caller 想"跳过 block_range 注入"
          的信号——非 PDF KB / 旧 KB / 异常 layout 走 fallback 高亮)。
        - ``[]``:``by_layout == []``(空 list 与 None 语义不同:caller 想
          跑注入,但 layout 实际为空)。
        - ``list[PageLayout]``:归一化结果。
    """
    if by_layout is None:
        return None
    if not by_layout:
        return []
    if not isinstance(by_layout[0], PageLayout):
        from core.parse_document import Block as _Block
        normalized = []
        for p in by_layout:
            if isinstance(p, dict):
                blocks_raw = p.get("blocks") or []
                blocks = [
                    _Block(**b) if isinstance(b, dict) else b
                    for b in blocks_raw
                ]
                normalized.append(PageLayout(
                    page=p.get("page", 0),
                    width=p.get("width", 0),
                    height=p.get("height", 0),
                    blocks=blocks,
                ))
            else:
                normalized.append(p)
        return normalized
    return list(by_layout)

