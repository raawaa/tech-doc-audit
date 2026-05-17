"""索引构建服务（简化版 — 向量检索无需预建索引）。

向量检索采用 indexless 方式，无需预建索引。
保留此模块仅为兼容已有 API/CLI 调用路径（reindex 等），
所有构建操作改为 no-op。
"""

import os
from pathlib import Path
from datetime import datetime

from models.document import KBDocument
import storage.doc_repo as doc_repo
import storage.index_repo as index_repo
import storage.kb_repo as kb_repo


def build_index_for_doc(doc: KBDocument) -> KBDocument:
    """无需预建索引。标记为 ready 即可。"""
    doc.index_status = "ready"
    doc_repo._save_doc_meta(doc)
    _update_kb_index_status(doc.kb_id)
    return doc


def rebuild_kb_index(kb_id: str) -> None:
    """重建知识库所有文档的索引 — no-op。"""
    kb = kb_repo.get(kb_id)
    if not kb:
        raise ValueError(f"知识库不存在: {kb_id}")

    kb.index_status = "ready"
    kb_repo.update(kb)

    docs = doc_repo.list_docs(kb_id)
    for doc in docs:
        doc.index_status = "ready"
        doc_repo._save_doc_meta(doc)


def _update_kb_index_status(kb_id: str) -> None:
    """根据文档索引状态更新知识库整体索引状态。"""
    kb = kb_repo.get(kb_id)
    if not kb:
        return

    docs = doc_repo.list_docs(kb_id)
    if not docs:
        kb.index_status = "none"
    elif all(d.index_status == "ready" for d in docs):
        kb.index_status = "ready"
    elif any(d.index_status == "building" for d in docs):
        kb.index_status = "building"
    else:
        kb.index_status = "failed"
    kb_repo.update(kb)
