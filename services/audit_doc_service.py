import os
from pathlib import Path
from typing import Optional

import pdfplumber

from models.audit_document import AuditDocument
import storage.audit_doc_repo as repo


def _detect_file_type(filename: str) -> Optional[str]:
    ext = os.path.splitext(filename)[1].lower()
    mapping = {".pdf": "pdf", ".doc": "doc", ".docx": "docx"}
    return mapping.get(ext)


def upload_document(original_name: str, content: bytes) -> AuditDocument:
    """上传待审核文档。"""
    file_type = _detect_file_type(original_name)
    if not file_type:
        raise ValueError(f"不支持的文件格式: {original_name}")

    doc = AuditDocument(
        name=original_name,
        original_name=original_name,
        file_type=file_type,
        file_path="",
        status="uploaded",
    )

    # 保存文件
    doc_dir = repo._doc_dir(doc.id)
    repo._ensure_dir(doc_dir)
    doc.file_path = str(repo._doc_file(doc.id, file_type))
    with open(doc.file_path, "wb") as f:
        f.write(content)

    return repo.save_doc(doc)


def get_document(doc_id: str) -> Optional[AuditDocument]:
    """获取文档。"""
    return repo.get_doc(doc_id)


def list_documents() -> list[AuditDocument]:
    """列出所有待审核文档。"""
    return repo.list_docs()


def parse_document(doc_id: str) -> AuditDocument:
    """解析文档，提取文本和页数。"""
    doc = repo.get_doc(doc_id)
    if not doc:
        raise ValueError(f"文档不存在: {doc_id}")

    try:
        if doc.file_type == "pdf":
            text_parts = []
            with pdfplumber.open(doc.file_path) as pdf:
                doc.page_count = len(pdf.pages)
                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        text_parts.append(page_text)
            doc.parsed_content = "\n\n".join(text_parts)
        else:
            # Word 文档暂不支持
            doc.parsed_content = "[Word 文档解析待实现]"
            doc.page_count = None

        doc.status = "parsed"
    except Exception as e:
        doc.status = "failed"
        doc.error_message = str(e)

    return repo.update_doc(doc)


def delete_document(doc_id: str) -> bool:
    """删除文档。"""
    return repo.delete_doc(doc_id)


def update_document(doc: AuditDocument) -> AuditDocument:
    """更新文档。"""
    return repo.update_doc(doc)
