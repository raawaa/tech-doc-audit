"""KB 文档"服务端静默截短"判定(spec #173 / issue #179)。

PaddleOCR SaaS 对超过 ``PADDLEOCR_PAGE_LIMIT`` 的 PDF 历史上有过静默截断
(返回前 N 页的解析结果但不报错),导致 KB 内出现"声称 ``embedded`` 但
解析页数 < 源 PDF 物理页数"的 doc。本模块是这类 doc 的**唯一**判定入口
—— issue #93 教训:同一个问题只要有第二份实现就必然分叉
(批量重新解析三条选取规则的第三条就是这么丢的)。

公开 API:
- :class:`TruncatedDoc` —— 命中条目(doc + 解析页数 + 物理页数)
- :func:`find_truncated_docs(kb_id)` —— 返回该 KB 内全部命中条目

判定流程(issue #179 spec,**全部**命中才入选):

1. ``doc.embedding_status == "embedded"`` —— 只查声称成功的。``failed`` /
   ``truncated`` / ``pending_index`` 的 doc 早在**待重解析文档**名单里
   (``services.bulk_reparse_service`` 规则 1),重复报告没有价值。
2. :func:`core.paddleocr_cache.cache_source_by_hash` ∈ ``{"paddleocr",
   "paddleocr_split"}`` —— PyMuPDF / fallback_* 路径不截断,先挡掉避免误报。
3. ``physical = doc.page_count or pdf_page_count(doc.file_path)``;
   ``physical`` 为 ``None`` → 跳过(读不到就不下结论,绝不为读不到的
   信息发明坏结论 —— spec AC#42 的精神,与 :func:`core.parse_document.
   pdf_page_count` 同口径)。
4. ``len(pages_store.load_pages(...)["by_page"]) < physical``。
   pages 文件缺失按"读不到"走,跳过本条报告(``missing_pages`` 已由
   批量选取规则 2 覆盖,不归本模块管)。

不做的事:
- 不写 doc / kb 状态 —— 这是**只读**判定;真正修复由后续
  ``repair_truncated.py`` / ``run_bulk_reparse`` 走 ``reparse_one``
  全流程推到 ``embedded``。
- 不发请求 / 不烧 OCR 配额 —— 全部基于 doc_repo + pages_store + 缓存条目
  + 源 PDF ``pymupdf.open().page_count`` 就地可读。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from core import pages_store, paddleocr_cache
from core.parse_document import pdf_page_count
from models.document import KBDocument
from storage import doc_repo


# 触发"可能被截短"判定的缓存 source 取值集(spec #173:解析器真烧过配额的
# 两条路径都可能出现服务端截短 —— 整篇 ``paddleocr`` 与拆分 ``paddleocr_split``)。
# 这两个值直接来自 :mod:`core.paddleocr_cache`,在此用字面量集合表达
# "**哪些 source 会被服务端截断**"的语义,不是字符串比较:换 source 名得
# 同时改判定 + 改 :func:`cache_source_by_hash` 的写入点,grep 即可穷举。
_TRUNCATABLE_SOURCES = frozenset({
    paddleocr_cache.SOURCE_PADDLEOCR,
    paddleocr_cache.SOURCE_PADDLEOCR_SPLIT,
})


@dataclass(frozen=True)
class TruncatedDoc:
    """一篇被判定为"服务端静默截短"的 KB 文档 + 诊断信息。

    Attributes:
        doc: 命中的 KBDocument 实例。
        parsed_pages: 实际解析的页数(``len(pages_store.load_pages(...)["by_page"])``)。
        physical_pages: 源 PDF 真实物理页数(``doc.page_count`` 或
            ``pdf_page_count(doc.file_path)`` 回落)。
    """

    doc: KBDocument
    parsed_pages: int
    physical_pages: int


def find_truncated_docs(kb_id: str) -> list[TruncatedDoc]:
    """返回该 KB 内被服务端静默截短过的全部 doc。

    判据见模块 docstring,四条全部命中才入选;任何一条不满足都跳过(不报)。
    """
    docs = doc_repo.list_docs(kb_id)
    truncated: list[TruncatedDoc] = []
    for doc in docs:
        # 判据 #1:只查声称成功的;其它终态已在待重解析名单里。
        if doc.embedding_status != "embedded":
            continue

        # 判据 #2:PyMuPDF 等不截断的路径先挡掉(content_hash 缺失视为 None,
        # 不在截短 source 集里 → 跳过,与"读不到就不下结论"一致)。
        content_hash = doc.content_hash
        if content_hash is None:
            continue
        if paddleocr_cache.cache_source_by_hash(content_hash) not in _TRUNCATABLE_SOURCES:
            continue

        # 判据 #3:物理页数优先读元数据,缺失回落 ``pdf_page_count``;再读不到 → 跳过。
        physical: Optional[int] = doc.page_count
        if physical is None:
            physical = pdf_page_count(doc.file_path) if doc.file_path else None
        if physical is None:
            continue

        # 判据 #4:解析页数严格 < 物理页数才报。pages 文件缺失按"读不到"处理,
        # 不归本模块管 —— 批量选取规则 2 已经覆盖。
        loaded = pages_store.load_pages(kb_id, doc.id)
        if loaded is None:
            continue
        parsed_pages = len(loaded.get("by_page", []))
        if parsed_pages >= physical:
            continue

        truncated.append(TruncatedDoc(
            doc=doc,
            parsed_pages=parsed_pages,
            physical_pages=physical,
        ))
    return truncated
