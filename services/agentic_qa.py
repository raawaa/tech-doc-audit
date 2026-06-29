"""Agentic 知识库问答。

ReAct agent loop：LLM 自主调用 search_kb / search_kb_text 多轮搜索知识库，
根据搜索结果质量动态调整搜索策略，最终给出带来源引用的答案。

对比 qa_service.py（纯 RAG 管道：一次性检索 → LLM 回答），
Agentic 方式允许 LLM 在搜索结果不理想时换关键词重搜，更接近人类查阅资料的行为。

内部通过 services.agentic_audit 的 run_agent_loop + StreamingLLMStep 执行。
"""

import os
from pathlib import Path
from typing import Callable

from core.logger import get_logger
from services.agent_tools import search_kb, search_kb_text
from services.agent_trace import save_trace
from services.agentic_audit import (
    StreamingLLMStep,
    run_agent_loop,
)

_logger = get_logger(__name__)

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

_QA_TOOLS_SPEC = [
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
# Agent Loop（统一 run_agent_loop + StreamingLLMStep）
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

    streaming_step = StreamingLLMStep(tools_spec=_QA_TOOLS_SPEC)

    def _qa_tool_executor(func_name: str, func_args: dict) -> str:
        return _execute_tool(func_name, func_args, kb_ids)

    loop_out = run_agent_loop(
        llm_step=streaming_step,
        initial_messages=messages,
        kb_ids=kb_ids,
        emitter=_emit,
        tool_executor=_qa_tool_executor,
        cancel_checker=None,
        max_turns=MAX_ITERATIONS,
        start_event_msg="",
        max_consecutive_failures=3,
    )

    sources = _extract_sources(loop_out.messages)

    save_trace(
        _TRACE_DIR / f"{qa_id or 'unknown'}_trace.json",
        loop_out.messages,
        metadata={
            "qa_id": qa_id or "unknown",
            "question": question,
            "kb_ids": kb_ids,
            "finished": loop_out.finished,
        },
    )

    return {"answer": loop_out.raw_analysis, "sources": sources}


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
