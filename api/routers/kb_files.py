"""KB 文档文件服务 — PDF 预览、文本降级、元数据查询、重新解析。"""

import os
import mimetypes
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

import storage.doc_repo as doc_repo
from storage import validate_id

router = APIRouter(prefix="/api/v1/kb-documents", tags=["kb-documents"])



@router.get("/{doc_id}")
def get_document_meta(doc_id: str):
    """获取单个 KB 文档的元数据（供 PDF 查看器使用）。"""
    doc = doc_repo.find_doc_by_id(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    return {
        "id": doc.id,
        "name": doc.name,
        "original_name": doc.original_name,
        "file_type": doc.file_type,
        "page_count": doc.page_count,
        "kb_id": doc.kb_id,
    }


@router.get("/{doc_id}/file")
def get_document_file(doc_id: str, request: Request):
    """返回 KB 文档的原始文件。支持 Range 请求（pdfjs 需要）。"""
    doc = doc_repo.find_doc_by_id(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")

    file_path = Path(doc.file_path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="文件不存在")

    # 路径穿越校验：确保文件在 data/kbs/ 目录下
    data_dir = Path(os.environ.get("AUDIT_DATA_DIR", "./data")).resolve()
    try:
        file_path.resolve().relative_to(data_dir)
    except ValueError:
        raise HTTPException(status_code=403, detail="非法文件路径")

    media_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    file_size = file_path.stat().st_size

    # Range 请求支持
    range_header = request.headers.get("range")
    if range_header:
        match = re.match(r"bytes=(\d+)-(\d*)", range_header)
        if match:
            start = int(match.group(1))
            end_str = match.group(2)
            end = int(end_str) if end_str else file_size - 1

            if start >= file_size:
                raise HTTPException(status_code=416, detail="Range not satisfiable")

            def range_stream():
                with open(file_path, "rb") as f:
                    f.seek(start)
                    remaining = end - start + 1
                    chunk_size = 64 * 1024
                    while remaining > 0:
                        data = f.read(min(chunk_size, remaining))
                        if not data:
                            break
                        yield data
                        remaining -= len(data)

            return StreamingResponse(
                range_stream(),
                status_code=206,
                media_type=media_type,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(end - start + 1),
                },
            )

    # 非 Range 请求：流式返回整个文件
    def full_stream():
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(64 * 1024)
                if not chunk:
                    break
                yield chunk

    return StreamingResponse(
        full_stream(),
        media_type=media_type,
        headers={
            "Accept-Ranges": "bytes",
            "Content-Length": str(file_size),
        },
    )



@router.get("/{doc_id}/layout")
def get_document_layout(doc_id: str):
    """获取 KB 文档的 OCR 版面布局（PRD #29 V7 / 高亮定位用）。

    返回 ``pages/{doc_id}.json`` 中按页组织的 layout blocks（含 ``bbox_norm`` 与
    ``block_content``），供前端 ``PdfViewer`` 走 OCR bbox 路线画高亮。

    - 文档不存在 → 404
    - pages 文件缺失 / 损坏 → 404（区分"未解析"与"解析无 layout"）
    - pages 存在但 ``layout`` 全空 → 404（pdfplumber fallback 产物无 OCR layout）
    - 正常返回：``{layout: [...], has_layout: true}``
    """
    doc = doc_repo.find_doc_by_id(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")

    from core.pages_store import load_pages as _load_pages
    pages_doc = _load_pages(doc.kb_id, doc_id)
    if not pages_doc:
        raise HTTPException(status_code=404, detail="该文档未解析（请触发重新解析）")

    raw_layout = pages_doc.get("layout") or []
    if not raw_layout:
        raise HTTPException(status_code=404, detail="该文档无 OCR layout 数据（仅按页文本）")

    # 仅保留非空 layout 页 + 非空 blocks 段（与前端契约对齐：has_layout=True 即至少有 1 个 block）。
    # 按文档化的契约字段白名单输出，避免把 pages 文件里多余的字段透传给前端
    # （将来 pages 文件加新字段时不会意外破坏前端契约）。
    # 仅保留非空 layout 页 + 非空 blocks 段（has_layout=True 即至少有 1 个 block）。
    # 同时按文档化的契约字段白名单输出 blocks（block_label / block_content /
    # bbox_norm / polygon_norm / block_order），避免把 pages 文件里多余的字段
    # 透传给前端。
    cleaned = []
    for page in raw_layout:
        raw_blocks = page.get("blocks") or []
        if not raw_blocks:
            continue
        cleaned_blocks = [
            {
                "block_label": b.get("block_label", ""),
                "block_content": b.get("block_content", ""),
                "bbox_norm": b.get("bbox_norm", []),
                "polygon_norm": b.get("polygon_norm", []),
                "block_order": b.get("block_order", 0),
            }
            for b in raw_blocks
        ]
        cleaned.append({
            "page": page.get("page", 0),
            "width": page.get("width", 0),
            "height": page.get("height", 0),
            "blocks": cleaned_blocks,
        })

    if not cleaned:
        raise HTTPException(status_code=404, detail="该文档无 OCR layout 数据")

    return {"layout": cleaned, "has_layout": True}

@router.get("/{doc_id}/page/{page_number}")
def get_page_text(doc_id: str, page_number: int):
    """获取文档指定页码的文本内容（V6：从 pages/{doc_id}.json 读取）。

    page_number 为 0-based 页码。
    """
    from core.pages_store import load_pages as _load_pages
    doc = doc_repo.find_doc_by_id(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")

    pages_doc = _load_pages(doc.kb_id, doc_id)
    if not pages_doc:
        raise HTTPException(status_code=404, detail="该文档无按页文本数据（请触发重新解析）")

    by_page = pages_doc.get("by_page") or []
    page_texts = [entry.get("text", "") for entry in by_page]
    if not page_texts or page_number < 0 or page_number >= len(page_texts):
        raise HTTPException(
            status_code=404,
            detail=f"页码 {page_number} 超出范围 (0-{len(page_texts) - 1})",
        )

    return {
        "page_number": page_number,
        "text": page_texts[page_number],
        "total_pages": len(page_texts),
    }


@router.post("/{doc_id}/reparse", status_code=202)
def reparse_kb_document(doc_id: str):
    """对单篇 KB 文档触发重新解析（PRD #29 / V4）。

    流程：``parse_document`` → ``pages_store.save_pages`` → 重建向量索引。
    - 立即返回 202 + ``{status: pending_index, doc_id}``。
    - 后端 KB 级锁内起后台线程；前端轮询 ``GET /kb-documents/{doc_id}`` 看 ``embedding_status``。
    - 状态机：``pending_index`` → ``indexing`` → ``embedded``，失败回 ``failed``。
    - V6：``GET /kb-documents/{doc_id}/page/{N}`` 已从 ``pages_store`` 读，reparse 后
      即可拿到正确页号。
    """
    try:
        from services.reparse_service import reparse_document as _reparse
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"reparse service unavailable: {e}")

    try:
        result = _reparse(doc_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return result
