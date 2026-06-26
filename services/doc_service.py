import hashlib
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


def _append_doc_ids_atomic(kb_id: str, doc_ids: list[str]) -> None:
    """原子地把 doc_ids 追加到 kb.document_ids（锁内 read-modify-write）。

    跳过已存在的 id；KB 不存在时静默返回。集中表达「document_ids 追加必须
    在 _get_lock 内完成」这一约束，避免 import_document / batch_import_documents
    与异步索引线程交错时 document_ids 被陈旧对象覆盖（见 review_report.md #2
    TOCTOU 残留）。
    """
    with _get_lock(kb_id):
        kb = kb_repo.get(kb_id)
        if kb is None:
            return
        changed = False
        for doc_id in doc_ids:
            if doc_id not in kb.document_ids:
                kb.document_ids.append(doc_id)
                changed = True
        if changed:
            kb_repo.update(kb)


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

    # 内容去重：检查同 KB 下是否已有相同文件（SHA-256 字节级）
    file_hash = hashlib.sha256(content).hexdigest()
    existing_docs = doc_repo.list_docs(kb_id)
    for d in existing_docs:
        if d.content_hash == file_hash:
            _logger.info("文档 %s 与已有文档 %s 内容相同（%s），跳过导入",
                         original_name, d.original_name, file_hash[:12])
            return d

    doc = doc_repo.save_doc(kb_id, original_name, content, file_type)
    doc.content_hash = file_hash

    doc.index_status = "ready"
    doc_repo._save_doc_meta(doc)

    # 提取 PDF 页数和逐页文本
    if file_type == "pdf":
        try:
            import tempfile
            from core.text_extraction import extract_text_by_page
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(content)
                tmp_path = tmp.name
            with pdfplumber.open(tmp_path) as pdf:
                doc.page_count = len(pdf.pages)
            # 提取逐页文本并存储到 metadata（page_texts[0] = 第1页文本）
            page_texts = extract_text_by_page(tmp_path)
            doc.metadata["page_texts"] = [text for _, text in page_texts]
            os.unlink(tmp_path)
        except Exception as e:
            _logger.warning("failed to extract page data for %s: %s", doc.id, e)

    # 更新知识库 document_ids（原子 get→modify→update，防与异步线程交错）
    _append_doc_ids_atomic(kb_id, [doc.id])

    # 向量索引
    if doc.file_path:
        _page_texts = doc.metadata.get("page_texts")
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
                _index_vec(kb_id, doc.id, doc.file_path, page_texts=_page_texts)
            except Exception as e:
                _logger.warning("vector indexing failed for doc %s: %s", doc.id, e)

    return doc


