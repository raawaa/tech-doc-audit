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
from core.kb_index_status import KbIndexStatusWriter
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
    # 立刻落盘 content_hash —— 让下一次去重查询能看到，即使后续 parse / 索引崩溃。
    # 否则同一字节内容两次上传会被当成两个不同文档，浪费 OCR 配额。
    doc_repo._save_doc_meta(doc)
    # 解析 PDF 一次：parse_document + save_pages（V6 唯一入口）
    if file_type == "pdf":
        try:
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(content)
                tmp_path = tmp.name
            import pdfplumber
            with pdfplumber.open(tmp_path) as pdf:
                doc.page_count = len(pdf.pages)
            from core.parse_document import parse_document
            from core.pages_store import save_pages
            parse_result = parse_document(tmp_path)
            save_pages(
                kb_id, doc.id, parse_result.to_dict(),
                file_hash=doc.content_hash,
            )
            os.unlink(tmp_path)
        except Exception as e:
            _logger.warning("failed to parse + store pages for %s: %s", doc.id, e)
    # 更新知识库 document_ids（原子 get→modify→update，防与异步线程交错）
    _append_doc_ids_atomic(kb_id, [doc.id])
    if doc.file_path:
        if async_index:
            # 异步：后台线程索引，不阻塞 API 响应
            doc.embedding_status = "pending_index"
            doc_repo._save_doc_meta(doc)
            thread = threading.Thread(
                target=_index_single_doc_async,
                args=(kb_id, doc),
                daemon=True,
            )
            thread.start()
        else:
            # 同步：等待索引完成（CLI 等场景）。
            # 提早置 embedding_status="indexing"，失败回退 "failed"（见 ADR-0003 §决策 5）：
            # 此前的同步路径先写成 "ready" 又不撤销，会出现"未向量化但状态已 ready"的主动误导。
            doc.embedding_status = "indexing"
            doc_repo._save_doc_meta(doc)
            try:
                _index_vec(kb_id, doc.id, doc.file_path)
                doc.embedding_status = "embedded"
            except Exception as e:
                _logger.warning("vector indexing failed for doc %s: %s", doc.id, e)
                doc.embedding_status = "failed"
            doc_repo._save_doc_meta(doc)
    return doc


