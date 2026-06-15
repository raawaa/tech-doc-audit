"""KB VectorStoreIndex 生命周期管理。

每个知识库（KB）对应一个独立的 FAISS 索引文件。
索引加载后缓存在内存中，避免重复读盘。
"""

import os
import shutil
from pathlib import Path
from typing import Optional

from core.logger import get_logger

_logger = get_logger(__name__)

import faiss
from llama_index.core import VectorStoreIndex, StorageContext, Document, Settings
from llama_index.core.node_parser import SentenceSplitter, MarkdownNodeParser
from llama_index.vector_stores.faiss import FaissVectorStore

from core.settings import get_embed_model
from core.text_extraction import extract_text as _extract_text

DATA_DIR = Path(os.environ.get("AUDIT_DATA_DIR", "./data"))

# 内存缓存: kb_id -> VectorStoreIndex
_index_cache: dict[str, VectorStoreIndex] = {}


# ── 内部路径 ────────────────────────────────────────────────────────────────────

def _vectors_dir(kb_id: str) -> Path:
    return DATA_DIR / "kbs" / kb_id / "vectors"


def _ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)


# ── 索引创建 / 加载 / 缓存 ─────────────────────────────────────────────────────

def _create_index(dim: int = 1024) -> VectorStoreIndex:
    """创建新的空 FAISS 索引（FlatIP: Inner Product，适用于已 normalize 的向量）。"""
    # 确保 embedding 模型已初始化
    get_embed_model()
    faiss_index = faiss.IndexFlatIP(dim)
    vector_store = FaissVectorStore(faiss_index=faiss_index)
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


# ── 文档索引 ────────────────────────────────────────────────────────────────────

def index_document(kb_id: str, doc_id: str, text: str, source_name: str = ""):
    """对文档文本分块 → embedding → 写入 KB 索引。

    优先使用 MarkdownNodeParser 按标题层级切块（适合带 # 标题的文本），
    降级到 SentenceSplitter 按 token 数切块。
    """
    if not text or len(text) < 20:
        return

    index = get_kb_index(kb_id)

    doc = Document(
        text=text,
        metadata={"doc_id": doc_id, "source": source_name or doc_id},
    )

    # 检测文本是否包含 Markdown 标题，决定使用哪种分块器
    if _has_markdown_headings(text):
        splitter = MarkdownNodeParser()
    else:
        splitter = SentenceSplitter(chunk_size=512, chunk_overlap=50)
    nodes = splitter.get_nodes_from_documents([doc])
    index.insert_nodes(nodes)

    _persist(kb_id, index)


def _has_markdown_headings(text: str) -> bool:
    """快速检测文本是否包含 Markdown 标题层级。"""
    import re
    # 检查是否包含至少 2 个带层级的 Markdown 标题（# 或 ## 或 ###）
    return bool(re.search(r"^#{2,6}\s+\S", text, re.MULTILINE))


def remove_document(kb_id: str, doc_id: str):
    """从 KB 索引中删除指定文档的所有节点。

    IndexFlatIP 不支持删除单个向量，因此采用重建策略：
    清空缓存 → 删除向量目录 → 重新索引除目标文档外的所有文档。
    """
    _index_cache.pop(kb_id, None)

    vectors_dir = _vectors_dir(kb_id)
    if vectors_dir.exists():
        shutil.rmtree(str(vectors_dir))

    import storage.kb_repo as kb_repo
    kb = kb_repo.get(kb_id)
    if not kb:
        return

    remaining_ids = [did for did in kb.document_ids if did != doc_id]
    if not remaining_ids:
        return  # 已无其他文档，无需重建

    index = _create_index()
    _index_cache[kb_id] = index

    from storage.doc_repo import get_doc

    for did in remaining_ids:
        doc = get_doc(kb_id, did)
        if doc and doc.file_path and Path(doc.file_path).exists():
            try:
                text = _extract_text(doc.file_path)
                if text:
                    index_document(kb_id, did, text)
            except Exception as e:
                print(f"  [skip] {did}: {e}")

    _persist(kb_id, index)


def rebuild_kb_index(kb_id: str):
    """重建 KB 索引（清除旧索引，重新索引所有文档）。"""
    _index_cache.pop(kb_id, None)

    vectors_dir = _vectors_dir(kb_id)
    if vectors_dir.exists():
        shutil.rmtree(str(vectors_dir))

    import storage.kb_repo as kb_repo
    kb = kb_repo.get(kb_id)
    if not kb:
        return

    index = _create_index()
    _index_cache[kb_id] = index

    from storage.doc_repo import get_doc

    for doc_id in kb.document_ids:
        doc = get_doc(kb_id, doc_id)
        if doc and doc.file_path and Path(doc.file_path).exists():
            try:
                text = _extract_text(doc.file_path)
                if text:
                    index_document(kb_id, doc_id, text)
            except Exception as e:
                print(f"  [skip] {doc_id}: {e}")

    _persist(kb_id, index)


def get_kb_index_built(kb_id: str) -> bool:
    """检查 KB 是否已有索引（default__vector_store.json 文件存在）。"""
    return (_vectors_dir(kb_id) / "default__vector_store.json").exists()


# ── 搜索 ────────────────────────────────────────────────────────────────────────

def search(kb_ids: list[str], query: str, top_k: int = 5) -> list[dict]:
    """跨 KB 向量搜索。

    返回格式与旧版 vec_search() 兼容：
    [{source, kb_id, doc_id, content, doc_source, relevance}, ...]
    """
    if not query or not kb_ids:
        return []

    hits = []
    # 确保 embed_model 已加载，防止 LlamaIndex 默认解析到 OpenAI
    get_embed_model()
    for kb_id in kb_ids:
        if not get_kb_index_built(kb_id):
            continue
        try:
            index = get_kb_index(kb_id)
            retriever = index.as_retriever(similarity_top_k=top_k)
            nodes = retriever.retrieve(query)
            for node in nodes:
                meta = node.metadata or {}
                hits.append({
                    "source": "vec_search",
                    "kb_id": kb_id,
                    "doc_id": meta.get("doc_id", ""),
                    "content": node.text,
                    "doc_source": meta.get("source", ""),
                    "relevance": round(node.get_score() or 0, 4),
                })
        except Exception as e:
            _logger.warning("vector search failed for kb %s: %s", kb_id, e)
            continue

    hits.sort(key=lambda x: -x["relevance"])
    return hits[:top_k]
