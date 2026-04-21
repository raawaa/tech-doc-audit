from typing import Optional

from fastapi import APIRouter, HTTPException, UploadFile, File, Query
from pydantic import BaseModel

import services.audit_doc_service as audit_doc_svc
import services.structure_service as structure_svc
import services.temp_index_service as temp_index_svc

router = APIRouter(prefix="/api/v1/audit-documents", tags=["audit-documents"])


class AuditDocumentResponse(BaseModel):
    id: str
    name: str
    original_name: str
    file_type: str
    page_count: Optional[int]
    status: str
    created_at: str
    updated_at: str
    has_structure: bool
    has_index: bool

    @classmethod
    def from_doc(cls, doc):
        return cls(
            id=doc.id,
            name=doc.name,
            original_name=doc.original_name,
            file_type=doc.file_type,
            page_count=doc.page_count,
            status=doc.status,
            created_at=str(doc.created_at),
            updated_at=str(doc.updated_at),
            has_structure=doc.structure is not None,
            has_index=doc.tree_index_path is not None,
        )


class StructureResponse(BaseModel):
    doc_id: str
    title: Optional[str]
    chapters: list[dict]
    total_clauses: int

    @classmethod
    def from_structure(cls, doc_id: str, structure):
        return cls(
            doc_id=doc_id,
            title=structure.title if structure else None,
            chapters=[
                {
                    "number": ch.number,
                    "title": ch.title,
                    "clauses": [{"number": c.number, "text": c.text[:100]} for c in ch.clauses]
                }
                for ch in (structure.chapters if structure else [])
            ],
            total_clauses=structure.total_clauses if structure else 0,
        )


@router.get("", response_model=list[AuditDocumentResponse])
def list_audit_documents(status: Optional[str] = Query(None)):
    """获取待审核文档列表。"""
    docs = audit_doc_svc.list_documents()
    if status:
        docs = [d for d in docs if d.status == status]
    return [AuditDocumentResponse.from_doc(d) for d in docs]


@router.post("", response_model=AuditDocumentResponse)
async def upload_audit_document(file: UploadFile = File(...)):
    """上传待审核文档。"""
    content = await file.read()
    try:
        doc = audit_doc_svc.upload_document(file.filename, content)
        return AuditDocumentResponse.from_doc(doc)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{doc_id}", response_model=AuditDocumentResponse)
def get_audit_document(doc_id: str):
    """获取文档详情。"""
    doc = audit_doc_svc.get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    return AuditDocumentResponse.from_doc(doc)


@router.delete("/{doc_id}")
def delete_audit_document(doc_id: str):
    """删除文档。"""
    success = audit_doc_svc.delete_document(doc_id)
    if not success:
        raise HTTPException(status_code=404, detail="文档不存在")
    # 同时删除临时索引
    temp_index_svc.delete_temp_index(doc_id)
    return {"message": "删除成功"}


@router.post("/{doc_id}/parse", response_model=AuditDocumentResponse)
def parse_audit_document(doc_id: str):
    """解析文档，提取文本。"""
    try:
        doc = audit_doc_svc.parse_document(doc_id)
        return AuditDocumentResponse.from_doc(doc)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{doc_id}/structure", response_model=StructureResponse)
def get_document_structure(doc_id: str):
    """获取文档结构。"""
    doc = audit_doc_svc.get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")

    # 如果文档未解析，先解析
    if not doc.parsed_content:
        doc = audit_doc_svc.parse_document(doc_id)

    # 如果没有结构，先分析
    if not doc.structure:
        try:
            doc = structure_svc.analyze_document_structure(doc_id)
        except Exception:
            pass

    return StructureResponse.from_structure(doc_id, doc.structure)


@router.post("/{doc_id}/structure", response_model=StructureResponse)
def analyze_document_structure(doc_id: str):
    """分析文档结构。"""
    doc = audit_doc_svc.get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")

    if not doc.parsed_content:
        raise HTTPException(status_code=400, detail="请先解析文档")

    try:
        doc = structure_svc.analyze_document_structure(doc_id)
        return StructureResponse.from_structure(doc_id, doc.structure)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{doc_id}/index", response_model=AuditDocumentResponse)
def build_temp_index(doc_id: str):
    """构建临时索引。"""
    doc = audit_doc_svc.get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")

    doc = temp_index_svc.build_temp_index(doc)
    return AuditDocumentResponse.from_doc(doc)


@router.post("/{doc_id}/process")
def process_document_full(doc_id: str):
    """完整处理文档：解析 + 结构分析 + 索引。"""
    doc = audit_doc_svc.get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")

    # 1. 解析
    doc = audit_doc_svc.parse_document(doc_id)

    # 2. 结构分析
    if doc.parsed_content:
        try:
            doc = structure_svc.analyze_document_structure(doc_id)
        except Exception:
            pass

    # 3. 构建索引
    doc = temp_index_svc.build_temp_index(doc)

    return AuditDocumentResponse.from_doc(doc)