def _index_single_doc_async(kb_id: str, doc: KBDocument):
    """后台索引单篇文档（由 import_document async_index=True 调用）。"""
    import storage.kb_repo as kb_repo

    # 标记为 indexing（崩溃后可识别）
    doc.index_status = "indexing"
    doc_repo._save_doc_meta(doc)

    # 获取最新 kb 并标记 building（原子 get→modify→update）
    with _get_lock(kb_id):
        kb = kb_repo.get(kb_id)
        if not kb:
            return
        kb.index_status = "building"
        kb.index_progress = 0.0
        kb.index_current_doc = doc.original_name
        kb_repo.update(kb)

    try:
        page_texts = doc.metadata.get("page_texts")
        _index_vec(kb_id, doc.id, doc.file_path, page_texts=page_texts)
        doc.index_status = "ready"
    except Exception as e:
        _logger.warning("async indexing failed for doc %s: %s", doc.id, e)
        doc.index_status = "failed"

    # 原子地更新文档和 KB 状态（同一锁内，防止前端看到 doc ready 而 KB 还在 building）
    with _get_lock(kb_id):
        doc_repo._save_doc_meta(doc)
        kb = kb_repo.get(kb_id)
        if kb:
            kb.index_status = "ready"
            kb.index_progress = 1.0
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

    # 加载已有文档哈希集合，用于批量去重
    existing_docs = doc_repo.list_docs(kb_id)
    existing_hashes = {d.content_hash for d in existing_docs if d.content_hash}

    for original_name, content in files:
        file_type = _detect_file_type(original_name)
        if not file_type:
            _logger.warning("跳过不支持的文件: %s", original_name)
            continue

        # 内容去重
        file_hash = hashlib.sha256(content).hexdigest()
        if file_hash in existing_hashes:
            _logger.info("跳过重复文档: %s (hash=%s)", original_name, file_hash[:12])
            continue
        existing_hashes.add(file_hash)

        doc = doc_repo.save_doc(kb_id, original_name, content, file_type)
        doc.content_hash = file_hash
        doc.index_status = "pending_index"
        doc_repo._save_doc_meta(doc)
        docs.append(doc)

    # 原子追加 document_ids（锁内 read-modify-write，防与异步索引线程交错覆盖）
    _append_doc_ids_atomic(kb_id, [d.id for d in docs])

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

        # 收集需要索引的文档（使用 _extract_text 提取纯文本，支持 PDF/DOCX 等二进制格式）
        from core.text_extraction import extract_text as _extract_text
        texts = []
        doc_map = {doc.id: doc for doc in docs}
        for doc in docs:
            # 断点续传：跳过已索引完成的文档
            if doc.index_status == "ready":
                _logger.info("文档 %s 已索引，跳过", doc.id)
                continue

            # 标记为 indexing（崩溃后可识别并重置）
            doc.index_status = "indexing"
            doc_repo._save_doc_meta(doc)

            if doc.file_path and os.path.exists(doc.file_path):
                try:
                    text = _extract_text(doc.file_path)
                    if text and len(text) >= 20:
                        page_texts = doc.metadata.get("page_texts")
                        texts.append((doc.id, text, doc.original_name, page_texts))
                    else:
                        _logger.warning("文档 %s 文本提取为空，跳过索引", doc.id)
                        doc_map[doc.id].index_status = "failed"
                        doc_repo._save_doc_meta(doc_map[doc.id])
                except Exception as e:
                    _logger.warning("读取文档 %s 失败: %s", doc.id, e)
                    doc_map[doc.id].index_status = "failed"
                    doc_repo._save_doc_meta(doc_map[doc.id])

        if not texts:
            kb.index_status = "ready"
            kb_repo.update(kb)
            return

        kb.index_status = "building"
        kb.index_progress = 0.0
        kb.index_current_doc = texts[0][2] if texts else "准备中…"
        kb_repo.update(kb)

    indexed_ids = set()

    def _on_progress(current: int, total: int, doc_name: str):
        # 锁内更新 KB 进度 + 文档状态，防止前端看到 doc ready 而 KB 还在 building
        with _get_lock(kb_id):
            inner_kb = kb_repo.get(kb_id)
            if not inner_kb:
                return
            inner_kb.index_progress = current / total
            inner_kb.index_current_doc = doc_name
            kb_repo.update(inner_kb)
            doc_id = texts[current - 1][0]
            if doc_id in doc_map:
                doc_map[doc_id].index_status = "ready"
                doc_repo._save_doc_meta(doc_map[doc_id])
                indexed_ids.add(doc_id)

    try:
        from core.index_manager import index_documents_batch
        index_documents_batch(kb_id, texts, progress_callback=_on_progress)
        # 锁内 read-modify-write 原子更新完成状态；KB 已删则跳过（不写回陈旧对象）
        with _get_lock(kb_id):
            kb = kb_repo.get(kb_id)
            if kb is not None:
                kb.index_status = "ready"
                kb.index_progress = 1.0
                kb.index_current_doc = ""
                kb_repo.update(kb)
    except Exception as e:
        _logger.error("batch indexing failed for kb %s: %s", kb_id, e)
        with _get_lock(kb_id):
            kb = kb_repo.get(kb_id)
            if kb is not None:
                kb.index_status = "failed"
                kb.index_current_doc = f"错误: {e}"
                kb_repo.update(kb)


def delete_document(kb_id: str, doc_id: str) -> bool:
    # 原子地从 document_ids 中移除（防与异步线程交错）
    with _get_lock(kb_id):
        kb = kb_repo.get(kb_id)
        if kb and doc_id in kb.document_ids:
            kb.document_ids.remove(doc_id)
            kb_repo.update(kb)
    return doc_repo.delete_doc(kb_id, doc_id)
