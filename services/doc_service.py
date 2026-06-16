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


def _detect_file_type(filename: str) -> Optional[str]:
    ext = os.path.splitext(filename)[1].lower()
    mapping = {".pdf": "pdf", ".doc": "doc", ".docx": "docx", ".md": "md"}
    return mapping.get(ext)


def import_document(kb_id: str, original_name: str, content: bytes) -> KBDocument:
    file_type = _detect_file_type(original_name)
    if not file_type:
        raise ValueError(f"不支持的文件格式: {original_name}")

    doc = doc_repo.save_doc(kb_id, original_name, content, file_type)

    # 无需预建索引，文档导入即 ready
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

    # 更新知识库 document_ids
    kb = kb_repo.get(kb_id)
    if kb and doc.id not in kb.document_ids:
        kb.document_ids.append(doc.id)
        kb_repo.update(kb)

    # 自动建立向量索引（局部导入，免去手动 index rebuild）
    if doc.file_path:
        try:
            _index_vec(kb_id, doc.id, doc.file_path)
        except Exception as e:
            _logger.warning("vector indexing failed for doc %s: %s", doc.id, e)

    return doc


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

    kb = kb_repo.get(kb_id)
    if not kb:
        return

    # 收集需要索引的文档
    texts = []
    for doc in docs:
        if doc.file_path and os.path.exists(doc.file_path):
            try:
                with open(doc.file_path, "r", encoding="utf-8") as f:
                    text = f.read()
                texts.append((doc.id, text, doc.original_name))
            except Exception as e:
                _logger.warning("读取文档 %s 失败: %s", doc.id, e)

    if not texts:
        return

    # 建索引前更新 KB 状态
    kb.index_status = "building"
    kb_repo.update(kb)

    def _on_progress(current: int, total: int, doc_name: str):
        kb.index_progress = current / total
        kb.index_current_doc = doc_name
        kb_repo.update(kb)

    try:
        from core.index_manager import index_documents_batch
        index_documents_batch(kb_id, texts, progress_callback=_on_progress)
        kb.index_status = "ready"
        kb.index_progress = 1.0
        kb.index_current_doc = ""
    except Exception as e:
        _logger.error("batch indexing failed for kb %s: %s", kb_id, e)
        kb.index_status = "failed"
        kb.index_current_doc = f"错误: {e}"
    kb_repo.update(kb)


def delete_document(kb_id: str, doc_id: str) -> bool:
    # 更新知识库 document_ids
    kb = kb_repo.get(kb_id)
    if kb and doc_id in kb.document_ids:
        kb.document_ids.remove(doc_id)
        kb_repo.update(kb)
    return doc_repo.delete_doc(kb_id, doc_id)
