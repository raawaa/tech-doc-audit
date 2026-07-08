"""Agentic 知识库问答。

ReAct agent loop：LLM 自主调用 search_kb / search_kb_text 多轮搜索知识库，
根据搜索结果质量动态调整搜索策略，最终给出带来源引用的答案。

对比 qa_service.py（纯 RAG 管道：一次性检索 → LLM 回答），
Agentic 方式允许 LLM 在搜索结果不理想时换关键词重搜，更接近人类查阅资料的行为。

内部通过 services.agentic_audit 的 run_agent_loop + StreamingLLMStep 执行。
"""

import os
import re
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
        # QA 路径：异步降级，不阻塞 HTTP 线程（ADR-0002 §决策 3）
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


# search_kb 结果文本的结构化解析模式（格式见 agent_tools.search_kb）
_SRC_BLOCK_START = re.compile(r"^(\d+)\.\s+(.*)$")
_SRC_NAME = re.compile(r"【(.+?)】")
_SRC_RELEVANCE = re.compile(r"相关度:\s*([\d.]+)")
_SRC_DOC_ID = re.compile(r"doc_id:\s*(\S+)")
_SRC_PAGE = re.compile(r"页码:\s*第(\d+)页")
# V8: search_kb 工具输出追加 "block_range: (x, y)" 时解析;非空闭区间
# (0-based block_order) 用于前端 PdfViewer 坐标高亮主路径。
_SRC_BLOCK_RANGE = re.compile(r"block_range:\s*\(?(\d+)\s*,\s*(\d+)\)?")
_SRC_SKIP_MARKERS = ("搜索结果", "文本搜索", "精确匹配")


def _extract_sources(messages: list[dict]) -> list[dict]:
    """从消息历史中提取知识库来源引用。

    解析 search_kb 工具返回的结构化文本（格式见 agent_tools.search_kb），
    回填完整字段：doc_source / doc_id / page_number(0-based) / content_snippet / relevance。
    而非旧实现那样仅保留文档名。

    search_kb_text 的结果无结构化 doc_id/page_number，仅按文档名尽力提取（向后兼容）。
    """

    def _is_content_line(line: str) -> bool:
        stripped = line.strip()
        if not stripped:
            return False
        return "⚠️" not in line and "来源单一性警告" not in line

    def _flush(buf: dict | None, seen: set[str], out: list[dict]):
        if buf and buf.get("doc_source") and buf["doc_source"] not in seen:
            seen.add(buf["doc_source"])
            snippet = (buf.get("content_snippet") or "")[:300]
            buf["content_snippet"] = snippet
            out.append(buf)

    sources: list[dict] = []
    seen: set[str] = set()

    for m in messages:
        if m.get("role") != "tool":
            continue
        content = str(m.get("content", ""))

        # search_kb_text 结果：无结构化 doc_id/page_number，仅按文档名尽力提取
        if "文本搜索结果" in content or "精确匹配" in content:
            for match in _SRC_NAME.finditer(content):
                name = match.group(1)
                if any(skip in name for skip in _SRC_SKIP_MARKERS):
                    continue
                if name not in seen:
                    seen.add(name)
                    sources.append({
                        "doc_source": name, "doc_id": "",
                        "page_number": None, "content_snippet": "", "relevance": 0.0,
                        # V8: search_kb_text 关键词搜索无 layout 概念,block_range 永远 None,
                        # 保持字段一致性,前端按缺失走 highlight fallback。
                        "block_range": None,
                    })
            continue

        # search_kb 结果：逐块解析结构化字段
        current: dict | None = None
        for line in content.split("\n"):
            bs = _SRC_BLOCK_START.match(line)
            if bs:
                _flush(current, seen, sources)
                label = bs.group(2)
                name_m = _SRC_NAME.search(label)
                doc_source = name_m.group(1) if name_m else (label.strip() or "未知来源")
                current = {
                    "doc_source": doc_source, "doc_id": "",
                    "page_number": None, "content_snippet": "", "relevance": 0.0,
                    # V8: 块坐标区间;由 search_kb 输出 "block_range: (x, y)" 解析,
                    # 缺失时为 None(非 PDF / 旧 KB / 匹配失败),前端 fallback。
                    "block_range": None,
                }
                continue
            if current is None:
                continue
            if "相关度" in line:
                rel_m = _SRC_RELEVANCE.search(line)
                if rel_m:
                    current["relevance"] = float(rel_m.group(1))
                did_m = _SRC_DOC_ID.search(line)
                if did_m:
                    current["doc_id"] = did_m.group(1)
                pg_m = _SRC_PAGE.search(line)
                if pg_m:
                    current["page_number"] = int(pg_m.group(1)) - 1  # 1-based 文本 → 0-based
                # V8: 块坐标区间。search_kb 工具输出 (x, y) 元组形式;
                # None(未匹配)走前端 highlight 字符串匹配 fallback。
                br_m = _SRC_BLOCK_RANGE.search(line)
                if br_m:
                    current["block_range"] = [int(br_m.group(1)), int(br_m.group(2))]
            elif _is_content_line(line):
                current["content_snippet"] = (current["content_snippet"] + "\n" + line.strip()).strip() if current["content_snippet"] else line.strip()
        _flush(current, seen, sources)

    return sources
