"""PaddleOCR 解析结果缓存层（#32 V1）。

按 (content_hash, model_version) 缓存到 ``data/.cache/paddleocr/``，命中跳过 OCR。
- model_version 来自环境变量 PADDLEOCR_MODEL，升级自动失效。
- file_hash 用 sha256，PDF 内容变更自动失效。
- source 记录解析器来源：``paddleocr``、``pymupdf`` 或非 PDF 路径的来源值。
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
import shutil

from pathlib import Path
from typing import Optional

def get_data_dir() -> Path:
    """解析数据根目录；每次调用读取 env（issue #137 per-test 隔离）。"""
    return Path(os.environ.get("AUDIT_DATA_DIR", "data"))


def get_cache_dir() -> Path:
    """缓存根目录：``data/.cache/paddleocr/``。每次调用解析，测试可 monkeypatch。"""
    return get_data_dir() / ".cache" / "paddleocr"

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
    return get_cache_dir() / f"{_file_hash(file_path)}_{model_version}.json"


def get_cached(file_path: str) -> Optional[dict]:
    """命中且版本一致 → 返回 ``result`` 字段（dict）；未命中或失效 → None。

    缓存条目 schema::

        {
            "version": "<model_version>",
            "file_hash": "<sha256>",
            "parsed_at": "<iso8601>",
            "source": "<paddleocr|pymupdf|fallback_docx|fallback_plain|empty>",
            "result": { ... },
        }

    命中逻辑：``entry.version == _MODEL_VERSION AND entry.file_hash == current_hash``，
    任一不匹配返回 None（不抛）。
    """
    if not get_cache_dir().exists():
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


def _paddleocr_currently_available() -> bool:
    """检查 PaddleOCR API 凭证当前是否配置。环境变量由 core.parse_document 维护,
    这里只读取,避免循环 import。

    注：#99/05 删除 ``_pdf_fallback`` 后,``_paddleocr_currently_available`` 不再
    驱动运行时分支(parse_document 直接跑 PaddleOCR 或抛 RuntimeError),仅作诊断
    与历史 cache 条目来源识别用。
    """
    token = os.environ.get("PADDLEOCR_API_TOKEN", "").strip()
    url = os.environ.get("PADDLEOCR_API_URL", "").rstrip("/")
    return bool(token and url)


# 缓存状态：给"不想真解析、只想知道会不会烧配额"的调用方（如批量重新解析的
# OCR 成本预检）用。判定规则与 get_cached 同源 —— 历史 source=fallback_pdfplumber
# 条目在 #99/05 后已被删除 V8 defense；这里仅识别"有无条目 + 版本一致 + JSON 可读"。
CACHE_STATE_HIT = "cached"
CACHE_STATE_MISS = "uncached"

# 真正消耗 OCR 配额的那个 source 值。批量重新解析的**实测**计数（#110）按它分桶：
# 只有 source=paddleocr 的条目才代表配额支出，其余（pymupdf / fallback_*）都不烧。
SOURCE_PADDLEOCR = "paddleocr"


def _entry_by_hash(content_hash: str, version: str) -> Optional[dict]:
    """按 ``(content_hash, model_version)`` 读一条缓存条目；判废则返回 ``None``。

    命中判定的**唯一实现**，``cache_state_by_hash`` 与 ``cache_source_by_hash``
    共用 —— 两个问题（"会不会烧配额" / "实际是谁解析的"）问的是同一条条目，
    判废规则一旦分叉，预检与实测就会互相矛盾（正是 #91 那类误报的温床）。

    判废口径与 ``get_cached`` 一致：无条目 / JSON 损坏 / ``version`` 不符。
    **不校验 ``file_hash``** —— 调用方给的就是 doc 侧记录的哈希，无从比对文件内容。
    """
    if not content_hash:
        return None
    path = get_cache_dir() / f"{content_hash}_{version}.json"
    if not path.exists():
        return None
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # 缓存损坏：get_cached 会降级为未命中并重解析，估算与实测必须同口径
        return None
    if entry.get("version") != version:
        return None
    return entry


def cache_state_by_hash(content_hash: str, *, model_version: Optional[str] = None) -> str:
    """按**已知的** ``content_hash`` 判断缓存状态，不重算文件哈希。

    ``get_cached`` 从 ``file_path`` 现算 sha256；成本预检要对几百篇 doc 问同一个问题，
    而哈希早已记在 ``doc.content_hash`` 上 —— 重算等于为了估算把整库 PDF 读一遍。

    返回值（与 ``get_cached`` 的命中判定同源）：
    - ``CACHE_STATE_MISS`` —— 无条目 / 条目损坏 / model_version 不符（``get_cached`` 同样返回 None）
    - ``CACHE_STATE_HIT`` —— 条目存在且版本一致

    注：``source`` 字段（``paddleocr`` / ``pymupdf`` / ``fallback_*``）不影响配额
    判定 —— 真正决定是否烧 OCR 的是 ``parse_document`` 拿到结果后的链路，
    不是预检阶段就能精确知道的。预检乐观地按"有缓存条目就当命中"，
    实测由 ``cache_source_by_hash`` 在跑完之后收（#110）。
    """
    version = model_version or _MODEL_VERSION
    entry = _entry_by_hash(content_hash, version)
    return CACHE_STATE_MISS if entry is None else CACHE_STATE_HIT


def cache_source_by_hash(content_hash: str, *, model_version: Optional[str] = None) -> Optional[str]:
    """按**已知的** ``content_hash`` 回读该条目的 ``source``；判废或无 source → ``None``。

    这是批量重新解析实测 OCR 消耗的**地面真相**（#110）：#93 事后正是靠数
    ``source=paddleocr`` 的条目才证明"154 篇真的跑了 OCR"，本函数把那次考古
    变成流程产物。跑完一篇就回读一次，静默降级（本该 OCR 却走了别的路）当场可见。

    返回 ``None`` 表示"读不到来源"，调用方**不得**默认按 OCR 记账 ——
    凭空补一个好看的页数正是 #90 报 1694 页而实际 0 页的错法。
    """
    version = model_version or _MODEL_VERSION
    entry = _entry_by_hash(content_hash, version)
    if entry is None:
        return None
    return entry.get("source") or None


def save_cached(
    file_path: str,
    result: dict,
    *,
    model_version: str = _MODEL_VERSION,
    source: str = SOURCE_PADDLEOCR,
) -> Path:
    """落盘 ``{sha256}_{model_version}.json``，返回写入的路径。

    目录不存在自动创建。覆盖已有条目。

    ``source`` 标识 cache 内容的来源解析器。
    - ``"paddleocr"`` (默认): PaddleOCR-VL 产物，layout 非空
    - ``"pymupdf"``: 文字层 PDF 的 PyMuPDF 解析（issue #99）
    - ``"fallback_docx"`` / ``"fallback_plain"`` / ``"empty"``: 非 PDF 路径
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
    cache_dir = get_cache_dir()
    if not cache_dir.exists():
        return 0
    count = sum(1 for _ in cache_dir.iterdir())
    shutil.rmtree(cache_dir)
    return count
