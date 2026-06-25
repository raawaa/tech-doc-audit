"""KB VectorStoreIndex 生命周期管理。

每个知识库（KB）对应一个独立的 FAISS 索引文件。
索引加载后缓存在内存中，避免重复读盘。
"""

import gc
import json as _json
import os
import threading
import shutil
from pathlib import Path
from typing import Optional

import numpy as np
from tenacity import retry, stop_after_attempt, wait_exponential

from core.logger import get_logger

_logger = get_logger(__name__)

import faiss
from llama_index.core import VectorStoreIndex, StorageContext, Document, Settings
from llama_index.core.node_parser import SentenceSplitter, MarkdownNodeParser
from llama_index.core.schema import TextNode
from llama_index.vector_stores.faiss import FaissVectorStore

from core.settings import get_embed_model, get_gpu_inference_lock
from core.text_extraction import extract_text as _extract_text

DATA_DIR = Path(os.environ.get("AUDIT_DATA_DIR", "./data"))

# 内存缓存: kb_id -> VectorStoreIndex
_index_cache: dict[str, VectorStoreIndex] = {}

# per-KB 可重入锁：防止并发索引同一 KB 导致 FAISS 死锁
# 使用 RLock 因为 rebuild_kb_index/remove_document 会递归调用 index_document
_index_locks: dict[str, threading.RLock] = {}
_index_locks_lock = threading.Lock()


def _get_index_lock(kb_id: str) -> threading.RLock:
    """获取 KB 对应的可重入锁（线程安全创建）。"""
    with _index_locks_lock:
        if kb_id not in _index_locks:
            _index_locks[kb_id] = threading.RLock()
        return _index_locks[kb_id]


# ── 内部路径 ────────────────────────────────────────────────────────────────────

def _vectors_dir(kb_id: str) -> Path:
    return DATA_DIR / "kbs" / kb_id / "vectors"


def _ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)


# ── 索引创建 / 加载 / 缓存 ─────────────────────────────────────────────────────

def _create_index(dim: int = 1024) -> VectorStoreIndex:
    """创建新的空 FAISS 索引（HNSW，支持高效 ANN 搜索）。

    注意：不套 IndexIDMap，因为 llama-index 的 FaissVectorStore.add()
    只使用 faiss `add()` 而非 `add_with_ids()`，IDMap 会导致崩溃。
    向量级删除（remove_document）降级到全量重建路径，代码已支持。
    """
    # 确保 embedding 模型已初始化
    get_embed_model()
    # HNSW: 高效的近似最近邻索引，O(log n) 搜索
    hnsw_index = faiss.IndexHNSWFlat(dim, 32)
    hnsw_index.hnsw.efConstruction = 200  # 建图质量（越大越准）
    hnsw_index.hnsw.efSearch = 64         # 搜索精度
    vector_store = FaissVectorStore(faiss_index=hnsw_index)
    storage_context = StorageContext.from_defaults(vector_store=vector_store)
    # 直接用 VectorStoreIndex 构造，传入空 nodes + storage_context（含 FaissVectorStore）
    index = VectorStoreIndex(
        nodes=[],
        storage_context=storage_context,
        embed_model=Settings.embed_model,
    )
    return index


def _load_index(kb_id: str) -> Optional[VectorStoreIndex]:
    """从磁盘加载已有 FAISS 索引。"""
    vectors_dir = _vectors_dir(kb_id)
    store_file = vectors_dir / "default__vector_store.json"
    if not store_file.exists():
        return None
    try:
        get_embed_model()
        faiss_index = faiss.read_index(str(store_file))
        vector_store = FaissVectorStore(faiss_index=faiss_index)

        from llama_index.core.storage.docstore import SimpleDocumentStore
        from llama_index.core.storage.index_store import SimpleIndexStore
        docstore = SimpleDocumentStore.from_persist_dir(str(vectors_dir))
        index_store = SimpleIndexStore.from_persist_dir(str(vectors_dir))

        storage_context = StorageContext.from_defaults(
            vector_store=vector_store,
            docstore=docstore,
            index_store=index_store,
        )

        # 从已加载的 index_store 中获取已有的 index_struct
        index_struct = None
        for is_ in index_store.index_structs():
            index_struct = is_
            break

        index = VectorStoreIndex(
            nodes=[],
            index_struct=index_struct,
            storage_context=storage_context,
            embed_model=Settings.embed_model,
        )
        return index
    except Exception as e:
        _logger.warning("failed to load index for kb %s: %s", kb_id, e)
        return None


