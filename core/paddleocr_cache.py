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
            "source": "<paddleocr|fallback_pdfplumber|fallback_docx|fallback_plain|empty>",
            "result": { ... },
        }

    命中逻辑：``entry.version == _MODEL_VERSION AND entry.file_hash == current_hash``，
    任一不匹配返回 None（不抛）。

    V8 cache defense (issue #57): 当 entry.source == "fallback_pdfplumber" 且
    PaddleOCR 当前可用时,返回 None 强制重解析 —— 这是「PaddleOCR 凭证首次配置后
    旧 fallback 产物 layout=[] 永远卡住」缺陷的根治。具体场景:
    部署时未设 PADDLEOCR_API_TOKEN → 走 pdfplumber 落 cache (layout=[]) →
    后来补上 token → 旧 cache 仍命中, V8 _inject_block_range 拿不到 layout。
    现在 token 就位后 get_cached 直接返回 None, 触发 PaddleOCR 重跑。
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
    # V8 cache defense: PaddleOCR 当前可用时, 旧 fallback_pdfplumber 产物视为污染
    if entry.get("source") == "fallback_pdfplumber" and _paddleocr_currently_available():
        return None
    return entry.get("result")


def _paddleocr_currently_available() -> bool:
    """检查 PaddleOCR API 凭证当前是否配置。环境变量由 core.parse_document 维护,
    这里只读取,避免循环 import。
    """
    token = os.environ.get("PADDLEOCR_API_TOKEN", "").strip()
    url = os.environ.get("PADDLEOCR_API_URL", "").rstrip("/")
    return bool(token and url)


def save_cached(
    file_path: str,
    result: dict,
    *,
    model_version: str = _MODEL_VERSION,
    source: str = "paddleocr",
) -> Path:
    """落盘 ``{sha256}_{model_version}.json``，返回写入的路径。

    目录不存在自动创建。覆盖已有条目。

    ``source`` (V8 cache defense, 见 issue #57): 标识 cache 内容的来源解析器。
    - ``"paddleocr"`` (默认): 真实 PaddleOCR-VL 产物，layout 非空
    - ``"fallback_pdfplumber"``: PaddleOCR 不可用 / 失败时降级 pdfplumber，layout=[]
    - ``"fallback_docx"`` / ``"fallback_plain"`` / ``"empty"``: 非 PDF 路径

    ``get_cached`` 命中时，``fallback_pdfplumber`` 条目会被视为可疑并强制重解析
    （PaddleOCR 当前可用时）。
    """
    path = _cache_path(file_path, model_version)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "version": model_version,
        "file_hash": _file_hash(file_path),
        "parsed_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
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
