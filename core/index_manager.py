"""KB 写入编排 + 跨 KB 向量检索。

Issue #165 / #168 拆分:存储层(FAISS HNSW build/persist、sidecar meta、
vector cache、per-KB 锁)已迁到 ``core.kb_index_store.KBIndexStore``;
本模块只剩写入编排(chunking / metadata 富化 / page-num 注入 /
block-range 注入 / embed 重试派发)与跨 KB 检索汇总。

公开入口(issue #168 AC #4 行为不变):
- ``index_document`` / ``index_documents_batch`` / ``remove_document``
- ``rebuild_kb_index``(2-phase 编排:``rebuild_from_vectors`` fast-path +
  GPU re-embed fallback)
- ``get_kb_index`` / ``get_kb_index_built`` / ``search`` / ``clear_cache``

私有边界:
- chunk / metadata / page-num / block-range 注入(``_inject_*`` 系列、
  ``_split_document`` / ``_has_markdown_headings`` / ``_enrich_chunk_metadata``
  / ``_chunk_prefix`` / ``_find_chunk_block_range`` / ``_normalize_layout``)
  留在本模块——它们是"编排的细节",不属于存储层。
- FAISS / sidecar / vector cache 的入口(``_vectors_dir`` / ``_index_meta_path``
  / ``_read_index_meta`` / ``_write_index_meta`` / ``_persist`` /
  ``_save_doc_vectors`` / ``_cleanup_doc_vectors`` / ``_rebuild_from_vectors``
  / ``_load_index`` / ``_create_index``)以下划线保留为 backward-compat shim,
  委派给 ``KBIndexStore.open(kb_id)``——50+ tests 沿用这些私有符号,
  本次拆分先保契约不动;后续 issue (#165 PR-3 KBIndexWriter)会迁测试到
  ``KBIndexStore`` 的公开 API 上。
- per-KB 锁(原 ``_get_index_lock``)已被 ``KBIndexStore`` 内部封装——
  issue #168 AC #3 "no external symbol exposes it" 的硬兑现:外部代码
  看不到锁,也拿不到锁对象,只能通过 ``KBIndexStore.open(kb_id)`` 单例化
  实例间接获取。
"""
from __future__ import annotations

import gc
import shutil
from pathlib import Path
from typing import Optional

from llama_index.core import Document, Settings
from llama_index.core.node_parser import MarkdownNodeParser, SentenceSplitter
from llama_index.core.schema import NodeWithScore

from core.embed_retry import embed_batch_with_retry
from core.kb_index_status import KbIndexStatusWriter
from core.kb_index_store import (
    INDEX_META_FILENAME,
    KBIndexStore,
    reset_singletons,
)
from core.logger import get_logger
from core.parse_document import PageLayout, PageText
from core.settings import get_embed_model, get_gpu_inference_lock
from core.text_norm import _block_matches_chunk, norm
import storage.doc_repo as doc_repo

_logger = get_logger(__name__)


# ── Backward-compat shims:存储层入口委派给 KBIndexStore ───────────────────────
# 历史 50+ tests 直接 ``from core.index_manager import _vectors_dir`` 等。
# 此次拆分把存储迁到 KBIndexStore,但保这些下划线符号仍能 import——
# 内部全部转调 ``KBIndexStore.open(kb_id)`` 的对应方法。
#
# 这是过渡期(issue #168 AC "All existing tests still pass"),后续
# KBIndexWriter PR 会把它们迁到 KBIndexStore 的公开 API,然后这里清空。


def _vectors_dir(kb_id: str) -> Path:
    """[shim] KB vectors 目录。"""
    return KBIndexStore.open(kb_id)._vectors_dir()


def _index_meta_path(kb_id: str) -> Path:
    """[shim] ``index.meta.json`` 路径。"""
    return KBIndexStore.open(kb_id)._index_meta_path()


