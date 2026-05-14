"""文档结构识别服务。

不再使用 LLM 做结构分析。完全通过多层降级链从文档格式/内容中提取章节结构。
"""

from models.audit_document import AuditDocument, DocumentStructure
import storage.audit_doc_repo as repo
from services.structure_parser import extract_structure


def identify_structure(doc: AuditDocument) -> DocumentStructure:
    """识别文档结构，零 LLM 调用。

    降级链（在 extract_structure 内部完成）：
    1. docx → Heading 样式
    2. Markdown → 正则解析
    3. 纯文本 → 正则解析
    4. 兜底 → 整篇单章节
    """
    if not doc.parsed_content:
        raise ValueError("文档未解析，请先调用 parse_document")

    return extract_structure(
        parsed_content=doc.parsed_content,
        file_type=doc.file_type,
        file_path=doc.file_path,
    )


def analyze_document_structure(doc_id: str) -> AuditDocument:
    """分析文档结构并更新文档。"""
    doc = repo.get_doc(doc_id)
    if not doc:
        raise ValueError(f"文档不存在: {doc_id}")

    if not doc.parsed_content:
        raise ValueError("文档未解析")

    doc.structure = identify_structure(doc)
    doc.status = "indexed"
    return repo.update_doc(doc)


def get_document_structure(doc_id: str) -> DocumentStructure | None:
    """获取文档结构。"""
    doc = repo.get_doc(doc_id)
    if not doc:
        return None
    return doc.structure
