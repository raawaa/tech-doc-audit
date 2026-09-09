"""KB 读取 + 跨 KB 向量检索(issue #169 / PR-3 收敛)。

Issue #165 / #168 / #169 拆分经过三段:
- PR-1(#167):抽出 ``core.text_norm`` / ``core.embed_retry`` /
  ``storage.doc_repo.mark_doc_embedding_failed`` 三个叶子模块。
- PR-2(#168):存储层(FAISS HNSW build/persist、sidecar meta、vector
  cache、per-KB 锁)迁到 ``core.kb_index_store.KBIndexStore``。
- PR-3(#169):编排层(chunking / metadata 富化 / page-number 注入 /
  block-range 注入 / embed 重试派发)迁到 ``core.kb_index_writer.KBIndexWriter``;
  ``chunk→layout`` 判定迁到 ``core.chunk_layout_mapper``。

本模块是 ``core.index_manager`` 在 PR-3 之后的样子:
- **写入路径全部迁出** —— 历史 ``index_document`` / ``index_documents_batch``
  / ``rebuild_kb_index`` / ``remove_document`` 退化为 thin shim,委派给
  ``KBIndexWriter`` / ``KBIndexStore``。保留它们是为了让 50+ tests 不破:
  项目实践(PR-2)选择"先拆模块,留 shim 给现有 caller,后续按测试迁移
  节奏逐个消除",而不是 hard-delete 一次到位(详见 issue #165 spec
  §Testing Decisions: "Tests that drove private symbols ... are rewritten
  against the new public surface — they are not ported verbatim" 是后续
  ticket 的目标)。
- **读取路径留在本模块**:跨 KB 向量检索 ``search()`` + KB 元数据读
  ``get_kb_index`` / ``get_kb_index_built`` + 测试隔离 ``clear_cache``。
  这些不是"存储层"也不是"编排层",是 search-time aggregation,放在
  index_manager 里读起来最自然。
"""
from __future__ import annotations

from typing import Optional

from llama_index.core import Settings
from llama_index.core.schema import NodeWithScore

from core.kb_index_status import KbIndexStatusWriter
from core.kb_index_store import (
    INDEX_META_FILENAME,
    KBIndexStore,
    reset_singletons,
)
from core.kb_index_writer import Doc, DocResult, KBIndexWriter
from core.embed_retry import embed_batch_with_retry
from core.logger import get_logger

_logger = get_logger(__name__)


# ── 公开 API:写入(向后兼容 shim) ────────────────────────────────────────────
# 历史上 50+ tests + services / api 直接 import 这些函数。PR-3 把实现
# 迁到 ``KBIndexWriter``,这里保留 thin shim 委派过去 —— 等所有 caller
# 迁到新 API 之后再删掉。项目硬改名惯例会做,但"一次 PR 全删"的副作用
# 是测试全红;分阶段做符合 issue #155 / #158 / #167 的一贯做法。


def index_document(
    kb_id: str, doc_id: str, text: str, source_name: str = "",
    by_page=None, by_layout=None,
) -> Optional[DocResult]:
    """[shim → KBIndexWriter.index_documents] 单文档入口。

    单篇路径在 PR-3 已"折叠"进 ``index_documents([doc])`` —— 旧契约
    ``return None`` 折叠为 ``return DocResult``(首个元素)。如果调用方
    期望 ``return None``(旧习惯),可继续忽略返回值。
    """
    if not text or len(text) < 20:
        # 沿袭旧契约:"if not text or len(text) < 20: return",不抛也不写
        return None
    docs = [Doc(
        doc_id=doc_id, text=text, source_name=source_name,
        by_page=by_page, by_layout=by_layout,
    )]
    results = KBIndexWriter(kb_id).index_documents(docs)
    return results[0] if results else None


