"""KB 文档重新解析服务（PRD #29 / V4）。

``reparse_document(doc_id)`` 走与导入相同的流水线：
``parse_document`` → ``pages_store.save_pages`` → 重建向量索引 → 更新 ``embedding_status``。

设计上：
- 立即返回（异步）；后台任务在 KB 级锁内执行，避免与重建索引混线。
- 任何步骤失败 → ``embedding_status=failed`` + ``index_current_doc`` 写错误信息（沿用现有契约）。
- KB 级检索状态（``index_status`` / ``index_progress`` / ``index_current_doc``）由
  ``core.kb_index_status.KbIndexStatusWriter``（issue #148）唯一写入。单篇入口默认
  自己造 ``total=1`` 的 writer 走完整生命周期；批量重新解析
  （``services.bulk_reparse_service``）注入编排层共享的 writer（``total=N``），
  由此函数在自己的 begin()/finish() 之间干活，编排层在批次首尾再
  begin()/finish() 收尾，整批期间 KB 不会在 ``building ⇄ searchable`` 间抖动。
- 完整逆向兼容：老 ``page_texts`` 路径仍在 ``import_document`` 里；reparse 走新路径。
"""
from __future__ import annotations

import threading
from typing import Optional

from core.kb_index_status import KbIndexStatusWriter
from core.logger import get_logger
from core.parse_document import parse_document, MIN_FULL_TEXT_CHARS
from core.pages_store import save_pages
from core.index_manager import (
    _get_index_lock,
    index_document,
    remove_document,
)
import storage.doc_repo as doc_repo
import storage.kb_repo as kb_repo

_logger = get_logger(__name__)


def reparse_document(
    doc_id: str, *, kb_writer: Optional[KbIndexStatusWriter] = None
) -> dict:
    """同步启动重新解析的 KB 文档。立即返回 status=pending_index；后台任务执行。

    Args:
        kb_writer: KB 检索状态字段的唯一写入者（issue #148 / #147）。批量重新解析
            （``services.bulk_reparse_service``）注入它跨文档共享的 writer，让
            整批期间 KB 状态由编排层独占管理；默认 ``None`` —— 函数自己构造一个
            ``KbIndexStatusWriter(kb_id, total=1)`` 走完整生命周期（开头 building、
            终态 searchable|failed）。无论是否传入，doc 级 ``embedding_status``
            都照常写。

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

    # 默认自己管 KB 状态；批量注入时由编排层管生命周期。
    if kb_writer is None:
        kb_writer = KbIndexStatusWriter(doc.kb_id, total=1)

    thread = threading.Thread(
        target=_reparse_async,
        args=(doc.kb_id, doc_id, kb_writer),
        daemon=True,
    )
    thread.start()

    return {"status": "pending_index", "doc_id": doc_id}


def _reparse_async(
    kb_id: str, doc_id: str, kb_writer: KbIndexStatusWriter
) -> None:
    """后台执行：parse → save_pages → 重建索引 → 更新状态。

    KB 级状态字段全部走 writer：单篇路径 writer 由 ``reparse_document`` 构造
    并 ``begin()``，批量路径 writer 由编排层注入、begin()/finish() 都归
    编排层管 —— 本函数只调 ``note_in_flight`` / ``finish`` / ``fail_doc``。
    """
    doc = doc_repo.get_doc(kb_id, doc_id)
    if not doc or not doc.file_path:
        _mark_failed(
            kb_id, doc_id, "doc or file_path missing",
            kb_writer=kb_writer,
        )
        return

    kb = kb_repo.get(kb_id)
    if not kb:
        return

    # 开锁前先标记 building。批量场景下编排层已 begin()，这里再调一次
    # 是幂等的（同样的 building + progress=0 + current_doc=""）。
    kb_writer.begin()
    kb_writer.note_in_flight(doc.original_name)

    with _get_index_lock(kb_id):
        try:
            # 1) 解析（带缓存；命中跳过 OCR 配额）
            parse_result = parse_document(doc.file_path)
            if not parse_result.full_text or len(parse_result.full_text) < MIN_FULL_TEXT_CHARS:
                raise RuntimeError("parse_document returned empty/sparse text")
            # 防御 #94 假成功指纹：full_text ≥ 20 chars 但 layout/by_page 为空
            # 意味着无高亮坐标，chip 预览会显示"未解析"；显式抛错走 _mark_failed
            # 而非继续走"embedded"路径。
            if not parse_result.layout:
                raise RuntimeError(f"empty layout for {doc_id}")
            if not parse_result.by_page:
                raise RuntimeError(f"empty by_page for {doc_id}")

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

            kb_writer.finish()

            _logger.info(
                "reparse: doc %s (%s) embedded %d chunks",
                doc_id, doc.original_name,
                len(parse_result.by_page),
            )
        except Exception as e:
            _logger.warning("reparse failed for doc %s: %s", doc_id, e)
            _mark_failed(
                kb_id, doc_id, str(e),
                kb_writer=kb_writer,
            )


def _mark_failed(
    kb_id: str, doc_id: str, err: str, *, kb_writer: KbIndexStatusWriter
) -> None:
    """doc 失败 → 写 ``embedding_status="failed"`` + 走 writer 收尾。

    KB 级终态由 writer 自己决定（``fail_doc`` 公开方法，单篇 / 批量各走各的
    路径 —— ``total=1`` 写终态 ``failed``，``total>1`` 只把错误摘要写到
    ``index_current_doc``，保留 ``status=building`` 让编排层收尾）。
    """
    doc = doc_repo.get_doc(kb_id, doc_id)
    if doc is not None:
        doc.embedding_status = "failed"
        doc_repo._save_doc_meta(doc)

    doc_name = doc.original_name if doc is not None and doc.original_name else doc_id
    kb_writer.fail_doc(doc_name, err)
