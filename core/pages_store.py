"""KB 文档按页文本存储层（PRD #29 / V3）。

按页文本 + layout 数据是 **KB 文档层结构**，不应再挂在 ``doc.metadata`` 里。
存储位置：``data/kbs/{kb_id}/pages/{doc_id}.json``。

设计要点：
- 内容是 ParseResult 的 JSON 表示，schema 与 PaddleOCR 缓存条目 ``result`` 字段对齐。
- 目录不存在自动创建；文件存在则覆盖。
- 读取失败（文件不存在 / JSON 损坏）一律返回 None 并 log warning，
  让调用方决定降级策略（不要在这里抛异常，KB 旧文档本来就缺 pages 文件）。
- 删除文档时同步清理（``delete_pages``）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from core.logger import get_logger

_logger = get_logger(__name__)


DATA_DIR = Path(os.environ.get("AUDIT_DATA_DIR", "./data"))


def _pages_dir(kb_id: str) -> Path:
    """``data/kbs/{kb_id}/pages/``。"""
    return DATA_DIR / "kbs" / kb_id / "pages"


def _pages_file(kb_id: str, doc_id: str) -> Path:
    """``data/kbs/{kb_id}/pages/{doc_id}.json``。"""
    return _pages_dir(kb_id) / f"{doc_id}.json"


def save_pages(
    kb_id: str,
    doc_id: str,
    parse_result: dict,
    *,
    file_hash: Optional[str] = None,
    model_version: Optional[str] = None,
    parsed_at: Optional[str] = None,
) -> Path:
    """把 ParseResult（dict 形式）落盘到 ``data/kbs/{kb_id}/pages/{doc_id}.json``。

    Args:
        kb_id: 知识库 ID。
        doc_id: 文档 ID。
        parse_result: ParseResult 序列化后的 dict，含 ``by_page`` / ``full_text`` / ``layout``。
        file_hash: 可选，写入缓存条目便于审计对应。
        model_version: 可选，模型版本。
        parsed_at: 可选，ISO8601 字符串；缺省为当前 UTC。

    Returns:
        落盘后的 Path。
    """
    from datetime import datetime, timezone

    path = _pages_file(kb_id, doc_id)
    path.parent.mkdir(parents=True, exist_ok=True)

    if parsed_at is None:
        parsed_at = datetime.now(timezone.utc).isoformat()

    payload = dict(parse_result)
    payload["doc_id"] = doc_id
    payload["kb_id"] = kb_id
    if file_hash is not None:
        payload["file_hash"] = file_hash
    if model_version is not None:
        payload["model_version"] = model_version
    if parsed_at is not None:
        payload["parsed_at"] = parsed_at

    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def load_pages(kb_id: str, doc_id: str) -> Optional[dict]:
    """读取 ``pages/{doc_id}.json``，返回 dict；不存在或损坏 → None。

    损坏 JSON 不抛异常，仅 ``logger.warning``：
    这是 KB 旧文档常见状态，调用方应降级为空 dict / 走另一条路径，
    而非因损坏文件让整条审核流程抛错。
    """
    path = _pages_file(kb_id, doc_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        _logger.warning(
            "pages_store: failed to load %s (%s): %s; treating as missing",
            path, type(e).__name__, e,
        )
        return None


def delete_pages(kb_id: str, doc_id: str) -> bool:
    """删除 ``pages/{doc_id}.json``；存在并删除 → True，不存在 → False。

    文档删除路径必须同步清理 pages 文件，否则会留下空洞文件。
    """
    path = _pages_file(kb_id, doc_id)
    if not path.exists():
        return False
    try:
        path.unlink()
        return True
    except OSError as e:
        _logger.warning("pages_store: failed to delete %s: %s", path, e)
        return False
