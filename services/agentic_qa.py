"""Agentic 知识库问答。

ReAct agent loop：LLM 自主调用 search_kb / search_kb_text 多轮搜索知识库，
根据搜索结果质量动态调整搜索策略，最终给出带来源引用的答案。

对比 qa_service.py（纯 RAG 管道：一次性检索 → LLM 回答），
Agentic 方式允许 LLM 在搜索结果不理想时换关键词重搜，更接近人类查阅资料的行为。
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from core.logger import get_logger
from core.degradation import record as _deg_record
from core.settings import make_deepseek_client
from services.agent_tools import search_kb, search_kb_text

_logger = get_logger(__name__)

# trace 文件存放目录
_TRACE_DIR = Path(
    os.environ.get("AUDIT_DATA_DIR", "data")
) / "qa_traces"

MAX_ITERATIONS = 20


# ═══════════════════════════════════════════════════════════════════════════════
# System Prompt
# ═══════════════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """你是一个知识库问答助手。你的任务是利用搜索工具在知识库中查找相关信息来回答用户的问题。

## 工作流程

1. 仔细分析用户的问题，提炼出核心概念和关键词
2. 根据问题类型选择合适的搜索工具
3. 阅读搜索结果，判断是否足以回答问题
4. 如果搜索结果不够——换关键词、换角度、换工具再搜
5. 如果搜索结果足够——基于检索到的内容给出答案
6. 在答案中引用具体的来源文档和条款

## 搜索策略

search_kb（语义向量搜索）vs search_kb_text（精确文本搜索）的选择规则：
- search_kb：适合搜索概念性、描述性的问题（如"质保期有什么要求"、"验收标准是什么"），能匹配同义词和近义表达
- search_kb_text：适合搜索具体术语、编号、参数（如"GB/T 12345"、"IP65"、"3.2.1条"），精确命中
- 遇到标准编号/参数值/专有名词时优先用 search_kb_text
- 遇到概念描述时优先用 search_kb
- 搜索结果不理想就换词重搜，不要放弃太早
- 可以从不同角度多次搜索同一问题

## 回答要求