def index_documents_batch(
    kb_id: str,
    docs: list,  # [(doc_id, text, source_name, by_page?, by_layout?)]
    progress_callback=None,
) -> list[DocResult]:
    """[shim → KBIndexWriter.index_documents] 批量入口。

    输入 ``docs`` 沿用旧 3/4/5 元组格式(``by_page`` / ``by_layout`` 可选);
    内部转 ``Doc`` dataclass。``progress_callback`` 旧契约是
    ``(current, total, doc_name)``,PR-3 的 ``index_documents`` 不暴露这
    个回调(per-doc 进度走 ``KbIndexStatusWriter.note_in_flight`` /
    ``advance`` / ``fail_doc``)—— shim 自己包一层 progress_callback。

    旧契约细节(影响 test 行为):
      - **callback 在 early-return 之前调**:``text < 20`` 字符的 doc
        也会触发 ``progress_callback``(只是不进 embed)—— 旧代码里
        callback 在 ``if not text or len(text) < 20: continue`` **之前**
        就调了。``doc_service._on_progress`` 依赖这个语义把"短文稿"
        也算 done。这里保留这一行为(逐 doc 循环 + 预调 callback)。
      - ``current`` 是 1-based 已完成计数(旧代码 ``for idx, item in
        enumerate(docs, 1): progress_callback(idx, ...)")。
    """
    coerced = []
    for item in docs:
        doc_id, text, source_name = item[0], item[1], item[2]
        by_page = item[3] if len(item) > 3 else None
        by_layout = item[4] if len(item) > 4 else None
        coerced.append(Doc(
            doc_id=doc_id, text=text, source_name=source_name,
            by_page=by_page, by_layout=by_layout,
        ))

    if progress_callback is None:
        # 无 callback 路径:直接调 KBIndexWriter.index_documents,最便宜。
        return KBIndexWriter(kb_id).index_documents(coerced)

    # 有 callback 路径:逐 doc 循环,每个 doc 之前先调 callback(对齐旧契约)。
    # 这样 ``text < 20`` 的"短文稿"也会触发 callback —— 旧 ``_on_progress``
    # 把它们算 embedded,这里跟着算。
    kb_status_writer = KbIndexStatusWriter(kb_id, total=len(coerced))
    kb_status_writer.begin()
    failed_results: list[DocResult] = []
    writer = KBIndexWriter(kb_id)
    for idx, doc in enumerate(coerced, 1):
        progress_callback(idx, len(coerced), doc.source_name or doc.doc_id)
        result = writer.index_documents([doc])[0]
        if result.status == "failed":
            failed_results.append(result)
    if failed_results:
        kb_status_writer.finish(failed=[
            (r.doc_id, r.error or "未知失败") for r in failed_results
        ])
    else:
        kb_status_writer.finish()
    return writer.index_documents(coerced)


def remove_document(kb_id: str, doc_id: str) -> None:
    """[shim → KBIndexStore.remove_doc] 委派存储层。"""
    KBIndexStore.open(kb_id).remove_doc(doc_id)


def rebuild_kb_index(
    kb_id: str,
    progress_callback=None,
    *,
    kb_status_writer: Optional[KbIndexStatusWriter] = None,
) -> None:
    """[shim → KBIndexWriter.rebuild_kb_index] 2-phase 编排。

    旧契约:rebuild 启动 → ``KbIndexStatusWriter.begin()`` 写 building +
    progress=0,然后跑 phase 1/2;终态由 writer ``finish()`` 一次写。
    PR-3 的 KBIndexWriter 不调 begin()(issue #155: caller 独家承担),
    所以本 shim 自己 ``begin()``,然后把 writer 注入 KBIndexWriter。
    """
    if kb_status_writer is None:
        kb_status_writer = KbIndexStatusWriter(kb_id)
        kb_status_writer.begin()
    KBIndexWriter(kb_id).rebuild_kb_index(progress_callback=progress_callback)


# ── Backward-compat:50+ tests 直接 import 私有符号 ───────────────────────────
# 这些符号的实现迁移到了 core.kb_index_store / core.chunk_layout_mapper /
# core.kb_index_writer;这里保留同义入口让 tests 不破,内部一律委派新
# 模块。所有 shim 都标 [shim → xxx] 让 grep 一眼能定位到新位置。


def _vectors_dir(kb_id: str):
    """[shim → KBIndexStore.open(kb_id)._vectors_dir]"""
    return KBIndexStore.open(kb_id)._vectors_dir()


def _index_meta_path(kb_id: str):
    """[shim → KBIndexStore.open(kb_id)._index_meta_path]"""
    return KBIndexStore.open(kb_id)._index_meta_path()


