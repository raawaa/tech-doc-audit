from fastapi import APIRouter, HTTPException, UploadFile, File, Form

import services.doc_service as doc_svc

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])


@router.post("/{kb_id}/upload")
async def upload_document(
    kb_id: str,
    file: UploadFile = File(...),
):
    """上传文档到指定知识库"""
    import services.kb_service as kb_svc
    kb = kb_svc.get_kb(kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

    content = await file.read()
    try:
        doc = doc_svc.import_document(kb_id, file.filename, content)
        return {
            "document_id": doc.id,
            "name": doc.name,
            "file_type": doc.file_type,
            "page_count": doc.page_count,
            "index_status": doc.index_status,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))