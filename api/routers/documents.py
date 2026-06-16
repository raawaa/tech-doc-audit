from fastapi import APIRouter, HTTPException, UploadFile, File

import services.doc_service as doc_svc

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])


@router.post("/{kb_id}/upload")
async def upload_document(
    kb_id: str,
    file: UploadFile = File(...),
):
    """上传文档到指定知识库（单文件，异步索引，前端轮询进度）。"""
    import services.kb_service as kb_svc
    kb = kb_svc.get_kb(kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

    content = await file.read()
    try:
        doc = doc_svc.import_document(kb_id, file.filename, content, async_index=True)
        return {
            "document_id": doc.id,
            "name": doc.name,
            "file_type": doc.file_type,
            "page_count": doc.page_count,
            "index_status": doc.index_status,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{kb_id}/batch-upload")
async def batch_upload_documents(
    kb_id: str,
    files: list[UploadFile] = File(...),
):
    """批量上传文档到指定知识库（多文件，异步索引，前端轮询进度）。"""
    import services.kb_service as kb_svc
    kb = kb_svc.get_kb(kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

    file_list = []
    for f in files:
        content = await f.read()
        file_list.append((f.filename, content))

    try:
        docs = doc_svc.batch_import_documents(kb_id, file_list, async_index=True)
        return {
            "total": len(docs),
            "documents": [
                {
                    "document_id": d.id,
                    "name": d.name,
                    "file_type": d.file_type,
                    "index_status": d.index_status,
                }
                for d in docs
            ],
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))