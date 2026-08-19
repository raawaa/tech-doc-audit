"""存储层公共工具。"""

import json
import os
import re
from pathlib import Path
from typing import Any

# 允许字母、数字、连字符、下划线，长度 1-64 字符。
# 拒绝路径分隔符（/ \）和点号序列（..），防止路径遍历。
_VALID_ID_RE = re.compile(r'^[0-9A-Za-z_\-]{1,64}$')


def validate_id(id_: str, name: str = "ID") -> str:
    """校验 ID 格式，防止路径遍历攻击。

    只允许字母、数字、下划线和连字符。
    拒绝含路径分隔符（/ \\ ..）的值。

    Args:
        id_: 待校验的 ID 字符串。
        name: ID 的显示名称（用于错误信息）。

    Returns:
        原样返回合法的 ID。

    Raises:
        ValueError: ID 格式不合法。
    """
    if not id_ or not _VALID_ID_RE.match(id_):
        raise ValueError(f"非法 {name}: {id_!r}")
    return id_


def atomic_write_json(path: Path, data: Any) -> None:
    """原子写 JSON：先写 ``.tmp`` 再 ``os.replace``，避免 read 端撞半截。

    旧 ``open(path, "w")`` + ``json.dump`` 是 truncate-then-write，daemon 线程
    写期间 reader 会看到 truncate 已发生 / dump 未完成的空文件 → JSONDecodeError
    （issue #157）。``os.replace`` 在 POSIX 上原子：reader 要么看到旧版要么
    看到完整新版。失败时清理遗留 ``.tmp``，避免多次崩溃后磁盘堆积。
    """
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except BaseException:
        # 任何失败（dump 中断 / rename 失败）都清理未完成的 tmp。
        # .tmp 文件名约定不会被 read 路径读取，清理是卫生而非正确性。
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise
