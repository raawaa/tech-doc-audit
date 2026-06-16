import os
import threading
from typing import Optional

import pdfplumber

from models.document import KBDocument
from models.knowledge_base import KnowledgeBase
import storage.doc_repo as doc_repo
import storage.kb_repo as kb_repo
from services.vector_search import index_document as _index_vec
from core.logger import get_logger

_logger = get_logger(__name__)

# per-KB 锁：保护 get(kb) → modify → update(kb) 原子性
# 防止 import_document 追加 document_ids 与异步线程更新状态交错
_doc_service_locks: dict[str, threading.Lock] = {}
_doc_service_locks_lock = threading.Lock()


def _get_lock(kb_id: str) -> threading.Lock:
    with _doc_service_locks_lock:
        if kb_id not in _doc_service_locks:
            _doc_service_locks[kb_id] = threading.Lock()
        return _doc_service_locks[kb_id]


def _detect_file_type(filename: str) -> Optional[str]:
    ext = os.path.splitext(filename)[1].lower()
    mapping = {".pdf": "pdf", ".doc": "doc", ".docx": "docx", ".md": "md"}
    return mapping.get(ext)


def import_document(
    kb_id: str,
    original_name: str,
    content: bytes,
    async_index: bool = False,
) -> KBDocument:
    """导入单篇文档。

    Args:
        kb_id: 知识库 ID。
        original_name: 原始文件名。
        content: 文件内容字节。
        async_index: True 则后台异步索引（上传即返回），
                     False 则同步等待索引完成（CLI 等场景）。
    """
    file_type = _detect_file_type(original_name)
    if not file_type:
        raise ValueError(f"不支持的文件格式: {original_name}")

    doc = doc_repo.save_doc(kb_id, original_name, content, file_type)

    doc.index_status = "ready"
    doc_repo._save_doc_meta(doc)

    # 提取 PDF 页数等元数据
    if file_type == "pdf":
        try:
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(content)
                tmp_path = tmp.name
            with pdfplumber.open(tmp_path) as pdf:
                doc.page_count = len(pdf.pages)
            os.unlink(tmp_path)
        except Exception as e:
            _logger.warning("failed to extract page count for %s: %s", doc.id, e)

    # 更新知识库 document_ids（原子 get→modify→update，防与异步线程交错）
    with _get_lock(kb_id):
        kb = kb_repo.get(kb_id)
        if kb and doc.id not in kb.document_ids:
            kb.document_ids.append(doc.id)
            kb_repo.update(kb)

    # 向量索引
    if doc.file_path:
        if async_index:
            # 异步：后台线程索引，不阻塞 API 响应
            doc.index_status = "pending_index"
            doc_repo._save_doc_meta(doc)
            thread = threading.Thread(
                target=_index_single_doc_async,
                args=(kb_id, doc),
                daemon=True,
            )
            thread.start()
        else:
            # 同步：等待索引完成（CLI 等场景）
            try:
                _index_vec(kb_id, doc.id, doc.file_path)
            except Exception as e:
                _logger.warning("vector indexing failed for doc %s: %s", doc.id, e)

    return doc


def _index_single_doc_async(kb_id: str, doc: KBDocument):
    """后台索引单篇文档（由 import_document async_index=True 调用）。"""
    import storage.kb_repo as kb_repo

    # 获取最新 kb 并标记 building（原子 get→modify→update）
    with _get_lock(kb_id):
        kb = kb_repo.get(kb_id)
        if not kb:
            return
        kb.index_status = "building"
        kb.index_current_doc = doc.original_name
        kb_repo.update(kb)

    try:
        _index_vec(kb_id, doc.id, doc.file_path)
        doc.index_status = "ready"
    except Exception as e:
        _logger.warning("async indexing failed for doc %s: %s", doc.id, e)
        doc.index_status = "failed"
    doc_repo._save_doc_meta(doc)

    # 原子地更新完成状态（锁内 re-read，防覆盖并发 import 追加的 document_ids）
    with _get_lock(kb_id):
        kb = kb_repo.get(kb_id)
        if kb:
            kb.index_status = "ready"
            kb.index_current_doc = ""
            kb_repo.update(kb)


