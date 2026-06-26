"""KB 文档文件服务 — PDF 预览、文本降级、元数据查询。"""

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


@router.get("/{doc_id}/page/{page_number}")
def get_page_text(doc_id: str, page_number: int):
    """获取文档指定页码的文本内容（非 PDF 格式的降级预览）。

    page_number 为 0-based 页码。
    """
    doc = doc_repo.find_doc_by_id(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")

    page_texts = doc.metadata.get("page_texts")
    if not page_texts:
        raise HTTPException(status_code=404, detail="该文档无逐页文本数据")

    if page_number < 0 or page_number >= len(page_texts):
        raise HTTPException(
            status_code=404,
            detail=f"页码 {page_number} 超出范围 (0-{len(page_texts) - 1})",
        )

    return {
        "page_number": page_number,
        "text": page_texts[page_number],
        "total_pages": len(page_texts),
    }