- 答案必须基于搜索结果，不要凭记忆或猜测
- 引用具体来源：文档名称、条款编号
- 如果知识库中没有相关信息，诚实说明，不要编造
- 如果搜索结果不足以完整回答问题，说明已知的部分和不确定的部分
- 多轮对话时，结合上下文理解用户的追问意图
- 回答简洁专业，使用中文"""


# ═══════════════════════════════════════════════════════════════════════════════
# 工具定义
# ═══════════════════════════════════════════════════════════════════════════════

_TOOLS_SPEC = [
    {
        "type": "function",
        "function": {
            "name": "search_kb",
            "description": (
                "在知识库中进行语义向量搜索，查找与查询概念语义相近的内容。"
                "适合搜索概念性、描述性的问题（如「质保期要求」、「验收标准」、「防水等级」），"
                "能够匹配同义词和近义表达，但无法匹配精确的编号或代码。"
                "返回结果按相关度降序排列，每条包含：【文档名称】、条款编号、相关度分数（0~1）、"
                "以及该条款前500字符的内容。"
                "与 search_kb_text 的区别：本工具使用语义向量匹配，能理解概念但返回较慢且不保证精确编号命中；"
                "search_kb_text 使用 rga/rg 精确文本匹配，速度快、不占GPU，适合搜索标准编号及专有名词。"
                "不要使用本工具的情形：(1)搜索词是精确的标准编号/参数值/专有名词时，请改用 search_kb_text；"
                "(2)已用同一关键词搜索过且相关度均低于0.3，应换词重搜而非重复相同查询。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "搜索关键词或概念描述。"
                            "示例：'质保期要求'、'验收标准'、'防雷接地规范'。"
                            "不要输入完整句子，用2-5个词的关键词短语。"
                        ),
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "返回结果数量，默认5。相关度低时可增至8-10。",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_kb_text",
            "description": (
                "在知识库中做精确关键词文本搜索（基于 rga/rg 全文检索，非语义匹配）。"
                "适合搜索具体的标准编号（如GB/T 12345）、参数值（如3000m²、IP65）、"
                "专有名词（如'镀锌钢管'、'环氧树脂'）等需要精确命中的术语。"
                "速度快、不占用GPU，但无法匹配同义词或语义相近的表达——若搜索概念性要求，请改用 search_kb。"
                "返回结果最多2000字符，格式为 rga/rg 的原始匹配行（含文件名、行号、上下文）。"
                "不要使用本工具的情形：(1)需要搜索概念性或描述性内容时，请用 search_kb；"
                "(2)搜索词过于宽泛（如单个字），会产生大量噪声结果。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "精确搜索关键词。示例：'GB/T 12345'、'IP65'。"
                            "输入具体的标准编号、参数值或专有术语。"
                        ),
                    },
                },
                "required": ["query"],
            },
        },
    },
]


# ═══════════════════════════════════════════════════════════════════════════════
# 工具分发（search_kb / search_kb_text 实现见 services.agent_tools）
# ═══════════════════════════════════════════════════════════════════════════════

def _execute_tool(func_name: str, args: dict, kb_ids: list[str]) -> str:
    """工具分发。"""
    if func_name == "search_kb":
        return search_kb(kb_ids, args.get("query", ""), args.get("top_k", 5))
    elif func_name == "search_kb_text":
        return search_kb_text(kb_ids, args.get("query", ""))
    return f"未知工具: {func_name}。可用工具：search_kb、search_kb_text。"


# ═══════════════════════════════════════════════════════════════════════════════
# Trace 持久化
# ═══════════════════════════════════════════════════════════════════════════════

def _save_trace(
    qa_id: str,
    question: str,
    kb_ids: list[str],
    total_iterations: int,
    messages: list[dict],
    *,
    finished: bool = True,
) -> Path | None:
    """保存完整对话跟踪。"""
    try:
        trace_dir = _TRACE_DIR
        trace_dir.mkdir(parents=True, exist_ok=True)
        trace_path = trace_dir / f"{qa_id}_trace.json"

        serializable_messages = []
        for m in messages:
            sm = dict(m)
            if sm.get("content") and len(str(sm["content"])) > 10000:
                sm["content"] = str(sm["content"])[:10000] + (
                    f"\n…[truncated from {len(str(m['content']))} chars]"
                )
            if "tool_calls" in sm:
                for tc in sm["tool_calls"]:
                    if "function" in tc and "arguments" in tc["function"]:
                        args_str = tc["function"]["arguments"]
                        if isinstance(args_str, str) and len(args_str) > 5000:
                            tc["function"]["arguments"] = (
                                args_str[:5000] + "…[truncated]"
                            )
            serializable_messages.append(sm)

        trace = {
            "qa_id": qa_id,
            "question": question,
            "kb_ids": kb_ids,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "finished": finished,
            "total_iterations": total_iterations,
            "messages": serializable_messages,
        }

        with open(trace_path, "w", encoding="utf-8") as f:
            json.dump(trace, f, ensure_ascii=False, indent=2)

        _logger.info("qa trace saved: %s (%d messages, %.1f KB)",
                      trace_path, len(messages), trace_path.stat().st_size / 1024)
        return trace_path
    except Exception as e:
        _logger.warning("failed to save qa trace: %s", e)
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# Agent Loop
# ═══════════════════════════════════════════════════════════════════════════════

def run_agentic_qa(
    question: str,
    kb_ids: list[str],
    *,
    chat_history: list[dict] | None = None,
    event_callback: Callable[[dict], None] | None = None,
    qa_id: str = "",
) -> dict:
    """Agentic 知识库问答主入口。

    Args:
        question: 用户问题
        kb_ids: 要搜索的知识库 ID 列表
        chat_history: 可选的多轮对话历史，格式 [{"role": "user"|"assistant", "content": ...}]
        event_callback: 流式事件回调，接收 {"type": "...", ...} 字典
        qa_id: 用于 trace 文件命名的标识

    Returns:
        {"answer": str, "sources": list[dict]}
    """
    def _emit(event: dict):
        if event_callback:
            try:
                event_callback(event)
            except Exception:
                pass

    _emit({"type": "start", "message": "Agentic 问答开始"})

    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

    # 原生 OpenAI SDK client；代理绕过集中在 core.settings.make_deepseek_client
    client = make_deepseek_client()

    # 构建消息历史
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
    ]

    if chat_history:
        for msg in chat_history:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role in ("user", "assistant"):
                messages.append({"role": role, "content": content})

    messages.append({"role": "user", "content": question})

    answer = ""
    finished = False

    for iteration in range(MAX_ITERATIONS):
        try:
            stream = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=_TOOLS_SPEC,
                stream=True,
                extra_body={"thinking": {"type": "enabled"}},
            )
        except Exception as e:
            _emit({"type": "error", "message": f"LLM 调用失败: {e}"})
            _logger.warning("agentic_qa: chat.completions failed: %s", e)
            answer = f"抱歉，问答服务暂时不可用。（{e}）"
            break

        # 流式接收响应：逐 token 发送推理过程和回答文本，累积 tool call 参数
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_call_accumulators: dict[int, dict] = {}  # index → {id, name, arguments}
        reasoning_started = False
        reasoning_ended = False
        text_started = False

        try:
            for chunk in stream:
                delta = chunk.choices[0].delta

                # DeepSeek thinking 模式：逐 token 发送推理过程
                rc = getattr(delta, 'reasoning_content', None)
                if rc:
                    if not reasoning_started:
                        reasoning_started = True
                        _emit({"type": "reasoning_start"})
                    reasoning_parts.append(rc)
                    _emit({"type": "reasoning_delta", "content": rc})

                # 逐 token 发送回答文本
                if delta.content:
                    if reasoning_started and not reasoning_ended:
                        _emit({"type": "reasoning_end"})
                        reasoning_ended = True
                    if not text_started:
                        text_started = True
                        _emit({"type": "text_start"})
                    content_parts.append(delta.content)
                    _emit({"type": "text_delta", "content": delta.content})

                # 累积 tool call 参数（流式下 tool call 参数分多个 chunk 到达）
                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        if idx not in tool_call_accumulators:
                            tool_call_accumulators[idx] = {
                                "id": "",
                                "name": "",
                                "arguments": "",
                            }
                        acc = tool_call_accumulators[idx]
                        if tc_delta.id:
                            acc["id"] = tc_delta.id
                        if tc_delta.function:
                            if tc_delta.function.name:
                                acc["name"] = tc_delta.function.name
                            if tc_delta.function.arguments:
                                acc["arguments"] += tc_delta.function.arguments
        except Exception as e:
            _emit({"type": "error", "message": f"流式响应中断: {e}"})
            _logger.warning("agentic_qa: stream interrupted: %s", e)
            answer = f"抱歉，问答服务暂时不可用。（流式响应中断: {e}）"
            break

        # 关闭未完成的阶段
        if reasoning_started and not reasoning_ended:
            _emit({"type": "reasoning_end"})
        if text_started:
            _emit({"type": "text_end"})

        content = "".join(content_parts)
        reasoning_content = "".join(reasoning_parts)

        # 构建 tool calls 列表（按 index 排序确保顺序正确）
        tool_calls_list: list[dict] = []
        for idx in sorted(tool_call_accumulators.keys()):
            acc = tool_call_accumulators[idx]
            if acc["id"] and acc["name"]:
                tool_calls_list.append({
                    "id": acc["id"],
                    "type": "function",
                    "function": {
                        "name": acc["name"],
                        "arguments": acc["arguments"],
                    },
                })

        # 追加 assistant 消息
        assistant_msg: dict = {"role": "assistant", "content": content}
        if reasoning_content:
            assistant_msg["reasoning_content"] = reasoning_content
        if tool_calls_list:
            assistant_msg["tool_calls"] = tool_calls_list
        messages.append(assistant_msg)

        # 没有工具调用 → 模型给出了最终回答
        if not tool_calls_list:
            answer = content
            finished = True
            _logger.info("agentic_qa finished after %d iterations", iteration + 1)
            break

        # 执行工具（参数已在流式接收时累积完整）
        for tc in tool_calls_list:
            func_name = tc["function"]["name"]
            try:
                func_args = json.loads(tc["function"]["arguments"])
            except json.JSONDecodeError:
                func_args = {}

            _emit({"type": "tool_call", "tool": func_name, "args": func_args})

            try:
                tool_result = _execute_tool(func_name, func_args, kb_ids)
            except Exception as e:
                tool_result = f"工具执行失败: {e}"
                _emit({"type": "error", "message": f"{func_name} 执行失败: {e}"})

            _emit({"type": "tool_result", "tool": func_name, "content": tool_result})

            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": tool_result,
            })
    else:
        _deg_record("agentic_qa", "max_iterations",
                     f"Reached {MAX_ITERATIONS} iterations")
        answer = "抱歉，问答搜索次数已达上限，请尝试缩小问题范围。"
        _emit({"type": "error", "message": answer})

    # 提取 sources（从 messages 中收集搜索工具返回的结果）
    sources = _extract_sources(messages)

    # 持久化 trace
    _save_trace(
        qa_id=qa_id or "unknown",
        question=question,
        kb_ids=kb_ids,
        total_iterations=iteration + 1,
        messages=messages,
        finished=finished,
    )

    return {"answer": answer, "sources": sources}


def _extract_sources(messages: list[dict]) -> list[dict]:
    """从消息历史中提取知识库来源引用。"""
    sources = []
    seen = set()
    for m in messages:
        if m.get("role") != "tool":
            continue
        content = str(m.get("content", ""))
        # 从搜索结果中提取文档来源
        import re
        for match in re.finditer(r'【(.+?)】', content):
            name = match.group(1)
            # 跳过非文档名的标记（如 "知识库搜索结果" 等）
            if any(skip in name for skip in ("搜索结果", "文本搜索", "精确匹配")):
                continue
            if name not in seen:
                seen.add(name)
                sources.append({"doc_source": name})
    return sources