def get_kb_index(kb_id: str) -> VectorStoreIndex:
    """获取 KB 的 VectorStoreIndex（加载或创建，带内存缓存）。"""
    if kb_id in _index_cache:
        return _index_cache[kb_id]
    index = _load_index(kb_id) or _create_index()
    _index_cache[kb_id] = index
    return index


def _persist(kb_id: str, index: VectorStoreIndex):
    """持久化 FAISS 索引 + docstore 到磁盘。"""
    vectors_dir = _vectors_dir(kb_id)
    _ensure_dir(vectors_dir)
    index.storage_context.persist(persist_dir=str(vectors_dir))


def clear_cache():
    """清空索引缓存（用于测试）。"""
    _index_cache.clear()


# ── 向量持久化辅助函数 ──────────────────────────────────────────────────────────

def _save_doc_vectors(kb_id: str, doc_id: str, nodes: list, embeddings: list):
    """保存文档的 embedding 向量和节点元数据到磁盘。

    每个文档保存两个文件：
    - {doc_id}.npy: float32 向量矩阵 (n_chunks, 1024)
    - {doc_id}_nodes.json: 节点元数据列表 [{node_id, text, metadata}, ...]

    先写 _nodes.json 再写 .npy：.npy 存在 ⇔ 向量缓存完整，重建时以此判断。
    写入顺序保证崩溃后不会出现「.npy 存在但 _nodes.json 缺失」的半完成状态。

    这些文件使索引重建时无需重新 embedding（纯 CPU 操作）。
    """
    vectors_dir = _vectors_dir(kb_id)
    _ensure_dir(vectors_dir)

    # 先写节点元数据（非原子写入可能崩溃残留，但 .npy 不存在时不会触发重建）
    nodes_data = []
    for node in nodes:
        nodes_data.append({
            "node_id": node.node_id,
            "text": node.text or "",
            "metadata": node.metadata or {},
        })
    nodes_file = vectors_dir / f"{doc_id}_nodes.json"
    nodes_tmp = vectors_dir / f"{doc_id}_nodes.json.tmp"
    nodes_tmp.write_text(_json.dumps(nodes_data, ensure_ascii=False))
    nodes_tmp.rename(nodes_file)

    # 后写向量（np.save 内部写临时文件 + rename，原子操作）
    vec_array = np.array(embeddings, dtype=np.float32)
    np.save(str(vectors_dir / f"{doc_id}.npy"), vec_array)


def _cleanup_doc_vectors(kb_id: str, doc_id: str):
    """删除文档的向量缓存文件。"""
    vectors_dir = _vectors_dir(kb_id)
    for suffix in [".npy", "_nodes.json"]:
        f = vectors_dir / f"{doc_id}{suffix}"
        if f.exists():
            f.unlink()


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    reraise=True,
)
def _embed_batch_with_retry(embed_model, texts: list[str]) -> list:
    """批量 embedding，瞬态错误自动重试（最多 3 次，指数退避）。

    可重试的错误：GPU 瞬态错误、CUDA 异常。
    不可重试的错误（如模型未加载、ValueError）会直接抛出。
    """
    return embed_model.get_text_embedding_batch(texts)


# ── 文档索引 ────────────────────────────────────────────────────────────────────

def index_document(kb_id: str, doc_id: str, text: str, source_name: str = ""):
    """对文档文本分块 → embedding → 写入 KB 索引 + 持久化向量。

    优先使用 MarkdownNodeParser 按标题层级切块（适合带 # 标题的文本），
    降级到 SentenceSplitter 按 token 数切块。
    embedding 结果同时保存为 .npy 文件，供后续快速重建。
    """
    if not text or len(text) < 20:
        return

    with _get_index_lock(kb_id):
        embed_model = get_embed_model()
        if embed_model is None:
            raise RuntimeError("Embedding model not loaded, cannot index document")

        index = get_kb_index(kb_id)

        doc = Document(
            text=text,
            id_=doc_id,
            metadata={"doc_id": doc_id, "source": source_name or doc_id},
        )

        nodes = _split_document(doc)
        _enrich_chunk_metadata(nodes, doc_id, source_name or doc_id)

        if not nodes:
            del doc
            return

        # 预 embedding：拿到向量引用后再插入索引，避免重复推理
        node_texts = [node.text or "" for node in nodes]
        with get_gpu_inference_lock():
            embeddings = _embed_batch_with_retry(embed_model, node_texts)
        for node, emb in zip(nodes, embeddings):
            node.embedding = emb

        # 持久化向量（索引重建时无需 GPU）
        _save_doc_vectors(kb_id, doc_id, nodes, embeddings)

        # 插入索引（节点已有 embedding，不会重复推理）
        index.insert_nodes(nodes)

        del doc, nodes, embeddings
        gc.collect()

        _persist(kb_id, index)