def _read_index_meta(kb_id: str) -> Optional[dict]:
    """[shim → KBIndexStore.open(kb_id).get_meta]"""
    return KBIndexStore.open(kb_id).get_meta()


def _assert_kb_embedding_system_matches(
    kb_id: str, *, model_id: str = "BAAI/bge-m3", dim: int = 1024,
) -> None:
    """[shim → KBIndexStore.open(kb_id).assert_embedding_system_matches]"""
    KBIndexStore.open(kb_id).assert_embedding_system_matches(
        model_id=model_id, dim=dim,
    )


def _write_index_meta(
    kb_id: str, *, model_id: str = "BAAI/bge-m3", dim: int = 1024,
    created_at: Optional[str] = None, force: bool = False,
) -> None:
    """[shim → KBIndexStore.open(kb_id)._write_index_meta]"""
    KBIndexStore.open(kb_id)._write_index_meta(
        model_id=model_id, dim=dim, created_at=created_at, force=force,
    )


def _create_index(dim: int = 1024):
    """[shim → KBIndexStore.open(...)._create_index] 创建空 FAISS HNSW 索引。"""
    return KBIndexStore.open("__create_index_dummy__")._create_index(dim)


def _load_index(kb_id: str):
    """[shim → KBIndexStore.open(kb_id)._load_index] 从磁盘加载。"""
    return KBIndexStore.open(kb_id)._load_index()


def _persist(kb_id: str, index) -> None:
    """[shim → KBIndexStore.open(kb_id)._persist] 持久化。

    保留模块级函数:tests 用 ``mock.patch("core.index_manager._persist")``
    拦截失败路径。patch 的是符号 ``_persist``,本 shim 还在模块级,
    patch 仍生效。
    """
    KBIndexStore.open(kb_id)._persist(index)


def _save_doc_vectors(
    kb_id: str, doc_id: str, nodes: list, embeddings: list,
) -> None:
    """[shim → KBIndexStore.open(kb_id)._save_doc_vectors] 落 .npy + _nodes.json。"""
    KBIndexStore.open(kb_id)._save_doc_vectors(doc_id, nodes, embeddings)


def _cleanup_doc_vectors(kb_id: str, doc_id: str) -> None:
    """[shim → KBIndexStore.open(kb_id)._cleanup_doc_vectors]"""
    KBIndexStore.open(kb_id)._cleanup_doc_vectors(doc_id)


def _rebuild_from_vectors(
    kb_id: str, doc_ids: list[str], progress_callback=None,
) -> None:
    """[shim → KBIndexStore.open(kb_id).rebuild_from_vectors]"""
    KBIndexStore.open(kb_id).rebuild_from_vectors(
        doc_ids, progress_callback=progress_callback,
    )


def _split_document(doc):
    """[shim → core.kb_index_writer._split_document] 分块器选择。"""
    from core.kb_index_writer import _split_document as _impl
    return _impl(doc)


def _has_markdown_headings(text: str) -> bool:
    """[shim → core.kb_index_writer._has_markdown_headings]"""
    from core.kb_index_writer import _has_markdown_headings as _impl
    return _impl(text)


def _enrich_chunk_metadata(nodes: list, doc_id: str, source_name: str) -> None:
    """[shim → core.kb_index_writer._enrich_chunk_metadata]"""
    from core.kb_index_writer import _enrich_chunk_metadata as _impl
    _impl(nodes, doc_id, source_name)


def _chunk_prefix(text: str, max_chars: int = 200) -> str:
    """[shim → core.kb_index_writer._chunk_prefix]"""
    from core.kb_index_writer import _chunk_prefix as _impl
    return _impl(text, max_chars)


def _inject_page_number(nodes: list, by_page) -> None:
    """[shim → core.kb_index_writer._inject_page_number]"""
    from core.kb_index_writer import _inject_page_number as _impl
    _impl(nodes, by_page)


