from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Literal

import services.kb_service as kb_svc
import storage.doc_repo as doc_repo
from core.logger import get_logger

_logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/knowledge-bases", tags=["knowledge-bases"])


def _get_actually_indexed_doc_ids(kb_id: str) -> set[str]:
    """读取 FAISS docstore，返回实际已索引的 doc_id 集合。

    用于 rebuild 后验证哪些文档成功进入了向量索引。
    不依赖内存中的 index_cache（rebuild 后缓存可能已失效）。

    兼容两种 docstore 格式：
    1. 新格式（rebuild 产物）：docstore/data → 每个节点 metadata.doc_id
    2. 旧格式（增量索引）：docstore/ref_doc_info → {doc_id: {node_ids: [...]}}
    """
    from pathlib import Path
    import json as _json
    import os as _os

    vectors_dir = Path(_os.environ.get("AUDIT_DATA_DIR", "data")) / "kbs" / kb_id / "vectors"
    docstore_path = vectors_dir / "docstore.json"
    if not docstore_path.exists():
        return set()
    try:
        docstore = _json.loads(docstore_path.read_text(encoding="utf-8"))

        # 格式1（新）：docstore/data — 每个节点存储 metadata.doc_id
        data = docstore.get("docstore/data", {})
        if data:
            doc_ids = set()
            for node_data in data.values():
                meta = node_data.get("__data__", {}).get("metadata", {})
                doc_id = meta.get("doc_id", "")
                if doc_id:
                    doc_ids.add(doc_id)
            if doc_ids:
                return doc_ids

        # 格式2（旧）：docstore/ref_doc_info
        ref_info = docstore.get("docstore/ref_doc_info", {})
        return set(ref_info.keys())
    except Exception as e:
        _logger.warning("failed to read docstore for kb %s: %s", kb_id, e)
        return set()


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
    embedding_status: str

    @classmethod
    def from_doc(cls, doc):
        return cls(
            id=doc.id,
            name=doc.name,
            original_name=doc.original_name,
            file_type=doc.file_type,
            page_count=doc.page_count,
            embedding_status=doc.embedding_status,
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

    # 标记为 building，所有文档标记为 pending_index
    kb.index_status = "building"
    kb.index_progress = 0.0
    kb.index_current_doc = ""
    kb_svc.update_kb(kb)

    # 将所有关联文档的状态重置为 pending_index，表示正在等待重建
    all_docs = doc_repo.list_docs(kb_id)
    for doc in all_docs:
        doc.embedding_status = "pending_index"
        doc_repo._save_doc_meta(doc)

    def _on_progress(current: int, total: int, doc_name: str):
        """每索引完一篇文档的回调，更新 KB 状态供前端轮询。"""
        fresh = kb_svc.get_kb(kb_id)
        if fresh is None:
            return  # KB 已删，不写陈旧对象
        fresh.index_progress = current / total if total else 0
        fresh.index_current_doc = doc_name
        kb_svc.update_kb(fresh)

    def _run():
        """后台执行重建。"""
        import services.vector_search as vs
        try:
            vs.rebuild_kb_index(kb_id, progress_callback=_on_progress)

            # 重建完成后，检查实际索引结果并更新各文档的向量化状态
            # （rebuild_kb_index 内部可能因节点不匹配等原因跳过某些文档，
            #   所以需要核实 FAISS 索引中实际包含哪些文档）
            indexed_doc_ids = _get_actually_indexed_doc_ids(kb_id)
            for doc in all_docs:
                if doc.id in indexed_doc_ids:
                    doc.embedding_status = "embedded"
                else:
                    doc.embedding_status = "failed"
                    _logger.warning(
                        "reindex: doc %s (%s) not found in FAISS index after rebuild, marked as failed",
                        doc.id, doc.original_name,
                    )
                doc_repo._save_doc_meta(doc)

            # KB 检索状态由 rebuild_kb_index 在锁内按内置契约写回（ADR-0002）。
            # 这里再次按 fetch-fresh 模式只更新进度字段，避免把 status 写回陈旧值
            # （rebuild_kb_index 持锁内独立 fetch+update；handler 自己也是 fetch+update，
            #  互不踩各自锁，无需担心 TOCTOU）。
            fresh = kb_svc.get_kb(kb_id)
            if fresh is not None:
                fresh.index_progress = 1.0
                fresh.index_current_doc = ""
                kb_svc.update_kb(fresh)
        except Exception as e:
            # 失败路径写 failed（rebuild_kb_index 也会自己写 failed；这里再保险一次）
            fresh = kb_svc.get_kb(kb_id)
            if fresh is not None:
                fresh.index_status = "failed"
                fresh.index_current_doc = f"错误: {e}"
                kb_svc.update_kb(fresh)
            for doc in all_docs:
                if doc.embedding_status == "pending_index":
                    doc.embedding_status = "failed"
                    doc_repo._save_doc_meta(doc)

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