def _enrich_chunk_metadata(
    nodes: list,
    doc_id: str,
    source_name: str,
):
    """从文档分块文本中检测条款编号和章节标题，注入 node.metadata。

    使 FAISS 搜索结果能追溯到标准的某个具体条款（如 "CJJ101-2016 第 3.2.1 条"）。
    不在 text 中注入元数据，避免稀释 embedding 语义信号。
    """
    import re
    clause_re = re.compile(r"(\d+(?:\.\d+)*)")

    for node in nodes:
        text = node.text or ""
        if not text:
            continue

        # 检测条款编号（取最长的数字段，如 3.2.1 而非 3）
        nums = clause_re.findall(text)
        if nums:
            clause = max(nums, key=lambda n: n.count("."))
            node.metadata["clause_number"] = clause

        # 检测章节标题
        sec_match = re.search(r"^(#{1,6})\s+(.+)", text, re.MULTILINE)
        if sec_match:
            node.metadata["section_path"] = sec_match.group(2).strip()

        # 保证 doc_id / source 完整
        node.metadata.setdefault("doc_id", doc_id)
        node.metadata.setdefault("source", source_name)


def index_documents_batch(
    kb_id: str,
    docs: list[tuple[str, str, str]],
    progress_callback=None,
):
    """批量索引文档：分块 → 批量 embedding → 保存向量 → 写入 FAISS。

    每篇文档内部的所有 chunk 批量 embedding（利用 embed_batch_size 加速），
    embedding 结果持久化为 .npy 文件供后续快速重建。

    Args:
        kb_id: 知识库 ID。
        docs: [(doc_id, text, source_name), ...] 列表。
        progress_callback: 可选回调 (current, total, doc_name) → None。
    """
    if not docs:
        return

    with _get_index_lock(kb_id):
        embed_model = get_embed_model()
        if embed_model is None:
            raise RuntimeError("Embedding model not loaded, cannot index documents")

        index = get_kb_index(kb_id)
        total = len(docs)

        for i, (doc_id, text, source_name) in enumerate(docs, 1):
            if progress_callback:
                progress_callback(i, total, source_name or doc_id)

            if not text or len(text) < 20:
                continue

            doc = Document(
                text=text,
                id_=doc_id,
                metadata={"doc_id": doc_id, "source": source_name or doc_id},
            )
            nodes = _split_document(doc)
            _enrich_chunk_metadata(nodes, doc_id, source_name or doc_id)

            if not nodes:
                del doc
                continue

            # 批量 embedding 本稿件所有 chunk（GPU 锁内，失败自动重试）
            node_texts = [node.text or "" for node in nodes]
            try:
                with get_gpu_inference_lock():
                    embeddings = _embed_batch_with_retry(embed_model, node_texts)
            except Exception as e:
                _logger.error("embedding failed for doc %s after retries: %s", doc_id, e)
                del doc, nodes
                raise

            for node, emb in zip(nodes, embeddings):
                node.embedding = emb

            # 持久化向量（索引重建时无需 GPU）
            _save_doc_vectors(kb_id, doc_id, nodes, embeddings)

            # 插入索引（节点已有 embedding）
            index.insert_nodes(nodes)

            del doc, nodes, embeddings
            if i % 5 == 0:
                gc.collect()

        _persist(kb_id, index)


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
    # 检查是否包含至少 2 个带层级的 Markdown 标题（# 或 ## 或 ###）
    return bool(re.search(r"^#{1,6}\s+\S", text, re.MULTILINE))