def _find_chunk_block_range(chunk_text: str, page_blocks: list):
    """[shim → core.chunk_layout_mapper.map_chunk_to_blocks]

    旧契约接 ``page_blocks``(单页 blocks 列表),新契约接 ``page_layout``
    (单页 layout 对象)。这里是薄壳:从 blocks 临时包一个 layout 对象
    再调新 API。tests 已经在迁到 map_chunk_to_blocks(page_layout) 形态;
    本 shim 仅为旧 test_inject_block_range_* 家族留活路。
    """
    from core.parse_document import PageLayout
    layout = PageLayout(page=0, width=0, height=0, blocks=page_blocks)
    return map_chunk_to_blocks(chunk_text, layout)


def _normalize_layout(by_layout):
    """[shim → core.chunk_layout_mapper.normalize_layout]"""
    from core.chunk_layout_mapper import normalize_layout
    return normalize_layout(by_layout)


def _inject_block_range(nodes: list, by_page=None, by_layout=None) -> list:
    """[shim → core.kb_index_writer._inject_block_range]

    by_page 参数保留为位置参数(旧契约),新 ``_inject_block_range`` 只接
    ``by_layout``。这里忽略 ``by_page``(注入路径不依赖 by_page,详见
    core.index_manager 旧版 docstring: "by_page:预留接口、当前未使用")。

    真实定义在 :mod:`core.kb_index_writer`——issue #169 / PR-3 决策
    "for-all-nodes inject" 循环留在 Writer(metadata 富化)。
    """
    from core.kb_index_writer import _inject_block_range as _impl
    return _impl(nodes, by_layout)


def map_chunk_to_blocks(chunk_text, page_layout):
    """[re-export → core.chunk_layout_mapper.map_chunk_to_blocks]"""
    from core.chunk_layout_mapper import map_chunk_to_blocks as _impl
    return _impl(chunk_text, page_layout)


# ── 公开 API:读 ──────────────────────────────────────────────────────────────


def get_kb_index(kb_id: str):
    """获取 KB 的 ``VectorStoreIndex``(加载或创建,带内存缓存)。

    委托 ``KBIndexStore.open(kb_id)._get_index()``——同一 kb_id 跨调用方
    共享同一 ``_index_cache`` 实例。保留旧"读也持锁"语义:``acquire_write_lock``
    把整次读包起来,跟 ``add_doc`` 写路径串行化,防止读到正在 persist 的
    半完成 FAISS。
    """
    store = KBIndexStore.open(kb_id)
    with store.acquire_write_lock():
        return store._get_index()


def get_kb_index_built(kb_id: str) -> bool:
    """检查 KB 是否可被向量检索。

    ADR-0002 单真相:本函数只读 KB 元数据中的 ``kb.index_status`` 字段。
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
    因为它们的 ``_index_cache`` 是实例属性。
    """
    reset_singletons()


def search(
    kb_ids: list[str], query: str, top_k: int = 5, use_reranker: bool = True,
) -> list[dict]:
    """跨 KB 向量搜索。

    返回格式与旧版 ``vec_search()`` 兼容:
    ``[{source, kb_id, doc_id, content, doc_source, relevance, page_number, block_range}, ...]``

    当 reranker 可用时,用 cross-encoder 对候选结果重排序提升精度。
    """
    if not query or not kb_ids:
        return []

    from core.settings import get_embed_model, get_gpu_inference_lock, run_reranker
    get_embed_model()

    # 一次 query embedding(原代码在每个 KB retriever.retrieve 内各做一次,
    # 现在提到 search 入口做一次——同一 embedder、同一 query、同一结果,
    # 跨 KB 复用)。Query embedding 不走 ``embed_batch_with_retry``——
    # ADR-0007 §2:查询路径零附加重试。
    query_embedding = Settings.embed_model.get_query_embedding(query)

    gpu_lock = get_gpu_inference_lock()

    with gpu_lock:
        all_nodes: list[NodeWithScore] = []
        for kb_id in kb_ids:
            if not get_kb_index_built(kb_id):
                continue
            try:
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
        all_nodes = all_nodes[: top_k * 2]

        if use_reranker:
            try:
                reranked = run_reranker(all_nodes, query)
                if reranked:
                    all_nodes = reranked
            except Exception as e:
                _logger.warning("reranker failed in search, using raw ranking: %s", e)

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
            "page_number": meta.get("page_number"),
            "block_range": meta.get("block_range"),
            "relevance": round(node.get_score() or 0, 4),
        })

    return hits