def _read_index_meta(kb_id: str) -> Optional[dict]:
    """[shim] 读 ``index.meta.json``;缺失返 None。"""
    return KBIndexStore.open(kb_id).get_meta()


def _assert_kb_embedding_system_matches(
    kb_id: str, *, model_id: str = "BAAI/bge-m3", dim: int = 1024,
) -> None:
    """[shim] 写入前断言 ``index.meta.json`` 与 provider 一致(issues/144 AC#3)。

    委派 ``KBIndexStore.open(kb_id).assert_embedding_system_matches()``。
    """
    KBIndexStore.open(kb_id).assert_embedding_system_matches(
        model_id=model_id, dim=dim,
    )


def _write_index_meta(
    kb_id: str, *, model_id: str = "BAAI/bge-m3", dim: int = 1024,
    created_at: Optional[str] = None, force: bool = False,
) -> None:
    """[shim] 原子写入 ``index.meta.json``。

    ``scripts/backfill_kb_meta.py`` 与 tests 沿用此签名。
    """
    KBIndexStore.open(kb_id)._write_index_meta(
        model_id=model_id, dim=dim, created_at=created_at, force=force,
    )


def _create_index(dim: int = 1024):
    """[shim] 创建新的空 FAISS HNSW 索引(不入 store 单例缓存)。

    注:返回**未注入单例缓存**的新索引;调用方负责后续 ``_persist``。
    测试场景偶有需要"自己造一个空索引,不入缓存"的用法。
    """
    # KBIndexStore._create_index 是实例方法,需要先 open()——但 open() 会
    # 把新索引注入 ``_index_cache[kb_id]``。为避免污染,临时借一个 dummy
    # 实例的内部实现,丢弃该实例。
    return KBIndexStore.open("__create_index_dummy__")._create_index(dim)


def _load_index(kb_id: str):
    """[shim] 从磁盘加载 FAISS 索引;失败返 None。"""
    return KBIndexStore.open(kb_id)._load_index()


def _persist(kb_id: str, index) -> None:
    """[shim] 持久化 FAISS 索引 + docstore 到磁盘。

    测试用 ``mock.patch("core.index_manager._persist", ...)`` 拦截失败路径,
    故保留模块级函数(而非 ``store._persist`` 直调)——同名符号被 patch 时
    本函数能拦截。
    """
    KBIndexStore.open(kb_id)._persist(index)


def _save_doc_vectors(
    kb_id: str, doc_id: str, nodes: list, embeddings: list,
) -> None:
    """[shim] 保存 doc 的向量 + 节点元数据到磁盘。"""
    KBIndexStore.open(kb_id)._save_doc_vectors(doc_id, nodes, embeddings)


def _cleanup_doc_vectors(kb_id: str, doc_id: str) -> None:
    """[shim] 删除 doc 的向量缓存文件。"""
    KBIndexStore.open(kb_id)._cleanup_doc_vectors(doc_id)


def _rebuild_from_vectors(
    kb_id: str, doc_ids: list[str], progress_callback=None,
) -> None:
    """[shim] 从已落盘的 ``.npy`` 向量文件重建 FAISS 索引。"""
    KBIndexStore.open(kb_id).rebuild_from_vectors(
        doc_ids, progress_callback=progress_callback,
    )


# ── 编排层工具:chunking / metadata / page-num / block-range 注入 ───────────


def _split_document(doc: Document):
    """根据文档内容选择分块器。"""
    if _has_markdown_headings(doc.text):
        splitter = MarkdownNodeParser()
    else:
        splitter = SentenceSplitter(chunk_size=512, chunk_overlap=50)
    return splitter.get_nodes_from_documents([doc])


def _has_markdown_headings(text: str) -> bool:
    """快速检测文本是否包含 Markdown 标题层级。"""
    import re
    return bool(re.search(r"^#{1,6}\s+\S", text, re.MULTILINE))


