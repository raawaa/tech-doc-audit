"""**截短返修报告 (Truncated Repair Report)** 存储层（spec #173 / issue #180）。

一次截短返修运行的结构化产出落在 ``data/kbs/{kb_id}/truncated_report.json``——
``bulk_reparse_report.json`` 的兄弟文件、同套心智模型：KB 磁盘产物、可读 JSON、
只留最近一次（与 :mod:`core.bulk_reparse_report_store` 完全对齐）。

设计要点（沿用 :mod:`core.bulk_reparse_report_store` 形态）：

- 只留**最近一次**运行；历史归档明确不在 v1 范围（与 ``bulk_reparse_report.json``
  同源决策：spec #102 / #173 都把"历史归档"列 out of scope）。
- 读取失败（文件不存在 / JSON 损坏）一律返回 ``None`` 并 log warning，不抛 ——
  复盘路径不该因为一个损坏的报告文件而崩掉。
- 写入用 ``indent=2`` + ``ensure_ascii=False``：报告是给人读的（文件名与失败原因
  都是中文），不是给机器压缩的。

为什么单独成模块（issue #180 review #2）：CLI 不再自带 report-store 实现，与
:mod:`core.bulk_reparse_report_store` 同源同构；后续若加报告端点（与 #111 的 bulk
报告端点同形态），本模块即唯一入口。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from core.data_dir import get_data_dir
from core.logger import get_logger

_logger = get_logger(__name__)


# 报告文件名。模块级常量：测试与未来的 report endpoint 都据此定位，不各自拼字符串。
REPORT_FILENAME = "truncated_report.json"


def _report_file(kb_id: str) -> Path:
    """``data/kbs/{kb_id}/truncated_report.json``（``pages/`` 的兄弟）。"""
    return get_data_dir() / "kbs" / kb_id / REPORT_FILENAME


def save_report(kb_id: str, report: dict) -> Path:
    """落盘一次截短返修报告，返回写入的路径。**覆盖**上一次的报告。"""
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
            "truncated_report_store: failed to load %s (%s): %s; treating as missing",
            path, type(e).__name__, e,
        )
        return None
