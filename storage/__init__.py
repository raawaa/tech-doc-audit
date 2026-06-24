"""存储层公共工具。"""

import re

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