def remove_document(kb_id: str, doc_id: str):
    """从 KB 索引中删除指定文档的所有节点。

    优先尝试向量级删除（delete_ref_doc），降级到从已保存的 .npy 向量
    重建索引（无需 GPU 重新 embedding）。
    """
    with _get_index_lock(kb_id):
        # 快速路径：通过 delete_ref_doc 直接从索引删除
        try:
            index = get_kb_index(kb_id)
            if hasattr(index.vector_store, '_faiss_index') and hasattr(index.vector_store._faiss_index, 'remove_ids'):
                index.delete_ref_doc(doc_id, delete_from_docstore=True)
                _persist(kb_id, index)
                _cleanup_doc_vectors(kb_id, doc_id)
                _logger.info("removed doc %s from kb %s via delete_ref_doc", doc_id, kb_id)
                return
        except Exception as e:
            _logger.warning("vector-level deletion failed for %s/%s (%s), fallback to rebuild from cached vectors", kb_id, doc_id, e)

        # 降级路径：从已保存的 .npy 向量重建索引（无需 GPU）
        _logger.info("rebuilding kb %s index from cached vectors after removing doc %s", kb_id, doc_id)
        _index_cache.pop(kb_id, None)

        import storage.kb_repo as kb_repo
        kb = kb_repo.get(kb_id)
        if not kb:
            return

        remaining_ids = [did for did in kb.document_ids if did != doc_id]
        vectors_dir = _vectors_dir(kb_id)

        if not remaining_ids:
            # 无剩余文档，清理所有索引文件
            if vectors_dir.exists():
                shutil.rmtree(str(vectors_dir))
            return

        # 从向量缓存重建（_rebuild_from_vectors 成功后 _persist 会覆盖旧 FAISS 文件；
        # 失败则旧文件仍在，不会丢失索引）
        _rebuild_from_vectors(kb_id, remaining_ids)
        _cleanup_doc_vectors(kb_id, doc_id)


def _rebuild_from_vectors(kb_id: str, doc_ids: list[str], progress_callback=None):
    """从已保存的 .npy 向量文件重建 FAISS 索引（纯 CPU，无需 GPU）。

    从 {doc_id}.npy 加载向量，从 {doc_id}_nodes.json 加载节点文本/元数据，
    重建 TextNode 并插入新索引。

    Args:
        progress_callback: 可选回调 (current, total, doc_name) → None。
    """
    vectors_dir = _vectors_dir(kb_id)

    new_index = _create_index()
    _index_cache[kb_id] = new_index

    total = len(doc_ids)
    loaded = 0
    for i, doc_id in enumerate(doc_ids, 1):
        vec_file = vectors_dir / f"{doc_id}.npy"
        nodes_file = vectors_dir / f"{doc_id}_nodes.json"

        if not vec_file.exists():
            _logger.warning("vector cache missing for doc %s, will need re-embedding", doc_id)
            if progress_callback:
                progress_callback(i, total, doc_id)
            continue

        vectors = np.load(str(vec_file))

        # 加载节点元数据（文本 + metadata）
        if not nodes_file.exists():
            _logger.error(
                "vector cache incomplete for doc %s: .npy exists but _nodes.json missing. "
                "This doc will be skipped in rebuild. Run `index rebuild --kb-id %s` to re-embed.",
                doc_id, kb_id,
            )
            if progress_callback:
                progress_callback(i, total, doc_id)
            continue

        try:
            nodes_data = _json.loads(nodes_file.read_text())
        except Exception as e:
            _logger.error(
                "failed to load nodes metadata for doc %s (%s). "
                "This doc will be skipped in rebuild.",
                doc_id, e,
            )
            if progress_callback:
                progress_callback(i, total, doc_id)
            continue

        if len(nodes_data) != len(vectors):
            _logger.error(
                "node count mismatch for doc %s: %d nodes in _nodes.json vs %d vectors in .npy. "
                "This doc will be skipped in rebuild.",
                doc_id, len(nodes_data), len(vectors),
            )
            if progress_callback:
                progress_callback(i, total, doc_id)
            continue

        nodes = []
        for j, vec in enumerate(vectors):
            nd = nodes_data[j]
            node = TextNode(
                text=nd.get("text", ""),
                id_=nd.get("node_id", f"{doc_id}_{j}"),
                metadata=nd.get("metadata", {}),
                embedding=vec.tolist() if hasattr(vec, 'tolist') else list(vec),
            )
            nodes.append(node)

        new_index.insert_nodes(nodes)
        loaded += 1

        if progress_callback:
            progress_callback(i, total, doc_id)

        if loaded % 20 == 0:
            gc.collect()

    _persist(kb_id, new_index)
    _logger.info("rebuilt index for kb %s from %d/%d docs (cached vectors)", kb_id, loaded, len(doc_ids))