def batch_import_documents(
    kb_id: str,
    files: list[tuple[str, bytes]],
    async_index: bool = True,
) -> list[KBDocument]:
    """批量导入文档，可选择异步索引。

    Args:
        kb_id: 知识库 ID。
        files: [(original_name, content_bytes), ...]。
        async_index: True 则后台异步索引（立即返回），False 则同步等待。

    Returns:
        已保存的文档列表（不含向量索引结果）。
    """
    docs = []
    kb = kb_repo.get(kb_id)
    if not kb:
        raise ValueError(f"知识库不存在: {kb_id}")

    for original_name, content in files:
        file_type = _detect_file_type(original_name)
        if not file_type:
            _logger.warning("跳过不支持的文件: %s", original_name)
            continue

        doc = doc_repo.save_doc(kb_id, original_name, content, file_type)
        doc.index_status = "pending_index"
        doc_repo._save_doc_meta(doc)

        if doc.id not in kb.document_ids:
            kb.document_ids.append(doc.id)

        docs.append(doc)

    # 原子写入 document_ids（防与异步线程的 status 更新交错）
    with _get_lock(kb_id):
        kb_repo.update(kb)

    if async_index and docs:
        thread = threading.Thread(
            target=_batch_index_docs,
            args=(kb_id, docs),
            daemon=True,
        )
        thread.start()
    elif not async_index and docs:
        _batch_index_docs(kb_id, docs)

    return docs


def _batch_index_docs(kb_id: str, docs: list[KBDocument]):
    """后台批量索引文档（由 batch_import_documents 调用）。"""
    import storage.kb_repo as kb_repo

    # 获取最新 kb 并标记 building（原子周期）
    with _get_lock(kb_id):
        kb = kb_repo.get(kb_id)
        if not kb:
            return

        # 收集需要索引的文档
        texts = []
        doc_map = {doc.id: doc for doc in docs}
        for doc in docs:
            if doc.file_path and os.path.exists(doc.file_path):
                try:
                    with open(doc.file_path, "r", encoding="utf-8") as f:
                        text = f.read()
                    texts.append((doc.id, text, doc.original_name))
                except Exception as e:
                    _logger.warning("读取文档 %s 失败: %s", doc.id, e)
                    doc_map[doc.id].index_status = "failed"
                    doc_repo._save_doc_meta(doc_map[doc.id])

        if not texts:
            kb.index_status = "ready"
            kb_repo.update(kb)
            return

        kb.index_status = "building"
        kb_repo.update(kb)

    indexed_ids = set()

    def _on_progress(current: int, total: int, doc_name: str):
        # 锁内 re-read kb，防覆盖并发 import 追加的 document_ids
        with _get_lock(kb_id):
            inner_kb = kb_repo.get(kb_id)
            if not inner_kb:
                return
            inner_kb.index_progress = current / total
            inner_kb.index_current_doc = doc_name
            kb_repo.update(inner_kb)
        # 逐篇更新文档索引状态（文档元数据独立于 kb，无需锁）
        doc_id = texts[current - 1][0]
        if doc_id in doc_map:
            doc_map[doc_id].index_status = "ready"
            doc_repo._save_doc_meta(doc_map[doc_id])
            indexed_ids.add(doc_id)

    try:
        from core.index_manager import index_documents_batch
        index_documents_batch(kb_id, texts, progress_callback=_on_progress)
        # 锁内原子更新完成状态
        with _get_lock(kb_id):
            kb = kb_repo.get(kb_id) or kb
            kb.index_status = "ready"
            kb.index_progress = 1.0
            kb.index_current_doc = ""
    except Exception as e:
        _logger.error("batch indexing failed for kb %s: %s", kb_id, e)
        with _get_lock(kb_id):
            kb = kb_repo.get(kb_id) or kb
            kb.index_status = "failed"
            kb.index_current_doc = f"错误: {e}"
    with _get_lock(kb_id):
        kb_repo.update(kb)


def delete_document(kb_id: str, doc_id: str) -> bool:
    # 原子地从 document_ids 中移除（防与异步线程交错）
    with _get_lock(kb_id):
        kb = kb_repo.get(kb_id)
        if kb and doc_id in kb.document_ids:
            kb.document_ids.remove(doc_id)
            kb_repo.update(kb)
    return doc_repo.delete_doc(kb_id, doc_id)
