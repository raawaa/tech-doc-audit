"""KB 文档重新解析服务（PRD #29 / V4）。

``reparse_document(doc_id)`` 走与导入相同的流水线：
``parse_document`` → ``pages_store.save_pages`` → 重建向量索引 → 更新 ``embedding_status``。

设计上：
- 立即返回（异步）；后台任务在 KB 级锁内执行，避免与重建索引混线。
- 任何步骤失败 → ``embedding_status=failed`` + ``index_current_doc`` 写错误信息（沿用现有契约）。
- 完整逆向兼容：老 ``page_texts`` 路径仍在 ``import_document`` 里；reparse 走新路径。
"""
from __future__ import annotations

import threading
from typing import Optional

from core.logger import get_logger
from core.parse_document import parse_document
from core.pages_store import save_pages
from core.index_manager import (
    _get_index_lock,
    index_document,
    remove_document,
)
import storage.doc_repo as doc_repo
import storage.kb_repo as kb_repo

_logger = get_logger(__name__)


def reparse_document(doc_id: str) -> dict:
    """同步启动重新解析的 KB 文档。立即返回 status=pending_index；后台任务执行。

    Returns:
        ``{"status": "pending_index", "doc_id": "..."}`` 表示已调度。
    Raises:
        ValueError: doc 不存在或 type 不支持（如 md/md 不需要 PaddleOCR 重解析，仍会跑）。
    """
    doc = doc_repo.find_doc_by_id(doc_id)
    if not doc:
        raise ValueError(f"document not found: {doc_id}")

    # 标记 pending_index（崩溃后可见）
    doc.embedding_status = "pending_index"
    doc_repo._save_doc_meta(doc)

    thread = threading.Thread(
        target=_reparse_async,
        args=(doc.kb_id, doc_id),
        daemon=True,
    )
    thread.start()

    return {"status": "pending_index", "doc_id": doc_id}


def _reparse_async(kb_id: str, doc_id: str) -> None:
    """后台执行：parse → save_pages → 重建索引 → 更新状态。"""
    doc = doc_repo.get_doc(kb_id, doc_id)
    if not doc or not doc.file_path:
        _mark_failed(kb_id, doc_id, "doc or file_path missing")
        return

    # 标记 KB building
    kb = kb_repo.get(kb_id)
    if not kb:
        return
    kb.index_status = "building"
    kb.index_progress = 0.0
    kb.index_current_doc = doc.original_name
    kb_repo.update(kb)

    with _get_index_lock(kb_id):
        try:
            # 1) 解析（带缓存；命中跳过 OCR 配额）
            parse_result = parse_document(doc.file_path)
            if not parse_result.full_text or len(parse_result.full_text) < 20:
                raise RuntimeError("parse_document returned empty/sparse text")

            # 2) 落 pages/{doc_id}.json
            save_pages(
                kb_id, doc_id, parse_result.to_dict(),
                file_hash=doc.content_hash,
            )

            # 3) 先清理该 doc 的旧节点（避免重复写入）
            try:
                remove_document(kb_id, doc_id)
            except Exception as e:
                _logger.warning("reparse: failed to remove old nodes for %s: %s", doc_id, e)

            # 4) 重建索引（整篇切 chunk + _inject_page_number 自动注入）
            index_document(
                kb_id, doc_id, parse_result.full_text,
                source_name=doc.original_name,
                by_page=parse_result.by_page,
                by_layout=parse_result.layout,
            )

            # 5) 更新文档与 KB 状态
            doc.embedding_status = "embedded"
            doc_repo._save_doc_meta(doc)

            fresh = kb_repo.get(kb_id)
            if fresh is not None:
                fresh.index_status = "searchable"
                fresh.index_progress = 1.0
                fresh.index_current_doc = ""
                kb_repo.update(fresh)

            _logger.info(
                "reparse: doc %s (%s) embedded %d chunks",
                doc_id, doc.original_name,
                len(parse_result.by_page),
            )
        except Exception as e:
            _logger.warning("reparse failed for doc %s: %s", doc_id, e)
            _mark_failed(kb_id, doc_id, str(e))


def _mark_failed(kb_id: str, doc_id: str, err: str) -> None:
    doc = doc_repo.get_doc(kb_id, doc_id)
    if doc is not None:
        doc.embedding_status = "failed"
        doc_repo._save_doc_meta(doc)
    kb = kb_repo.get(kb_id)
    if kb is not None:
        kb.index_status = "failed"
        kb.index_current_doc = f"reparse 错误: {err}"
        kb_repo.update(kb)