def _index_single_doc_async(kb_id: str, doc: KBDocument):
    """后台索引单篇文档（由 import_document async_index=True 调用）。

    KB 检索状态字段（``index_status`` / ``index_progress`` / ``index_current_doc``）
    交给 ``KbIndexStatusWriter``（issue #148 / #152）—— 这是该字段在
    ``doc_service`` 内的**唯一**写入者，与 ``reparse_service`` / ``rebuild_kb_index``
    等所有路径共享同一个 writer 接口，KB 级状态字段不再分散直写。
    ``_get_lock(kb_id)`` 仍是**文档生命周期锁**（防 ``document_ids`` 与异步
    索引交错，issue #136 残留）：与 writer 自带的 per-instance 锁保护**不同**
    资源，两把锁并存、互不替代。
    """
    # 标记为 indexing（崩溃后可识别）
    doc.embedding_status = "indexing"
    doc_repo._save_doc_meta(doc)

    # KB 检索状态字段交给 writer —— writer 自己起一把 per-instance 锁串行化
    # read-modify-write。开锁前先 begin()，让 UI/其他线程看见 ``building``
    # 中间态（``rebuild_kb_index`` 同款内置契约）。
    # ``_get_lock(kb_id)`` 仍在**此处**持有（issue #152 AC3）：它防的是
    # document_ids 与 KB 状态字段交错（issue #136 残留）—— 与 writer 的
    # per-instance 锁不同资源；两把锁都保留、嵌套持有。
    # 最终 ``searchable`` 由本函数在末尾 ``finish()``，作为"一个 KB 一篇文档且
    # 导入即完成"的兜底（rebuild_kb_index 不在此路径上跑）。
    with _get_lock(kb_id):
        kb_writer = KbIndexStatusWriter(kb_id, total=1)
        kb_writer.begin()
        kb_writer.note_in_flight(doc.original_name)

    try:
        _index_vec(kb_id, doc.id, doc.file_path)
        doc.embedding_status = "embedded"
    except Exception as e:
        _logger.warning("async indexing failed for doc %s: %s", doc.id, e)
        doc.embedding_status = "failed"

    # 原子地更新文档和 KB 状态（同一锁内，防止前端看到 doc embedded 而 KB 还在 building）
    with _get_lock(kb_id):
        doc_repo._save_doc_meta(doc)
        # 终态走 writer：单文档场景下始终 ``finish()``（searchable），与旧契约
        # 对齐 —— 旧路径无论 doc.embedding_status 是 embedded 还是 failed 都写
        # searchable（KB 视角"已处理过这篇"，doc 视角失败仍记 ``failed``）。
        # 失败摘要留给 batch 路径 + reparse 路径（writer.fail_doc / finish(failed)），
        # 单文档异步入口不重复该摘要。KB 已删 → writer 的 ``_write`` 内部对
        # ``kb_repo.get`` 返 ``None`` 静默 return。
        kb_writer.finish()


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
        doc.embedding_status = "pending_index"
        # 解析 PDF 一次：parse_document + save_pages（V6 唯一入口）
        if file_type == "pdf":
            try:
                import tempfile
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                    tmp.write(content)
                    tmp_path = tmp.name
                with pdfplumber.open(tmp_path) as pdf:
                    doc.page_count = len(pdf.pages)
                from core.parse_document import parse_document
                from core.pages_store import save_pages
                parse_result = parse_document(tmp_path)
                save_pages(
                    kb_id, doc.id, parse_result.to_dict(),
                    file_hash=doc.content_hash,
                )
                os.unlink(tmp_path)
            except Exception as e:
                _logger.warning("batch import: failed to parse + store pages for %s: %s", doc.id, e)

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
    """后台批量索引文档（由 batch_import_documents 调用）。

    KB 检索状态字段（``index_status`` / ``index_progress`` / ``index_current_doc``）
    交给 ``KbIndexStatusWriter``（issue #148 / #152）—— 这是该字段在
    ``doc_service`` 内的**唯一**写入者。三处状态写入：

    1. 开头 ``building`` 占位 → ``kb_writer.begin()`` + ``note_in_flight(first_doc)``；
    2. ``_on_progress`` 回调里的进度推进 → ``kb_writer.advance(current)`` +
       ``note_in_flight(doc_name)``；
    3. 末尾 ``searchable`` / ``failed`` 终态 → ``kb_writer.finish()`` 或
       ``kb_writer.finish(interrupted=str(e))``。

    ``_get_lock(kb_id)`` 仍是**文档生命周期锁**（防 ``document_ids`` 与异步索引
    交错，issue #136 残留）：与 writer 自带的 per-instance 锁保护**不同**资源，
    两把锁并存、互不替代。doc 级 ``embedding_status`` 写入仍是 ``doc_service``
    的事（不同 owner）。
    """
    with _get_lock(kb_id):
        kb = kb_repo.get(kb_id)
        if not kb:
            return

        # 收集需要索引的文档（V6：用 parse_document 走唯一入口）
        from core.parse_document import parse_document
        texts = []
        doc_map = {doc.id: doc for doc in docs}
        for doc in docs:
            # 断点续传：跳过已索引完成的文档
            if doc.embedding_status == "embedded":
                _logger.info("文档 %s 已索引，跳过", doc.id)
                continue

            # 标记为 indexing（崩溃后可识别并重置）
            doc.embedding_status = "indexing"
            doc_repo._save_doc_meta(doc)

            if doc.file_path and os.path.exists(doc.file_path):
                try:
                    parse_result = parse_document(doc.file_path)
                    text = parse_result.full_text
                    if text and len(text) >= 20:
                        texts.append((doc.id, text, doc.original_name, parse_result.by_page))
                    else:
                        _logger.warning("文档 %s 文本提取为空，跳过索引", doc.id)
                        doc_map[doc.id].embedding_status = "failed"
                        doc_repo._save_doc_meta(doc_map[doc.id])
                except Exception as e:
                    _logger.warning("读取文档 %s 失败: %s", doc.id, e)
                    doc_map[doc.id].embedding_status = "failed"
                    doc_repo._save_doc_meta(doc_map[doc.id])

        # KB 检索状态字段交给 writer（issue #148 / #152）：
        # total 在此处确定（解析完成的篇数）—— writer.advance() / finish() 需要它。
        kb_writer = KbIndexStatusWriter(kb_id, total=max(len(texts), 1))

        if not texts:
            # 无文本要索引 → KB 兜底 ``searchable``（旧契约保留，issue #152 AC）。
            # doc_repo 这边的 ``embedding_status`` 失败/跳过的状态已在上面写完。
            kb_writer.finish()
            return

        # 开头 ``building`` 占位：writer 自己起锁串行化 read-modify-write。
        kb_writer.begin()
        kb_writer.note_in_flight(texts[0][2])
    indexed_ids = set()

    def _on_progress(current: int, total: int, doc_name: str):
        # 锁内更新 KB 进度 + 文档状态，防止前端看到 doc embedded 而 KB 还在 building
        with _get_lock(kb_id):
            doc_id = texts[current - 1][0]
            if doc_id in doc_map:
                doc_map[doc_id].embedding_status = "embedded"
                doc_repo._save_doc_meta(doc_map[doc_id])
                indexed_ids.add(doc_id)
            # KB 检索状态推进走 writer（issue #148 / #152）。
            kb_writer.advance(current)
            kb_writer.note_in_flight(doc_name)

    try:
        from core.index_manager import index_documents_batch
        index_documents_batch(kb_id, texts, progress_callback=_on_progress)
        # 锁内 read-modify-write 原子更新完成状态；KB 已删则跳过（不写回陈旧对象）
        # 批量路径此处直接 searchable 而非依赖 rebuild_kb_index（因为我们没调用它）
        with _get_lock(kb_id):
            kb_writer.finish()
    except Exception as e:
        _logger.error("batch indexing failed for kb %s: %s", kb_id, e)
        with _get_lock(kb_id):
            # 失败消息走 writer 的 ``_format_interruption``（issue #152 AC）：
            # 统一前缀 ``批量重新解析中断: ``，与批量重新解析 / 单篇 reparse 失败
            # 路径字面对齐（issue #147 §User Stories 第 14 条）。
            kb_writer.finish(interrupted=str(e))


def delete_document(kb_id: str, doc_id: str) -> bool:
    # 原子地从 document_ids 中移除（防与异步线程交错）
    with _get_lock(kb_id):
        kb = kb_repo.get(kb_id)
        if kb and doc_id in kb.document_ids:
            kb.document_ids.remove(doc_id)
            kb_repo.update(kb)
    # 清理 pages/{doc_id}.json（V6：pages_store 自身的契约要求）
    from core.pages_store import delete_pages as _delete_pages
    _delete_pages(kb_id, doc_id)
    return doc_repo.delete_doc(kb_id, doc_id)
