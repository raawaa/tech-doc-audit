"""KB 文档重新解析服务（PRD #29 / V4）。

``reparse_document(doc_id)`` 走与导入相同的流水线：
``parse_document`` → ``pages_store.save_pages`` → 重建向量索引 → 更新 ``embedding_status``。

设计上：
- 立即返回（异步）；后台任务在 KB 级锁内执行，避免与重建索引混线。
- 任何步骤失败 → ``embedding_status=failed`` + ``index_current_doc`` 写错误信息（沿用现有契约）。
- KB 级检索状态（``index_status`` / ``index_progress`` / ``index_current_doc``）由
  ``core.kb_index_status.KbIndexStatusWriter``（issue #148）唯一写入。单篇入口默认
  自己造 ``total=1`` 的 writer，构造后**立即** ``begin()``，把 KB 拍到 building+0
  再起后台线程；批量重新解析（``services.bulk_reparse_service``）注入编排层共享的
  writer（``total=N``），由此函数在自己的 begin()/finish() 之间干活，编排层在批次首尾再
  begin()/finish() 收尾，整批期间 KB 不会在 ``building ⇄ searchable`` 间抖动。
- 完整逆向兼容：老 ``page_texts`` 路径仍在 ``import_document`` 里；reparse 走新路径。
"""
from __future__ import annotations

import threading
from typing import Optional

from core.kb_index_status import KbIndexStatusWriter
from core.kb_index_store import KBIndexStore
from core.logger import get_logger
from core.parse_document import parse_document, MIN_FULL_TEXT_CHARS, ParseResult
from core.pages_store import save_pages
from core.index_manager import (
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
            整批期间 KB 状态由编排层独占管理；调用方**必须**已经 ``begin()``
            过（issue #155：避免批量场景下每篇 per-doc 线程再 begin()，变成
            N+1 次把 ``_progress`` 清零 + 把 ``index_progress=0.0`` 写盘）。
            默认 ``None`` —— 函数自己构造一个 ``KbIndexStatusWriter(kb_id, total=1)``
            并立即 ``begin()``，走完整生命周期（开头 building、终态 searchable|failed）。
            无论是否传入，doc 级 ``embedding_status`` 都照常写。

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

    # 默认自己管 KB 状态；批量注入时由编排层管生命周期 —— 注入路径必须已 begin()
    # （详见 docstring 与 issue #155）。
    if kb_writer is None:
        kb_writer = KbIndexStatusWriter(doc.kb_id, total=1)
        kb_writer.begin()

    thread = threading.Thread(
        target=_reparse_async,
        args=(doc.kb_id, doc_id, kb_writer),
        daemon=True,
    )
    thread.start()

    return {"status": "pending_index", "doc_id": doc_id}


def _resolve_doc_and_kb(
    kb_id: str, doc_id: str,
) -> tuple[Optional[object], Optional[object]]:
    """查 doc + kb；缺失位置为 ``None``（#156 保留 mark_failed/silent-return 不对称语义）。"""
    doc = doc_repo.get_doc(kb_id, doc_id)
    if not doc or not doc.file_path:
        return None, None
    kb = kb_repo.get(kb_id)
    if not kb:
        return doc, None
    return doc, kb


def _parse_with_guards(doc_id: str, file_path: str) -> ParseResult:
    """``parse_document`` + 三道守卫（#94 假成功指纹防御）。失败 → ``RuntimeError``。"""
    parse_result = parse_document(file_path)
    if not parse_result.full_text or len(parse_result.full_text) < MIN_FULL_TEXT_CHARS:
        raise RuntimeError("parse_document returned empty/sparse text")
    if not parse_result.layout:
        raise RuntimeError(f"empty layout for {doc_id}")
    if not parse_result.by_page:
        raise RuntimeError(f"empty by_page for {doc_id}")
    return parse_result


def _persist_index(kb_id: str, doc_id: str, parse_result: ParseResult, doc) -> None:
    """save_pages → remove_document → index_document；不获取锁。"""
    save_pages(
        kb_id, doc_id, parse_result.to_dict(),
        file_hash=doc.content_hash,
    )
    try:
        remove_document(kb_id, doc_id)
    except Exception as e:
        _logger.warning("reparse: failed to remove old nodes for %s: %s", doc_id, e)
    index_document(
        kb_id, doc_id, parse_result.full_text,
        source_name=doc.original_name,
        by_page=parse_result.by_page,
        by_layout=parse_result.layout,
    )


def _reparse_async(
    kb_id: str, doc_id: str, kb_writer: KbIndexStatusWriter
) -> None:
    """后台执行：parse → save_pages → 重建索引 → 更新状态。

    KB 状态字段全部走 writer（#155：begin() 由 caller 承担，本函数只调
    ``note_in_flight`` / ``finish`` / ``fail_doc``）。
    """
    doc, kb = _resolve_doc_and_kb(kb_id, doc_id)
    if doc is None:
        _mark_failed(kb_id, doc_id, "doc or file_path missing", kb_writer=kb_writer)
        return
    if kb is None:
        return

    kb_writer.note_in_flight(doc.original_name)

    # Issue #168: per-KB 锁由 ``KBIndexStore`` 封装,外部通过
    # ``acquire_write_lock()`` 拿 contextmanager(而非 RLock 对象)。
    with KBIndexStore.open(kb_id).acquire_write_lock():
        try:
            parse_result = _parse_with_guards(doc_id, doc.file_path)
            _persist_index(kb_id, doc_id, parse_result, doc)
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
            _mark_failed(kb_id, doc_id, str(e), kb_writer=kb_writer)


def _mark_failed(
    kb_id: str, doc_id: str, err: str, *, kb_writer: KbIndexStatusWriter
) -> None:
    """doc 失败 → 写 ``embedding_status="failed"`` + 走 writer 收尾。

    KB 终态由 writer 决定：``total=1`` 写 ``failed``，``total>1`` 只写错误摘要。
    """
    doc = doc_repo.get_doc(kb_id, doc_id)
    if doc is not None:
        doc.embedding_status = "failed"
        doc_repo._save_doc_meta(doc)

    doc_name = doc.original_name if doc is not None and doc.original_name else doc_id
    kb_writer.fail_doc(doc_name, err)