def _enrich_chunk_metadata(
    nodes: list,
    doc_id: str,
    source_name: str,
):
    """从文档分块文本中检测条款编号和章节标题,注入 ``node.metadata``。

    使 FAISS 搜索结果能追溯到标准的某个具体条款(如
    "CJJ101-2016 第 3.2.1 条")。不在 text 中注入元数据,避免稀释
    embedding 语义信号。
    """
    import re
    clause_re = re.compile(r"(\d+(?:\.\d+)*)")

    for node in nodes:
        text = node.text or ""
        if not text:
            continue

        nums = clause_re.findall(text)
        if nums:
            clause = max(nums, key=lambda n: n.count("."))
            node.metadata["clause_number"] = clause

        sec_match = re.search(r"^(#{1,6})\s+(.+)", text, re.MULTILINE)
        if sec_match:
            node.metadata["section_path"] = sec_match.group(2).strip()

        node.metadata.setdefault("doc_id", doc_id)
        node.metadata.setdefault("source", source_name)


def _chunk_prefix(text: str, max_chars: int = 200) -> str:
    """用于页号定位的 chunk 前缀。

    取 chunk 首段非空连续字符(前 ``max_chars`` 字),足以在 ``by_page[*].text``
    找到匹配;跨页章节的前缀会落在章节首字符所在页。
    """
    if not text:
        return ""
    return text.strip()[:max_chars]


def _inject_page_number(nodes: list, by_page) -> None:
    """把 chunk 起始文本所在的页号写进 ``node.metadata["page_number"]``。

    - 输入:已分块好的 nodes;by_page(可选)按页文本列表(page=0-based, text=str)。
    - 对每个 node,取 ``_chunk_prefix(node.text)`` 在每页文本里 ``find``;
      首个命中页写入 ``metadata["page_number"]``。
    - 找不到(页级粒度退化、文本修复后字符变化)→ 写 ``None``,不阻塞。
    - 没传 by_page 或为空 → 直接写 ``None``。
    - 纯函数:原地改 metadata。
    """
    if not nodes:
        return
    pages_text: list[str] = []
    if by_page:
        pages_text = [(p.text or "") for p in by_page if p.text is not None]

    for node in nodes:
        prefix = _chunk_prefix(node.text or "")
        page_num = None
        if prefix and pages_text:
            for i, pt in enumerate(pages_text):
                if pt.find(prefix) != -1:
                    page_num = i  # 0-based
                    break
        node.metadata["page_number"] = page_num


