"""数据根目录解析 — 单点(issue #171 / PR-4)。

为什么单独成模块:
- 历史 ``get_data_dir()`` 在 9 个模块里各自重复一份 ``Path(os.environ.get(
  "AUDIT_DATA_DIR", "./data"))``,改默认值要 grep 全仓。
- 抽到本模块后,默认值 / env 变量名只在这一处改动 —— 一次 PR-4 收敛,
  与 ``core.kb_index_store`` 等解耦。
- ``storage`` / ``api`` 等下游模块依然在自己模块顶定义同名函数(留作
  "模块内调用入口"别名),真实读取走 ``from core.data_dir import get_data_dir``
  —— 避免一次性改 9 个 caller 的 import 路径,把本次 PR 的"删除
  ``core.index_manager``"边界守住(issue #165 §Implementation Decisions:
  "Hard rename — no shim layer")。

每次调用读取 env(issue #137 per-test 隔离):测试 conftest
``_per_test_data_dir`` autouse fixture 把 ``AUDIT_DATA_DIR`` 指向
``tmp_path``,存储层立即生效。
"""
from __future__ import annotations

import os
from pathlib import Path


def get_data_dir() -> Path:
    """解析数据根目录;每次调用读取 env(issue #137 per-test 隔离)。"""
    return Path(os.environ.get("AUDIT_DATA_DIR", "./data"))
