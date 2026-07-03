"""PaddleOCR 解析结果缓存层（#32 V1）。

按 (content_hash, model_version) 缓存到 ``data/.cache/paddleocr/``，命中跳过 OCR。
- model_version 来自环境变量 PADDLEOCR_MODEL，升级自动失效。
- file_hash 用 sha256，PDF 内容变更自动失效。
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
import shutil

from pathlib import Path

# 缓存根目录：项目 data/.cache/paddleocr/。模块级常量，方便测试 monkeypatch。
_DATA_DIR = Path(os.environ.get("AUDIT_DATA_DIR", "data"))
CACHE_DIR: Path = _DATA_DIR / ".cache" / "paddleocr"

# 模型版本：与 core.text_extraction 保持同源（env var）。升级即失效。
_MODEL_VERSION = os.environ.get("PADDLEOCR_MODEL", "PaddleOCR-VL-1.6")


def _file_hash(file_path: str) -> str:
    """sha256(file contents) — 32-hex。"""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _cache_path(file_path: str, model_version: str = _MODEL_VERSION) -> Path:
    """``{sha256}_{model_version}.json``。"""
    return CACHE_DIR / f"{_file_hash(file_path)}_{model_version}.json"


def get_cached(file_path: str) -> Optional[dict]:
    """命中且版本一致 → 返回 ``result`` 字段（dict）；未命中或失效 → None。

    缓存条目 schema::

        {
            "version": "<model_version>",
            "file_hash": "<sha256>",
            "parsed_at": "<iso8601>",
            "result": { ... },   # PaddleOCR 原始 JSONL 解析后的 dict
        }

    命中逻辑：``entry.version == _MODEL_VERSION AND entry.file_hash == current_hash``，
    任一不匹配返回 None（不抛）。
    """
    if not CACHE_DIR.exists():
        return None
    path = _cache_path(file_path)
    if not path.exists():
        return None
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # 缓存损坏：降级为未命中，不抛
        return None
    if entry.get("version") != _MODEL_VERSION:
        return None
    if entry.get("file_hash") != _file_hash(file_path):
        return None
    return entry.get("result")


def save_cached(
    file_path: str,
    result: dict,
    *,
    model_version: str = _MODEL_VERSION,
) -> Path:
    """落盘 ``{sha256}_{model_version}.json``，返回写入的路径。

    目录不存在自动创建。覆盖已有条目。"""
    path = _cache_path(file_path, model_version)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "version": model_version,
        "file_hash": _file_hash(file_path),
        "parsed_at": datetime.now(timezone.utc).isoformat(),
        "result": result,
    }
    path.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def clear_cache() -> int:
    """清空整个缓存目录，返回删除的文件数。

    运维工具（CLI 暂未做，预埋）。目录不存在时返回 0，不抛。"""
    if not CACHE_DIR.exists():
        return 0
    count = sum(1 for _ in CACHE_DIR.iterdir())
    shutil.rmtree(CACHE_DIR)
    return count