def _find_chunk_block_range(
    chunk_text: str,
    page_blocks: list,
) -> tuple[int, int] | None:
    """在单页 layout blocks 中找 chunk 覆盖的 ``(start_block_order,
    end_block_order)`` 区间。

    按 block_order 升序遍历,记录所有命中 block 的 block_order;
    至少一个命中才返回闭区间 ``(min, max)``,全无命中返回 ``None``。
    跨 block 的 chunk(OCR 拆散场景)→ 区间 ``(min, max)``,``max > min``。
    """
    if not chunk_text or not page_blocks:
        return None
    chunk_norm = norm(chunk_text)
    if not chunk_norm:
        return None

    sorted_blocks = sorted(
        page_blocks, key=lambda b: getattr(b, "block_order", 0) or 0,
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


def _normalize_layout(by_layout) -> list | None:
    """``list[PageLayout]`` / ``list[dict]`` / ``None`` → ``list[PageLayout] | None``。

    旧 API 残留:tests / 序列化路径可能传 ``list[dict]``。归一后下游只读
    ``PageLayout.blocks`` / ``PageLayout.page`` 属性,不再做类型判断。
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
                    _Block(**b) if isinstance(b, dict) else b for b in blocks_raw
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


def _inject_block_range(nodes: list, by_page=None, by_layout=None) -> list:
    """[V8-S2] 把 chunk 覆盖的 KB layout block 区间写进
    ``node.metadata["block_range"]``。

    Contract:
      - 输入:已经 ``_inject_page_number`` 过的 nodes(``node.metadata["page_number"]``
        已是 0-based 页号)。
      - by_page:``list[PageText]``,与 ``_inject_page_number`` 同构(预留接口、
        当前未使用——分块落点已在 ``page_number`` 字段)。
      - by_layout:``list[PageLayout]`` 或 ``list[dict]``(0-based 与 by_page 平行)。
        旧 API 残留 dict 形式由本函数内自动归一为 PageLayout。
        ``None`` 或缺失页 → chunk.block_range = None(非 PDF KB / 旧 KB /
        异常 layout 走 fallback)。
      - 输出:原 nodes(原地修改 metadata),便于调用方链式接住。
      - 找不到任何命中(罕见,OCR 重排/字符差异大)→ 写 ``None``,不抛、
        不阻塞索引。
      - 跨页 chunk:仅记录起始页的 block 区间(与 ``page_number`` 同语义,
        MVP 限制)。

    匹配规则与 ``frontend/src/lib/layoutMatch.ts:matchHighlightToBlocks``
    对齐:NFKC + casefold + 去空白 + 去中英标点 → T1 双向 includes →
    P2 LCS 兜底(短串 < 4 字符不跑 LCS)。
    """
    if not nodes:
        return nodes
    normalized_layout = _normalize_layout(by_layout) if by_layout is not None else None
    for node in nodes:
        if normalized_layout is None:
            node.metadata["block_range"] = None
            continue
        page_number = node.metadata.get("page_number")
        if (
            page_number is None
            or page_number < 0
            or page_number >= len(normalized_layout)
        ):
            node.metadata["block_range"] = None
            continue
        page_layout = normalized_layout[page_number]
        page_blocks = getattr(page_layout, "blocks", None) or []
        chunk_text = node.text or ""
        result = _find_chunk_block_range(chunk_text, page_blocks)
        node.metadata["block_range"] = result
    return nodes


# ── 公开 API:写入 ────────────────────────────────────────────────────────────


def index_document(
    kb_id: str, doc_id: str, text: str, source_name: str = "",
    by_page=None, by_layout=None,
):
    """对文档文本分块 → embedding → 写入 KB 索引 + 持久化向量(V6 单一入口)。

    V4(PRD #29)chunking 与页码解耦:整篇 ``text`` 走一套分块器
    (``MarkdownNodeParser`` / ``SentenceSplitter``),事后
    ``_inject_page_number`` 把页号写回 ``node.metadata["page_number"]``,
    跨页章节不被腰斩。

    V8-S2 增 ``by_layout``:把每个 chunk 归一化后与该页 OCR layout blocks
    匹配,把覆盖区间 ``(start_block_order, end_block_order)`` 写入
    ``node.metadata["block_range"]``。``None`` → block_range = None 走 fallback。

    Args:
        by_page: ``ParseResult.by_page`` 同构(``list[PageText|str]``)。
        by_layout: ``ParseResult.layout`` 同构(``list[PageLayout]``)。``None``
            → 跳过 block_range 注入(旧 KB / 非 PDF 走 fallback 高亮)。
    """
    if not text or len(text) < 20:
        return

    # 兼容 ``list[str]``:旧 API 残留,pages_store / reparse 全部传 PageText 实例
    if by_page and not isinstance(by_page[0], PageText):
        by_page = [PageText(page=i, text=t) for i, t in enumerate(by_page)]
    # 兼容 ``list[dict]``:旧 API 残留的 layout dict 形式由
    # ``_inject_block_range`` 内 ``_normalize_layout`` 统一归一

    embed_model = get_embed_model()
    if embed_model is None:
        raise RuntimeError("Embedding model not loaded, cannot index document")

    # 整篇切 chunk(V4:不再按页硬切)
    doc = Document(
        text=text,
        id_=doc_id,
        metadata={"doc_id": doc_id, "source": source_name or doc_id},
    )
    all_nodes = _split_document(doc)
    _enrich_chunk_metadata(all_nodes, doc_id, source_name or doc_id)
    # 事后注入页号
    _inject_page_number(all_nodes, by_page)
    # V8-S2: 注入 chunk 覆盖的 layout block 区间(无 by_layout → 全 None,走 fallback)
    all_nodes = _inject_block_range(all_nodes, by_page, by_layout)
    del doc

    if not all_nodes:
        return

    # 预 embedding:拿到向量引用后再插入索引,避免重复推理
    node_texts = [node.text or "" for node in all_nodes]
    with get_gpu_inference_lock():
        embeddings = embed_batch_with_retry(embed_model, node_texts)
    for node, emb in zip(all_nodes, embeddings):
        node.embedding = emb

    # 委派存储层:per-KB 锁 + meta 断言 + .npy 落盘 + FAISS insert + persist
    KBIndexStore.open(kb_id).add_doc(doc_id, all_nodes, embeddings)

    del all_nodes, embeddings
    gc.collect()


def index_documents_batch(
    kb_id: str,
    docs: list,  # [(doc_id, text, source_name, by_page?, by_layout?)] — 兼容 3/4/5 元组
    progress_callback=None,
):
    """批量索引文档:分块 → 批量 embedding → 保存向量 → 写入 FAISS。

    每篇文档内部的所有 chunk 批量 embedding(利用 embed_batch_size 加速),
    embedding 结果持久化为 ``.npy`` 文件供后续快速重建。

    V4(PRD #29)跨页章节不再被腰斩:整篇 ``text`` 走一套分块器,
    事后 ``_inject_page_number`` 把页号写回 ``node.metadata``。

    V8-S2 增 ``by_layout``:每篇 doc 携带自身 layout 数据(与 by_page 平行);
    ``_inject_block_range`` 用其写入 chunk 覆盖的 ``block_range``。

    Args:
        kb_id: 知识库 ID。
        docs: 3/4/5 元组列表:
              ``(doc_id, text, source_name)`` 或
              ``(doc_id, text, source_name, by_page)`` 或
              ``(doc_id, text, source_name, by_page, by_layout)``。
        progress_callback: 可选回调 ``(current, total, doc_name) → None``。
    """
    embed_model = get_embed_model()
    if embed_model is None:
        raise RuntimeError("Embedding model not loaded, cannot index documents")

    total = len(docs)

    for idx, item in enumerate(docs, 1):
        doc_id, text, source_name = item[0], item[1], item[2]
        by_page = item[3] if len(item) > 3 else None
        by_layout = item[4] if len(item) > 4 else None
        # 兼容 ``list[str]``:旧 API 残留
        if by_page and not isinstance(by_page[0], PageText):
            by_page = [PageText(page=i, text=t) for i, t in enumerate(by_page)]
        if progress_callback:
            progress_callback(idx, total, source_name or doc_id)

        if not text or len(text) < 20:
            continue

        # 整篇切 chunk(V4:不再按页硬切)
        doc = Document(
            text=text,
            id_=doc_id,
            metadata={"doc_id": doc_id, "source": source_name or doc_id},
        )
        all_nodes = _split_document(doc)
        _enrich_chunk_metadata(all_nodes, doc_id, source_name or doc_id)
        # 事后注入页号(接受页级粒度退化,None 不阻塞)
        _inject_page_number(all_nodes, by_page)
        # V8-S2: 注入 chunk 覆盖的 layout block 区间(无 by_layout → 全 None)
        all_nodes = _inject_block_range(all_nodes, by_page, by_layout)
        del doc

        if not all_nodes:
            continue

        # 批量 embedding 本稿件所有 chunk(GPU 锁内,连接层失败自动重试)
        # ADR-0007 §3:单稿 embedding 失败不中止整批(每稿隔离)。失败稿
        # 标 ``embedding_status=failed``,原因 best-effort 落到 doc_repo,
        # 其余稿继续。追跑走 reparse 通道(OCR 缓存命中零配额)。
        node_texts = [node.text or "" for node in all_nodes]
        try:
            with get_gpu_inference_lock():
                embeddings = embed_batch_with_retry(embed_model, node_texts)
        except Exception as e:
            _logger.error(
                "embedding failed for doc %s after retries: %s", doc_id, e,
            )
            doc_repo.mark_doc_embedding_failed(kb_id, doc_id, e)
            del all_nodes
            continue

        for node, emb in zip(all_nodes, embeddings):
            node.embedding = emb

        # 委派存储层:per-KB 锁内做 .npy 落盘 + FAISS insert + persist
        KBIndexStore.open(kb_id).add_doc(doc_id, all_nodes, embeddings)

        del all_nodes, embeddings


def remove_document(kb_id: str, doc_id: str):
    """从 KB 索引中删除指定文档的所有节点。

    优先尝试向量级删除(``delete_ref_doc``),降级到从已保存的 ``.npy`` 向量
    重建索引(无需 GPU 重新 embedding)。
    """
    KBIndexStore.open(kb_id).remove_doc(doc_id)


def rebuild_kb_index(kb_id: str, progress_callback=None):
    """重建 KB 索引。

    优先从已保存的 ``.npy`` 向量重建(纯 CPU,秒级),
    向量缺失时降级到重新提取文本 + embedding(需要 GPU)。

    内置契约(ADR-0002 §决策 2):在 per-KB 锁内、根据本函数结果写回
    ``kb.index_status``:
    - 至少有一篇文档被成功索引 → ``'searchable'``(同时清 ``progress`` /
      ``current_doc``)
    - 重建过程抛异常 → ``'failed'``(保留 ``current_doc`` 为错误信息)
    - KB 不存在 → 静默返回

    Issue #149:本函数是首批持有 per-KB 锁(``KBIndexStore._lock``,保护 FAISS
    资源)的写入者之一——FAISS 锁继续保留。KB 检索状态字段(``index_status``
    / ``index_progress`` / ``index_current_doc``)则统一交给
    ``KbIndexStatusWriter``(issue #148),本函数不再直接赋值这三个字段。
    两种锁是不同资源:FAISS 锁保护 ``_load_index`` / ``_persist`` / 缓存
    失效,writer 的 per-instance 锁保护 KB 状态字段的 read-modify-write。

    Args:
        kb_id: 知识库 ID。
        progress_callback: 可选回调 ``(current_index, total, doc_name) → None``,
                           每处理完一篇文档后调用,用于外部汇报进度。
    """
    store = KBIndexStore.open(kb_id)
    with store.acquire_write_lock():
        # 缓存置空:让 phase 1 的 rebuild_from_vectors 拿一份全新的空索引
        # 起手,而 phase 2 的 index_document 后续也能从磁盘读到 phase 1
        # 写回的新索引——而不是用内存里残留的旧缓存。这是 issue #149
        # 引入的"编排层跨多次 store 调用持锁"——``acquire_write_lock()``
        # 把整个编排窗口包起来,内部的 ``store.rebuild_from_vectors`` /
        # ``index_document``(→ ``add_doc``)各自 ``with self._lock:`` 是 RLock
        # 重入,不死锁。
        store._index_cache.pop(kb_id, None)

        import storage.kb_repo as kb_repo
        kb = kb_repo.get(kb_id)
        if not kb:
            return

        # Issue #149:KB 检索状态字段交给 KbIndexStatusWriter 写。
        kb_writer = KbIndexStatusWriter(kb_id)
        kb_writer.begin()

        try:
            vectors_dir = _vectors_dir(kb_id)
            doc_ids = kb.document_ids

            with_vectors = []
            without_vectors = []
            for doc_id in doc_ids:
                if (vectors_dir / f"{doc_id}.npy").exists():
                    with_vectors.append(doc_id)
                else:
                    without_vectors.append(doc_id)

            # 阶段 1:从向量缓存快速重建(无需 GPU)
            if with_vectors:
                _logger.info(
                    "rebuilding kb %s from %d cached vectors (fast path)",
                    kb_id, len(with_vectors),
                )
                # 删除旧的 llama-index 持久化文件(_rebuild_from_vectors 成功后
                # 会 _persist 写回新的)
                old_store = vectors_dir / "default__vector_store.json"
                if old_store.exists():
                    old_store.unlink()
                for pattern in (
                    "docstore.json", "index_store.json", "graph_store.json",
                ):
                    p = vectors_dir / pattern
                    if p.exists():
                        p.unlink()

                _rebuild_from_vectors(
                    kb_id, with_vectors, progress_callback=progress_callback,
                )

            # 阶段 2:重新提取文本 + embedding(向量缓存缺失的文档)
            if without_vectors:
                _logger.info(
                    "rebuilding kb %s: %d docs need re-embedding (slow path)",
                    kb_id, len(without_vectors),
                )
                from storage.doc_repo import get_doc
                total = len(without_vectors)
                for i, doc_id in enumerate(without_vectors, 1):
                    doc = get_doc(kb_id, doc_id)
                    doc_name = (
                        doc.original_name if doc and doc.original_name else doc_id
                    )
                    if progress_callback:
                        progress_callback(i, total, doc_name)
                    if doc and doc.file_path and Path(doc.file_path).exists():
                        try:
                            from core.parse_document import parse_document as _parse_document
                            parse_result = _parse_document(doc.file_path)
                            text = parse_result.full_text
                            if text:
                                index_document(
                                    kb_id, doc_id, text,
                                    by_page=parse_result.by_page,
                                    by_layout=parse_result.layout,
                                )
                        except Exception as e:
                            _logger.warning("  [skip] %s: %s", doc_id, e)

            if not with_vectors and not without_vectors:
                # 无文档,清理 + 仍标 searchable(空库也是合法"无文档"状态)
                if vectors_dir.exists():
                    shutil.rmtree(str(vectors_dir))
                kb_writer.finish()
                return

            kb_writer.finish()
        except Exception as e:
            # 内置契约:失败 → 字段 failed(保留错误信息在 current_doc)。
            # Issue #149:失败摘要走 writer 的 ``finish(failed=...)`` 通道,
            # 不再直接写 ``kb.index_current_doc``。
            kb_writer.finish(failed=[("重建", str(e))])
            raise


# ── 公开 API:读 ──────────────────────────────────────────────────────────────


def get_kb_index(kb_id: str):
    """获取 KB 的 ``VectorStoreIndex``(加载或创建,带内存缓存)。

    委托 ``KBIndexStore.open(kb_id)._get_index()``——同一 kb_id 跨调用方
    共享同一 ``_index_cache`` 实例。

    保留原"读也持锁"语义:``acquire_write_lock`` 把整次读包起来,跟
    ``add_doc`` 写路径串行化,防止读到正在 persist 的半完成 FAISS。
    """
    store = KBIndexStore.open(kb_id)
    with store.acquire_write_lock():
        return store._get_index()


def get_kb_index_built(kb_id: str) -> bool:
    """检查 KB 是否可被向量检索。

    ADR-0002 单真相:本函数只读 KB 元数据中的 ``kb.index_status`` 字段。
    旧的依据 ``default__vector_store.json`` 文件是否存在的判定已弃用——
    FAISS 文件(含 ``.npy`` 文档向量缓存)仅为可从字段与文档重生的缓存,
    不再是状态真相。

    取值映射:
    - ``searchable`` → True(可向量检索)
    - ``building`` / ``none`` / ``failed`` → False(自愈路径触发条件)
    """
    import storage.kb_repo as _kb_repo
    kb = _kb_repo.get(kb_id)
    return kb is not None and kb.index_status == "searchable"


def clear_cache() -> None:
    """清空索引缓存(用于测试)。

    委托 ``KBIndexStore.reset_singletons()``——清掉所有 ``KBIndexStore`` 实例,
    因为它们的 ``_index_cache`` 是实例属性;只清 ``_index_cache`` 不够,
    旧实例仍持有旧缓存。
    """
    reset_singletons()


def search(
    kb_ids: list[str], query: str, top_k: int = 5, use_reranker: bool = True,
) -> list[dict]:
    """跨 KB 向量搜索。

    返回格式与旧版 ``vec_search()`` 兼容:
    ``[{source, kb_id, doc_id, content, doc_source, relevance}, ...]``

    当 reranker 可用时,用 cross-encoder 对候选结果重排序提升精度。
    """
    if not query or not kb_ids:
        return []

    from core.settings import get_embed_model, get_gpu_inference_lock, run_reranker
    # 确保 embed_model 已加载,防止 LlamaIndex 默认解析到 OpenAI
    get_embed_model()

    # 一次 query embedding(原代码在每个 KB retriever.retrieve 内各做一次,
    # 现在提到 search 入口做一次——同一 embedder、同一 query、同一结果,
    # 跨 KB 复用)。Query embedding 不走 ``embed_batch_with_retry``——
    # ADR-0007 §2:查询路径零附加重试,用户在等,长退避体感即挂死。
    query_embedding = Settings.embed_model.get_query_embedding(query)

    gpu_lock = get_gpu_inference_lock()

    # 将整个 GPU 相关操作置于锁内。HuggingFaceEmbedding 和
    # SentenceTransformerRerank 的 forward 非线程安全,并发调用会各自
    # 分配完整激活张量撑爆显存。此锁确保同时只有一个进程执行模型前向传播,
    # LLM 调用(DeepSeek API 不走 GPU)不受影响仍可并行。
    with gpu_lock:
        # 先收集 NodeWithScore 对象,保留完整 score 信息
        all_nodes: list[NodeWithScore] = []
        for kb_id in kb_ids:
            if not get_kb_index_built(kb_id):
                continue
            try:
                # ``store.search`` 内部持 per-KB 读锁;GPU 锁在外层包住
                # 整个跨 KB 检索,避免多 KB 并发撞 GPU。
                nodes = KBIndexStore.open(kb_id).search(query_embedding, top_k)
                for node in nodes:
                    node.node.metadata["kb_id"] = kb_id
                    all_nodes.append(node)
            except Exception as e:
                _logger.warning("vector search failed for kb %s: %s", kb_id, e)
                continue

        if not all_nodes:
            return []

        all_nodes.sort(key=lambda n: n.score or 0, reverse=True)
        all_nodes = all_nodes[: top_k * 2]  # 多留候选给 reranker

        # ── Reranker 重排序(按需加载→推理→卸载)────────────────────────
        if use_reranker:
            try:
                reranked = run_reranker(all_nodes, query)
                if reranked:
                    all_nodes = reranked
            except Exception as e:
                _logger.warning("reranker failed in search, using raw ranking: %s", e)

    # 转换为 dict 返回格式
    hits = []
    for node in all_nodes[:top_k]:
        meta = node.metadata or {}
        hits.append({
            "source": "vec_search",
            "kb_id": meta.get("kb_id", ""),
            "doc_id": meta.get("doc_id", ""),
            "content": node.text,
            "doc_source": meta.get("source", ""),
            "section_path": meta.get("section_path", ""),
            "clause_number": meta.get("clause_number", ""),
            "page_number": meta.get("page_number"),  # int or None, 0-based
            # V8-S3: 从 node.metadata 透传 block_range;None/缺失 → 字段仍暴露(None),
            # 调用方按字段是否存在决定显示——_tool_flag_issue / standard_linker /
            # IssueResponse 都按"非 None 才使用"消费。
            "block_range": meta.get("block_range"),
            "relevance": round(node.get_score() or 0, 4),
        })

    return hits
