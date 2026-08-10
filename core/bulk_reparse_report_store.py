"""**批量重新解析报告 (Bulk Reparse Report)** 存储层（issue #110 / spec #102）。

一次批量重新解析的结构化产出落在 ``data/kbs/{kb_id}/bulk_reparse_report.json``——
与 ``pages/`` 同级，同属该 KB 的磁盘产物。

为什么落盘而不是塞进 ``kb.metadata``：与 ``CONTEXT.md`` 里"按页文本不写在
``doc.metadata``"同源的理由 —— metadata 无 schema，随字段增长膨胀，而报告天然是
一份带嵌套明细的文档。

设计要点（与 ``core.pages_store`` 保持同一形状）：
- 只留**最近一次**运行；历史归档明确不在 v1 范围（spec #102 Out of Scope 4）。
- 读取失败（文件不存在 / JSON 损坏）一律返回 ``None`` 并 log warning，不抛 ——
  复盘路径不该因为一个损坏的报告文件而崩掉。
- 写入用 ``indent=2`` + ``ensure_ascii=False``：报告是给人读的（文件名与失败原因
  都是中文），不是给机器压缩的。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from core.logger import get_logger

_logger = get_logger(__name__)


def get_data_dir() -> Path:
    """解析数据根目录；每次调用读取 env（issue #137 per-test 隔离）。"""
    return Path(os.environ.get("AUDIT_DATA_DIR", "./data"))


# 报告文件名。模块级常量：测试与 #111 的报告端点都据此定位，不各自拼字符串。
REPORT_FILENAME = "bulk_reparse_report.json"


def _report_file(kb_id: str) -> Path:
    """``data/kbs/{kb_id}/bulk_reparse_report.json``（``pages/`` 的兄弟）。"""
    return get_data_dir() / "kbs" / kb_id / REPORT_FILENAME


def save_report(kb_id: str, report: dict) -> Path:
    """落盘一次批量重新解析报告，返回写入的路径。**覆盖**上一次的报告。"""
    path = _report_file(kb_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_report(kb_id: str) -> Optional[dict]:
    """读取最近一次报告；从未跑过 / 文件损坏 → ``None``（不抛）。"""
    path = _report_file(kb_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        _logger.warning(
            "bulk_reparse_report_store: failed to load %s (%s): %s; treating as missing",
            path, type(e).__name__, e,
        )
        return None