def rebuild_kb_index(kb_id: str, progress_callback=None):
    """重建 KB 索引。

    优先从已保存的 .npy 向量重建（纯 CPU，秒级），
    向量缺失时降级到重新提取文本 + embedding（需要 GPU）。

    Args:
        kb_id: 知识库 ID。
        progress_callback: 可选回调 (current_index, total, doc_name) → None，
                           每处理完一篇文档后调用，用于外部汇报进度。
    """
    with _get_index_lock(kb_id):
        _index_cache.pop(kb_id, None)

        import storage.kb_repo as kb_repo
        kb = kb_repo.get(kb_id)
        if not kb:
            return

        vectors_dir = _vectors_dir(kb_id)
        doc_ids = kb.document_ids

        # 区分有/无向量缓存的文档
        with_vectors = []
        without_vectors = []
        for doc_id in doc_ids:
            if (vectors_dir / f"{doc_id}.npy").exists():
                with_vectors.append(doc_id)
            else:
                without_vectors.append(doc_id)

        # 阶段 1：从向量缓存快速重建（无需 GPU）
        if with_vectors:
            _logger.info("rebuilding kb %s from %d cached vectors (fast path)", kb_id, len(with_vectors))
            # 删除旧的 llama-index 持久化文件（_rebuild_from_vectors 成功后会 _persist 写回新的）
            old_store = vectors_dir / "default__vector_store.json"
            if old_store.exists():
                old_store.unlink()
            for pattern in ["docstore.json", "index_store.json", "graph_store.json"]:
                p = vectors_dir / pattern
                if p.exists():
                    p.unlink()

            _rebuild_from_vectors(kb_id, with_vectors, progress_callback=progress_callback)

        # 阶段 2：重新提取文本 + embedding（向量缓存缺失的文档）
        if without_vectors:
            _logger.info("rebuilding kb %s: %d docs need re-embedding (slow path)", kb_id, len(without_vectors))
            from storage.doc_repo import get_doc
            total = len(without_vectors)
            for i, doc_id in enumerate(without_vectors, 1):
                doc = get_doc(kb_id, doc_id)
                doc_name = doc.original_name if doc and doc.original_name else doc_id
                if progress_callback:
                    progress_callback(i, total, doc_name)
                if doc and doc.file_path and Path(doc.file_path).exists():
                    try:
                        text = _extract_text(doc.file_path)
                        if text:
                            index_document(kb_id, doc_id, text)
                            # index_document 内部已调用 _persist + _save_doc_vectors
                    except Exception as e:
                        _logger.warning("  [skip] %s: %s", doc_id, e)

        if not with_vectors and not without_vectors:
            # 无文档，清理
            if vectors_dir.exists():
                shutil.rmtree(str(vectors_dir))


def get_kb_index_built(kb_id: str) -> bool:
    """检查 KB 是否已有索引（default__vector_store.json 文件存在）。"""
    return (_vectors_dir(kb_id) / "default__vector_store.json").exists()


# ── 搜索 ────────────────────────────────────────────────────────────────────────

def search(kb_ids: list[str], query: str, top_k: int = 5, use_reranker: bool = True) -> list[dict]:
    """跨 KB 向量搜索。

    返回格式与旧版 vec_search() 兼容：
    [{source, kb_id, doc_id, content, doc_source, relevance}, ...]

    当 reranker 可用时，用 cross-encoder 对候选结果重排序提升精度。
    """
    if not query or not kb_ids:
        return []

    # 确保 embed_model 已加载，防止 LlamaIndex 默认解析到 OpenAI
    get_embed_model()

    from core.settings import get_gpu_inference_lock
    gpu_lock = get_gpu_inference_lock()

    # 将整个 GPU 相关操作置于锁内。HuggingFaceEmbedding 和
    # SentenceTransformerRerank 的 forward 非线程安全，并发调用会各自
    # 分配完整激活张量撑爆显存。此锁确保同时只有一个进程执行模型前向传播，
    # LLM 调用（DeepSeek API 不走 GPU）不受影响仍可并行。
    with gpu_lock:
        # 先收集 NodeWithScore 对象，保留完整 score 信息
        from llama_index.core.schema import NodeWithScore
        all_nodes: list[NodeWithScore] = []
        for kb_id in kb_ids:
            if not get_kb_index_built(kb_id):
                continue
            try:
                with _get_index_lock(kb_id):
                    index = get_kb_index(kb_id)
                    retriever = index.as_retriever(similarity_top_k=top_k)
                    nodes = retriever.retrieve(query)
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

        # ── Reranker 重排序（按需加载→推理→卸载）────────────────────────
        if use_reranker:
            try:
                from core.settings import run_reranker
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
            "relevance": round(node.get_score() or 0, 4),
        })

    return hits
