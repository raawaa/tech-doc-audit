"""Agent 对话跟踪（trace）的共享持久化。

审核（agentic_audit）与问答（agentic_qa）共用：把一次 agent 运行的完整对话
（系统提示、每轮 tool_calls 及其结果、reasoning）序列化成 JSON 落盘，便于事后
诊断 agent 行为。best-effort——写入失败返回 None，不影响运行结果。

调用方负责计算 trace 路径与 domain metadata；本模块负责消息截断
（content > 10000 / tool_calls.arguments > 5000）、注入 UTC timestamp、写盘
与异常吞没。
"""
import copy
import json
from datetime import datetime, timezone
from pathlib import Path

from core.logger import get_logger

_logger = get_logger(__name__)

_CONTENT_MAX = 10000
_ARGS_MAX = 5000


def _truncate_messages(messages: list[dict]) -> list[dict]:
    """深拷贝并截断过大的 content / tool_call arguments，避免 trace 文件膨胀。

    深拷贝保证不修改调用方传入的 messages。
    """
    out = []
    for m in messages:
        sm = copy.deepcopy(m)
        # content 可能为 None（assistant 只有 tool_calls 时）
        if sm.get("content") and len(str(sm["content"])) > _CONTENT_MAX:
            sm["content"] = str(sm["content"])[:_CONTENT_MAX] + (
                f"\n…[truncated from {len(str(m['content']))} chars]"
            )
        # tool_calls 中的 arguments 也可能很大
        if "tool_calls" in sm:
            for tc in sm["tool_calls"]:
                if "function" in tc and "arguments" in tc["function"]:
                    args_str = tc["function"]["arguments"]
                    if isinstance(args_str, str) and len(args_str) > _ARGS_MAX:
                        tc["function"]["arguments"] = args_str[:_ARGS_MAX] + "…[truncated]"
        out.append(sm)
    return out


def save_trace(
    trace_path: Path,
    messages: list[dict],
    *,
    metadata: dict | None = None,
) -> Path | None:
    """将一次 agent 对话序列化为 trace JSON。

    Args:
        trace_path: 目标文件路径（父目录会自动创建）。
        messages: 原始消息列表（深拷贝，不会被修改）。
        metadata: domain 元数据（task_id/doc_id/… 或 qa_id/question/…），
                  与自动注入的 ``timestamp``、截断后的 ``messages`` 一同写入。

    Returns:
        写入成功返回 ``trace_path``；任何异常返回 ``None``（best-effort）。
    """
    try:
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        trace = dict(metadata or {})
        trace["timestamp"] = datetime.now(timezone.utc).isoformat()
        trace["messages"] = _truncate_messages(messages)
        with open(trace_path, "w", encoding="utf-8") as f:
            json.dump(trace, f, ensure_ascii=False, indent=2)
        _logger.info(
            "trace saved: %s (%d messages, %.1f KB)",
            trace_path, len(messages), trace_path.stat().st_size / 1024,
        )
        return trace_path
    except Exception as e:
        _logger.warning("failed to save trace: %s", e)
        return None
