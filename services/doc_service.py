import os
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
    mapping = {".pdf": "pdf", ".doc": "doc", ".docx": "docx"}
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


def delete_document(kb_id: str, doc_id: str) -> bool:
    # 更新知识库 document_ids
    kb = kb_repo.get(kb_id)
    if kb and doc_id in kb.document_ids:
        kb.document_ids.remove(doc_id)
        kb_repo.update(kb)
    return doc_repo.delete_doc(kb_id, doc_id)
