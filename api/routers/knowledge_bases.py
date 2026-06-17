from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Literal

import services.kb_service as kb_svc
import storage.doc_repo as doc_repo

router = APIRouter(prefix="/api/v1/knowledge-bases", tags=["knowledge-bases"])


class CreateKBRequest(BaseModel):
    name: str
    description: str = ""
    category: Literal["national", "industry", "enterprise"] = "national"


class KBDocumentResponse(BaseModel):
    id: str
    name: str
    original_name: str
    file_type: str
    page_count: int | None
    index_status: str

    @classmethod
    def from_doc(cls, doc):
        return cls(
            id=doc.id,
            name=doc.name,
            original_name=doc.original_name,
            file_type=doc.file_type,
            page_count=doc.page_count,
            index_status=doc.index_status,
        )


class KBResponse(BaseModel):
    id: str
    name: str
    description: str
    category: str
    created_at: str
    updated_at: str
    document_count: int
    index_status: str
    index_progress: Optional[float] = None
    index_current_doc: str = ""

    @classmethod
    def from_kb(cls, kb):
        return cls(
            id=kb.id,
            name=kb.name,
            description=kb.description,
            category=kb.category,
            created_at=kb.created_at.isoformat() if hasattr(kb.created_at, 'isoformat') else str(kb.created_at),
            updated_at=kb.updated_at.isoformat() if hasattr(kb.updated_at, 'isoformat') else str(kb.updated_at),
            document_count=len(kb.document_ids),
            index_status=kb.index_status,
            index_progress=getattr(kb, 'index_progress', 0.0),
            index_current_doc=getattr(kb, 'index_current_doc', ''),
        )


@router.get("", response_model=list[KBResponse])
def list_kbs(category: Optional[str] = Query(None)):
    """获取知识库列表"""
    kbs = kb_svc.list_kbs(category=category)
    return [KBResponse.from_kb(kb) for kb in kbs]


@router.post("", response_model=KBResponse)
def create_kb(req: CreateKBRequest):
    """创建知识库"""
    kb = kb_svc.create_kb(
        name=req.name,
        description=req.description,
        category=req.category,
    )
    return KBResponse.from_kb(kb)


@router.get("/{kb_id}", response_model=KBResponse)
def get_kb(kb_id: str):
    """获取知识库详情"""
    kb = kb_svc.get_kb(kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return KBResponse.from_kb(kb)


@router.delete("/{kb_id}")
def delete_kb(kb_id: str):
    """删除知识库"""
    success = kb_svc.delete_kb(kb_id)
    if not success:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return {"message": "删除成功"}


@router.post("/{kb_id}/reindex")
def reindex_kb(kb_id: str):
    """重建知识库索引（异步：立即返回，后台运行，进度通过 GET 查询）。"""
    kb = kb_svc.get_kb(kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

    if kb.index_status == "building":
        raise HTTPException(status_code=409, detail="索引正在重建中")

    # 标记为 building
    kb.index_status = "building"
    kb.index_progress = 0.0
    kb.index_current_doc = ""
    kb_svc.update_kb(kb)

    def _on_progress(current: int, total: int, doc_name: str):
        """每索引完一篇文档的回调，更新 KB 状态供前端轮询。"""
        kb.index_progress = current / total if total else 0
        kb.index_current_doc = doc_name
        kb_svc.update_kb(kb)

    def _run():
        """后台执行重建。"""
        import services.vector_search as vs
        try:
            vs.rebuild_kb_index(kb_id, progress_callback=_on_progress)
            # 重建完成
            kb.index_status = "ready"
            kb.index_progress = 1.0
            kb.index_current_doc = ""
        except Exception as e:
            kb.index_status = "failed"
            kb.index_current_doc = f"错误: {e}"
        kb_svc.update_kb(kb)

    import threading
    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    return {"message": "索引重建已启动"}


@router.get("/{kb_id}/documents", response_model=list[KBDocumentResponse])
def list_kb_documents(kb_id: str):
    """获取知识库内的文档列表"""
    kb = kb_svc.get_kb(kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    docs = doc_repo.list_docs(kb_id)
    return [KBDocumentResponse.from_doc(doc) for doc in docs]


@router.delete("/{kb_id}/documents/{doc_id}")
def delete_kb_document(kb_id: str, doc_id: str):
    """删除知识库中的文档"""
    import services.doc_service as doc_svc
    success = doc_svc.delete_document(kb_id, doc_id)
    if not success:
        raise HTTPException(status_code=404, detail="文档不存在")
    return {"message": "删除成功"}