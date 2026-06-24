"""Agentic 审核管线。

Agent 自主调用工具逐章审核文档：
1. read_chapter   — 读取指定章节
2. search_kb      — 语义向量搜索知识库标准
3. search_kb_text — 精确关键词文本搜索
4. flag_issue     — 记录审核问题
5. finish         — 审核完毕（仅 structured_llm 路径）

对比 topic_audit（固定关键词 + 单次 LLM）：
- Agent 自主决定搜什么、搜多深
- 逐章推进，有上下文记忆
- 可迭代深挖可疑条款
"""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from llama_index.core.llms import ChatMessage, MessageRole

from core.logger import get_logger
from core.settings import get_llm
from core.degradation import record as _deg_record
from models.audit_document import DocumentStructure
from models.audit_task import (
    AuditIssue, AuditResult, IssueLocation,
    ResultSummary, StandardRef,
)
from models.llm_schemas import AgentAction

_logger = get_logger(__name__)

# trace 文件存放目录，默认 data/audits/{doc_id}/tasks/traces/
_TRACE_DIR = Path(
    os.environ.get("AUDIT_DATA_DIR", "data")
) / "audits"

MAX_TURNS = 30
CHAPTER_MAX_CHARS = 4000
MAX_CONSECUTIVE_FAILURES = 3

# per-task 共享事件日志：audit 线程写入，SSE 连接读取
# key=task_id, value=list[dict]
_task_event_logs: dict[str, list[dict]] = {}
import threading as _threading
_task_log_lock = _threading.Lock()


def get_task_events_since(task_id: str, index: int = 0) -> tuple[list[dict], int]:
    """获取 task_id 的事件日志中 index 之后的新事件。
    
    Returns:
        (new_events, next_index) — new_events 是 index 之后的新事件列表，
        next_index 是下次调用时应传入的 index。
    """
    with _task_log_lock:
        log = _task_event_logs.get(task_id, [])
        if index >= len(log):
            return [], index
        return log[index:], len(log)


def clear_task_events(task_id: str):
    """清理任务事件日志。"""
    with _task_log_lock:
        _task_event_logs.pop(task_id, None)


# ═══════════════════════════════════════════════════════════════════════════════
# System Prompt
# ═══════════════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """你是一个严格的技术文档审核专家。你的任务是对照知识库中的标准规范，审核文档是否合规。
你无法访问互联网或任何外部信息源，只能通过以下 action 操作知识库中的标准文档。

## 审核流程

1. 仔细阅读下方的文档全文（或文档开头部分）
2. 从文档内容中提炼具体的技术关键词，调用 search_kb 或 search_kb_text 搜索相关标准规范
3. 逐条比对文档内容与搜索到的标准条款
4. 发现问题立即调用 flag_issue 记录
5. 对文档的不同主题/技术点，使用不同的关键词多角度搜索
6. 如果文档很长，当前未显示完整内容，可调用 read_chapter 查看更多

## 搜索策略

search_kb（语义向量搜索）vs search_kb_text（精确文本搜索）的选择规则：
- search_kb：适合搜索概念性要求（如"质保期要求"、"验收标准"、"防水等级"），能匹配同义词。速度较慢，占GPU。
- search_kb_text：适合搜索具体术语/编号（如"IP65"、"GB/T 12345"、"镀锌钢管"），精确命中。速度快，不占GPU，但无法匹配同义词。
- 遇到标准编号/参数值/专有名词时优先用 search_kb_text
- 遇到概念描述时优先用 search_kb
- 从文档内容中提取具体技术术语作为 search_query
- 使用不同角度和关键词多次搜索
- 搜索结果 relevance < 0.3 可视为不相关，换词重搜

## 判断标准
- compliance: 文档内容违反标准规定（数值不达标、方法错误等）
- completeness: 文档缺少标准要求的内容（缺失必要条款、参数未明确等）
- consistency: 文档内部数据矛盾，或与标准条文不一致
- insufficient_evidence: 证据不足，无法做出确定判断
- 无问题的内容不需要强行找问题

## flag_issue 要求
- cited_excerpt 必须从文档原文逐字引用作为证据
- standard_name 和 standard_clause 必须来自 search_kb 的返回结果
- document_position 必须使用文档中的实际章节名称，不要使用编号
- description 清晰说明问题和标准依据
- 建议同时提供 suggestion（修改建议）

## 注意事项
- 每次只执行一个操作（一个 action）
- thought 中简要说明当前推理：在审哪个章节、看到了什么、为什么选择这个操作
- 对于 compliance 类型问题，不要在没有搜索到相关标准时就调用 flag_issue
- consistency/completeness 类问题可在文档内部直接发现，不强制要求外部标准
- 直接输出 JSON 格式，不要用 Markdown 包裹
- 调用 finish 时在 final_summary 中总结发现的所有问题数量和主要类型"""


# ═══════════════════════════════════════════════════════════════════════════════
# 章节文本提取
# ═══════════════════════════════════════════════════════════════════════════════

def _find_chapter_text(
    parsed_content: str,
    structure: DocumentStructure,
    chapter_index: int,
) -> str:
    """从 parsed_content 中提取指定章节的全文。

    策略：
    1. 如果 Chapter.text 已填充且内容充足，直接使用
    2. 按章节标签（含编号+标题）在 parsed_content 中定位区间
    3. 无标题时按顺序估算
    """
    chapter = structure.chapters[chapter_index]
    total = len(structure.chapters)

    # 策略 1：Chapter.text 已填充
    if chapter.text and len(chapter.text) > 100:
        return chapter.text

    # 策略 2：按章节标签定位
    label = _chapter_label(chapter, chapter_index)
    start = _locate_label(parsed_content, label)
    if start < 0 and chapter.title and chapter.title != label:
        start = _locate_label(parsed_content, chapter.title)
    if start < 0:
        start = 0  # 找不到标签时从头开始

    # 查找下一个章节的起始位置
    if chapter_index + 1 < total:
        next_chapter = structure.chapters[chapter_index + 1]
        next_label = _chapter_label(next_chapter, chapter_index + 1)
        end = _locate_label(parsed_content, next_label, after=start)
        if end < 0 and next_chapter.title and next_chapter.title != next_label:
            end = _locate_label(parsed_content, next_chapter.title, after=start)
        if end > start:
            return parsed_content[start:end].strip()
        # 找不到下一章 → 估算
        return parsed_content[start:start + 2000].strip()
    else:
        # 最后一章：到文档末尾
        return parsed_content[start:].strip()


def _chapter_label(chapter, index: int) -> str:
    """构造章节标签，如 '第二章 技术规格'。"""
    title = chapter.title or ""
    number = chapter.number or ""
    if number:
        return f"第{number}章 {title}".strip()
    if title:
        return title
    return f"第{index + 1}章"


def _locate_label(content: str, label: str, after: int = 0) -> int:
    """在 content 中定位 label，返回匹配起始位置。找不到返回 -1。"""
    if not label:
        return -1
    escaped = re.escape(label)
    # 按优先级尝试多种格式
    patterns = [
        rf"#+\s+{escaped}",          # "## 第二章 技术规格"
        rf"^{escaped}\s*$",          # 独立行 "第二章 技术规格"
        rf"^{escaped}",              # 行首 "第二章 技术规格..."
        escaped,                     # 任意位置
    ]
    for pat in patterns:
        m = re.search(pat, content[after:], re.MULTILINE)
        if m:
            return after + m.start()
    return -1


# ═══════════════════════════════════════════════════════════════════════════════
# 工具实现
# ═══════════════════════════════════════════════════════════════════════════════

def _tool_get_structure(structure: DocumentStructure | None, doc_name: str) -> str:
    """格式化文档结构。"""
    if not structure or not structure.chapters:
        return f"文档《{doc_name}》无结构信息（整篇为单一文本）。"

    lines = [
        f"文档《{doc_name}》共 {len(structure.chapters)} 章，"
        f"{structure.total_clauses} 个条款：",
    ]
    for i, ch in enumerate(structure.chapters, 1):
        label = ch.number or f"第{i}章"
        title = ch.title or ""
        header = f"  {label} {title}".strip()
        if ch.clauses:
            clause_nums = [c.number for c in ch.clauses[:10]]
            more = "..." if len(ch.clauses) > 10 else ""
            header += f"（{len(ch.clauses)} 个条款: {', '.join(clause_nums)}{more}）"
        lines.append(header)
    return "\n".join(lines)


def _tool_read_chapter(
    parsed_content: str,
    structure: DocumentStructure | None,
    chapter_index: int,
) -> str:
    """读取指定章节全文。"""
    if not structure or not structure.chapters:
        return _format_chapter_text(parsed_content[:CHAPTER_MAX_CHARS], 0, "全文")

    if chapter_index < 1 or chapter_index > len(structure.chapters):
        return f"章节序号 {chapter_index} 无效，文档共 {len(structure.chapters)} 章"

    ch = structure.chapters[chapter_index - 1]
    # 标签策略：优先用文档自带的章节名（title），避免编造"第N章"
    if ch.title:
        label = ch.title
    elif ch.number:
        label = ch.number
    else:
        label = f"第{chapter_index}章"

    text = _find_chapter_text(parsed_content, structure, chapter_index - 1)
    return _format_chapter_text(text, chapter_index, label)


def _format_chapter_text(text: str, index: int, label: str) -> str:
    """格式化章节文本（截断提示）。"""
    if not text:
        return f"=== {label} ===\n（该章节无内容）"

    header = f"=== {label} ==="
    if len(text) <= CHAPTER_MAX_CHARS:
        return f"{header}\n{text}"

    truncated = text[:CHAPTER_MAX_CHARS]
    remaining = len(text) - CHAPTER_MAX_CHARS
    return (
        f"{header}\n{truncated}\n\n"
        f"…（本段共 {len(text)} 字符，已显示前 {CHAPTER_MAX_CHARS} 字符，"
        f"剩余约 {remaining} 字符。\n"
        f"提示：建议先对已显示内容中的技术关键词调用 search_kb/search_kb_text "
        f"查找标准进行审核，而非逐字通读全文。"
        f"如确需继续阅读本章后续内容，请再次调用 read_chapter({index})）"
    )


def _tool_search_kb(kb_ids: list[str], query: str, top_k: int = 5) -> str:
    """搜索知识库，返回格式化的标准条款。"""
    if not query or not kb_ids:
        return "（未提供搜索关键词或知识库）"

    from services.vector_search import vec_search

    try:
        results = vec_search(kb_ids, query, top_k=top_k)
    except Exception as e:
        _logger.warning("search_kb failed for query '%s': %s", query, e)
        error_msg = str(e)
        return (
            f"（语义搜索失败: {error_msg}。\n"
            f"建议：1) 尝试用更简短的关键词（如去掉修饰词）；"
            f"2) 如果是精确术语或标准编号，改用 search_kb_text；"
            f"3) 如果持续失败，跳过当前搜索点继续审核其他内容）"
        )

    if not results:
        return f"（未找到与「{query}」相关的标准）"

    lines = [f"【知识库搜索结果（搜索词: {query}，共 {len(results)} 条）】"]
    for i, r in enumerate(results, 1):
        relevance = r.get("relevance", 0)
        doc = r.get("doc_source", "") or r.get("doc_id", "")
        clause = r.get("clause_number", "")
        section = r.get("section_path", "")
        content = (r.get("content", "") or "")[:500]

        label_parts = []
        if doc:
            label_parts.append(f"【{doc}】")
        if clause:
            label_parts.append(f"第{clause}条")
        if section and not clause:
            label_parts.append(section)
        label = " ".join(label_parts) if label_parts else "未知来源"

        lines.append(f"\n{i}. {label} (相关度: {relevance:.2f})\n   {content}")
    return "\n".join(lines)


def _tool_search_kb_text(kb_ids: list[str], query: str) -> str:
    """纯文本关键词搜索知识库（rga/rg），精确匹配。"""
    if not query or not kb_ids:
        return "（未提供搜索关键词或知识库）"

    from services.vector_search import _get_kb_search_paths, _run_rga

    paths = _get_kb_search_paths(kb_ids)
    if not paths:
        return "（知识库无可用文档路径）"

    try:
        result = _run_rga(query, paths)
    except Exception as e:
        _logger.warning("search_kb_text failed for query '%s': %s", query, e)
        error_msg = str(e)
        return (
            f"（文本搜索失败: {error_msg}。\n"
            f"建议：1) 简化搜索词为更短的关键词；"
            f"2) 如果是概念性要求，改用 search_kb 语义搜索；"
            f"3) 如果持续失败，跳过当前搜索继续审核其他内容）"
        )

    if not result:
        return f"（未找到与「{query}」匹配的文本）"

    # 截断
    if len(result) > 2000:
        result = result[:2000] + "\n... [截断]"
    return f"【知识库文本搜索结果（精确匹配: {query}）】\n{result}"


def _tool_flag_issue(action: AgentAction, issues: list[AuditIssue]) -> str:
    """记录审核问题。"""
    warnings = []
    if not action.cited_excerpt:
        warnings.append("缺少 cited_excerpt（原文引用），建议补充以增强证据力度")
    if not action.standard_name:
        warnings.append("缺少 standard_name（标准名称），建议补充以标明依据来源")
    if not action.issue_description:
        warnings.append("缺少 description（问题描述），这是必填项")

    issue = AuditIssue(
        id=len(issues) + 1,
        type=action.issue_type or "compliance",
        severity=action.issue_severity or "medium",
        description=action.issue_description or "",
        standard_reference=StandardRef(
            standard_name=action.standard_name or "",
            standard_id=action.standard_name or "",
            clause=action.standard_clause,
            requirement=action.standard_requirement,
        ),
        cited_excerpt=action.cited_excerpt or "",
        document_position=action.document_position or "",
        suggestion=action.issue_suggestion,
        location=IssueLocation(
            clause_number=action.standard_clause,
            original_text=(action.cited_excerpt or "")[:200],
        ),
    )
    issues.append(issue)

    result = f"问题 #{len(issues)} 已记录。"
    if warnings:
        result += "\n⚠️ 提示：" + "；".join(warnings)
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# 工具分发
# ═══════════════════════════════════════════════════════════════════════════════

def _execute_tool(
    action: AgentAction,
    parsed_content: str,
    structure: DocumentStructure | None,
    kb_ids: list[str],
    doc_name: str,
    issues: list[AuditIssue],
) -> str:
    """根据 action 分发到对应工具函数。"""
    tool_name = action.action

    if tool_name == "read_chapter":
        idx = action.chapter_index or 1
        return _tool_read_chapter(parsed_content, structure, idx)

    elif tool_name == "search_kb":
        query = action.search_query or ""
        top_k = action.search_top_k or 5
        return _tool_search_kb(kb_ids, query, top_k)

    elif tool_name == "search_kb_text":
        query = action.search_query or ""
        return _tool_search_kb_text(kb_ids, query)

    elif tool_name == "flag_issue":
        return _tool_flag_issue(action, issues)

    return f"未知操作: {tool_name}"


# ═══════════════════════════════════════════════════════════════════════════════
# 降级解析（structured_llm 失败时）
# ═══════════════════════════════════════════════════════════════════════════════

def _parse_action_fallback(text: str) -> AgentAction | None:
    """当 as_structured_llm 失败时，从纯文本中解析 AgentAction JSON。"""
    # 去除 Markdown 代码块
    text = re.sub(r'```(?:json)?\s*', '', text, flags=re.IGNORECASE).strip()
    text = text.rstrip('`').strip()

    # 尝试找到 JSON 对象
    match = re.search(r'\{[^{}]*"action"\s*:\s*"[^"]+"[^{}]*\}', text, re.DOTALL)
    if not match:
        return None

    try:
        data = json.loads(match.group(0))
        return AgentAction.model_validate(data)
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# 消息构建
# ═══════════════════════════════════════════════════════════════════════════════

def _build_system_msg() -> ChatMessage:
    return ChatMessage(role=MessageRole.SYSTEM, content=SYSTEM_PROMPT)


def _build_init_msg(
    doc_name: str,
    structure: DocumentStructure | None,
    parsed_content: str = "",
) -> ChatMessage:
    DOC_FULL_THRESHOLD = 30000
    structure_text = _tool_get_structure(structure, doc_name)

    if len(parsed_content) <= DOC_FULL_THRESHOLD:
        content = (
            f"请审核文档《{doc_name}》。\n\n"
            f"文档结构：\n{structure_text}\n\n"
            f"=== 文档全文 ===\n{parsed_content}"
        )
    else:
        content = (
            f"请审核文档《{doc_name}》。\n\n"
            f"文档结构：\n{structure_text}\n\n"
            f"=== 文档开头（共{len(parsed_content)}字）===\n"
            f"{parsed_content[:8000]}\n"
            f"\n（文档较长，如需查看更多内容请使用 read_chapter 工具）"
        )
    return ChatMessage(role=MessageRole.USER, content=content)


def _build_tool_result_msg(result: str) -> ChatMessage:
    return ChatMessage(role=MessageRole.USER, content=f"[工具结果]\n{result}")


# ═══════════════════════════════════════════════════════════════════════════════
# 结果构建
# ═══════════════════════════════════════════════════════════════════════════════

def _build_result(
    task_id: str,
    doc_id: str,
    doc_name: str,
    issues: list[AuditIssue],
    raw_analysis: str,
) -> AuditResult:
    return AuditResult(
        task_id=task_id,
        document_id=doc_id,
        document_name=doc_name,
        summary=ResultSummary(
            total_clauses=len(issues),
            issues_count=len(issues),
            compliance_issues=sum(1 for i in issues if i.type == "compliance"),
            completeness_issues=sum(1 for i in issues if i.type == "completeness"),
            consistency_issues=sum(1 for i in issues if i.type == "consistency"),
            high_severity=sum(1 for i in issues if i.severity == "high"),
            medium_severity=sum(1 for i in issues if i.severity == "medium"),
            low_severity=sum(1 for i in issues if i.severity == "low"),
        ),
        issues=issues,
        raw_analysis=raw_analysis,
    )


def _save_trace(
    task_id: str,
    doc_id: str,
    doc_name: str,
    issues_count: int,
    total_iterations: int,
    messages: list[dict],
    *,
    provider: str = "deepseek",
    model: str = "",
    finished: bool = True,
) -> Path | None:
    """持久化 agentic 审核的完整对话跟踪记录。

    保存到 data/audits/{doc_id}/tasks/{task_id}_trace.json，包含完整的
    消息历史（系统提示、用户消息、每轮 tool_calls 及其结果、reasoning），
    便于事后诊断 agent 行为、验证工具描述效果、分析 LLM 决策质量。
    """
    try:
        trace_dir = _TRACE_DIR / doc_id / "tasks" / "traces"
        trace_dir.mkdir(parents=True, exist_ok=True)
        trace_path = trace_dir / f"{task_id}_trace.json"

        # 序列化消息：去重过大的内容避免文件膨胀
        serializable_messages = []
        for m in messages:
            sm = dict(m)
            # content 可能为 None（assistant 只有 tool_calls 时）
            if sm.get("content") and len(str(sm["content"])) > 10000:
                sm["content"] = str(sm["content"])[:10000] + (
                    f"\n…[content truncated from "
                    f"{len(str(m['content']))} chars]"
                )
            # tool_calls 中的 arguments 也可能很大
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
            "task_id": task_id,
            "doc_id": doc_id,
            "doc_name": doc_name,
            "provider": provider,
            "model": model or os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "finished": finished,
            "total_iterations": total_iterations,
            "issues_count": issues_count,
            "messages": serializable_messages,
        }

        with open(trace_path, "w", encoding="utf-8") as f:
            json.dump(trace, f, ensure_ascii=False, indent=2)

        _logger.info("agentic trace saved: %s (%d messages, %.1f KB)",
                      trace_path, len(messages), trace_path.stat().st_size / 1024)
        return trace_path
    except Exception as e:
        _logger.warning("failed to save agentic trace: %s", e)
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# 原生 Function Calling 路径（DeepSeek thinking 模式）
# ═══════════════════════════════════════════════════════════════════════════════

_TOOLS_SPEC = [
    {
        "type": "function",
        "function": {
            "name": "read_chapter",
            "description": (
                "读取文档指定章节的全文内容。当系统消息中显示的文档片段不足以审核目标章节时使用此工具。"
                "返回的文本前会标注章节名称标签（=== 章节名 ===），内容最长显示4000字符。"
                "若内容被截断，返回末尾会显示已读/剩余字符数；此时建议先用更精准的搜索词调用 search_kb "
                "或 search_kb_text 获取对应标准进行审核，而非逐字通读全文。"
                "不要使用本工具的情形：(1)系统消息中已包含该章节的足够内容；"
                "(2)尚未对照 search_kb 结果审核当前可见内容就急于读更多章节；"
                "(3)已知目标技术关键词时，应优先用 search_kb/search_kb_text 查找标准而非漫无目的地阅读。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "chapter_index": {
                        "type": "integer",
                        "description": (
                            "章节序号，从1开始，对应文档结构列表中各章节的编号。"
                            "例如，要读第3章则传3。如不确定序号，先查看系统消息中的文档结构列表。"
                        ),
                    },
                },
                "required": ["chapter_index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_kb",
            "description": (
                "在知识库中进行语义向量搜索，查找与查询概念语义相近的标准规范条款。"
                "适合搜索概念性、描述性的要求（如「质保期要求」、「验收标准」、「防水等级」），"
                "能够匹配同义词和近义表达，但无法匹配精确的编号或代码。"
                "返回结果按相关度降序排列，每条包含：【标准文档名称】、条款编号、相关度分数（0~1）、"
                "以及该条款前500字符的内容。"
                "与 search_kb_text 的区别：本工具使用语义向量匹配，能理解概念但返回较慢且不保证精确编号命中；"
                "search_kb_text 使用 rga/rg 精确文本匹配，速度快、不占GPU，适合搜索标准编号（如GB/T 12345）"
                "及专有名词（如IP65）。"
                "不要使用本工具的情形：(1)搜索词是精确的标准编号/参数值/专有名词时，请改用 search_kb_text；"
                "(2)已用同一关键词搜索过且相关度均低于0.3，应换词重搜而非重复相同查询；"
                "(3)未从文档中提炼到具体技术关键词时。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "搜索关键词或概念描述，从文档当前章节中提取。"
                            "示例：'质保期'、'防雷接地要求'、'验收标准'。"
                            "不要输入完整句子，用2-5个词的关键词短语。"
                        ),
                    },
                    "top_k": {
                        "type": "integer",
                        "description": (
                            "返回结果数量，默认5。"
                            "若前次搜索结果相关度过低（<0.3），可提升至8-10以扩大搜索范围。"
                        ),
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
                "适合搜索具体的标准编号（如GB/T 12345）、参数值（如3000m²、IP65、≥100dB）、"
                "专有名词（如'镀锌钢管'、'环氧树脂'）等需要精确命中的术语。"
                "速度快、不占用GPU，但无法匹配同义词或语义相近的表达——若搜索概念性要求"
                "（如'防水要求'需要匹配'防渗'、'不透水'等），请改用 search_kb。"
                "返回结果最多2000字符，格式为 rga/rg 的原始匹配行（含文件名、行号、上下文），"
                "按文件分组显示匹配片段。"
                "不要使用本工具的情形：(1)需要搜索概念性或描述性要求时，请用 search_kb；"
                "(2)搜索词过于宽泛（如单个字'水'），会产生大量噪声结果；"
                "(3)已用相同关键词搜索过且结果为空，应换用近义术语重搜。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "精确搜索关键词。示例：'GB/T 12345'、'IP65'、'3000m²'、'镀锌钢管'。"
                            "输入具体的标准编号、参数值或专有术语，而非自然语言描述。"
                        ),
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "flag_issue",
            "description": (
                "记录一个审核发现的问题。必须参数：issue_type（问题类型）、severity（严重程度）、"
                "description（问题描述，应清晰说明文档何处不符合哪条标准）、"
                "cited_excerpt（从文档原文逐字引用的证据）。"
                "对于 compliance 类型（违反标准规定），调用前必须已用 search_kb 或 search_kb_text "
                "获取了相关标准依据，standard_name 和 standard_clause 必须来自搜索结果。"
                "对于 consistency（内部矛盾）、completeness（缺失必要内容）类型，"
                "可在文档内容中直接发现，不强制要求外部标准引用。"
                "强烈建议同时提供：document_position（引用所在的章节名称）、suggestion（修改建议）。"
                "返回格式为'问题 #N 已记录'，其中N为累计问题编号。可多次调用以记录多个问题。"
                "不要使用本工具的情形：(1)compliance 类型尚未搜索到相关标准依据时——请先调用 search_kb；"
                "(2)问题描述模糊、无法指出具体条款时；(3)文档内容实际上合规，不要强行找问题。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "issue_type": {
                        "type": "string",
                        "enum": ["compliance", "completeness", "consistency",
                                 "insufficient_evidence"],
                        "description": (
                            "问题类型：compliance=违反标准规定（如数值不达标、方法错误）；"
                            "completeness=缺少标准要求的必要内容（如缺失质保期条款）；"
                            "consistency=文档内部数据矛盾或与标准条文不一致；"
                            "insufficient_evidence=证据不足以确定判断（如信息不完整无法判定合规性）"
                        ),
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                        "description": (
                            "严重程度：high=可能导致项目失败或重大法律风险；"
                            "medium=影响质量或增加成本风险；"
                            "low=格式或表述瑕疵，不影响实质合规"
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": (
                            "问题描述，清晰说明：文档中何处（章节/段落）存在什么问题，"
                            "违反了哪条标准的哪项要求。"
                            "示例：'第三章技术规格中IP防护等级仅标注IP54，"
                            "而GB 4208-2008第5.1条要求室外设备不低于IP65。'"
                        ),
                    },
                    "standard_name": {
                        "type": "string",
                        "description": (
                            "标准文档名称，必须来自 search_kb 或 search_kb_text 返回结果中的"
                            "【文档来源】字段。示例：'CJJ101-2016'、'GB/T 31462-2015'。"
                            "不可自行编造标准编号。"
                        ),
                    },
                    "standard_clause": {
                        "type": "string",
                        "description": (
                            "标准条款编号，必须来自搜索结果中的'第X条'字段。示例：'3.2.1'、'5.4.2'。"
                        ),
                    },
                    "standard_requirement": {
                        "type": "string",
                        "description": "标准条款的原文要求（从搜索结果中摘录）",
                    },
                    "cited_excerpt": {
                        "type": "string",
                        "description": (
                            "从待审核文档中逐字引用的原文证据（必须原样复制，不可概括或改写）。"
                            "示例：'设备防护等级不低于IP54'。"
                            "这是证明问题存在的核心证据，请务必提供。"
                        ),
                    },
                    "document_position": {
                        "type": "string",
                        "description": (
                            "文档中引用原文所在的章节名称（使用文档实际的章节标题，不要用编号代替）。"
                            "示例：'第三章 技术规格与参数要求'。"
                        ),
                    },
                    "suggestion": {
                        "type": "string",
                        "description": (
                            "具体的修改建议。"
                            "示例：'将防护等级从IP54修改为不低于IP65，以满足GB 4208-2008室外设备要求。'"
                        ),
                    },
                },
                "required": ["issue_type", "severity", "description", "cited_excerpt"],
            },
        },
    },
]

NATIVE_SYSTEM_PROMPT = """你是一个严格的技术文档审核专家。你的任务是对照知识库中的标准规范，审核文档是否合规。
你无法访问互联网或任何外部信息源，只能通过提供的工具搜索知识库中的标准文档。

## 审核流程

1. 仔细阅读下方的文档全文（或文档开头部分）
2. 从文档内容中提炼具体的技术关键词，调用搜索工具查找相关标准规范
3. 逐条比对文档内容与搜索到的标准条款
4. 发现问题立即调用 flag_issue 记录
5. 对文档的不同章节/主题，使用不同的关键词多角度搜索
6. 如果文档很长，当前未显示的部分需要查看更多时，调用 read_chapter 读取

## 搜索策略
- 使用不同角度和关键词多次搜索，覆盖文档涉及的各个技术领域
- 搜索结果 relevance < 0.3 可视为不相关，换词重搜
- 各搜索工具的具体使用场景与边界详见各工具的 description，调用前请参考

## 判断标准
- compliance: 文档内容违反标准规定（数值不达标、方法错误等）
- completeness: 文档缺少标准要求的内容（缺失必要条款、参数未明确等）
- consistency: 文档内部数据矛盾，或与标准条文不一致
- insufficient_evidence: 证据不足，无法做出确定判断
- 无问题的内容不需要强行找问题

## flag_issue 要求
- cited_excerpt 必须从文档原文逐字引用作为证据
- standard_name 和 standard_clause 必须来自搜索工具的返回结果
- document_position 必须使用文档中的实际章节名称，不要使用编号
- description 清晰说明问题和标准依据

## 注意事项
- compliance 类型问题必须先搜索到相关标准才能调用 flag_issue
- consistency/completeness 类问题可在文档内部直接发现，不强制外部标准
- 不要在文档内容合规时强行找问题
- 工具调用失败时，根据返回的错误提示调整参数重试，不要放弃"""


def _execute_native_tool(
    func_name: str,
    args: dict,
    parsed_content: str,
    structure: DocumentStructure | None,
    kb_ids: list[str],
    doc_name: str,
    issues: list[AuditIssue],
) -> str:
    """原生 function calling 的工具分发。"""
    if func_name == "read_chapter":
        return _tool_read_chapter(
            parsed_content, structure,
            args.get("chapter_index", 1),
        )
    elif func_name == "search_kb":
        return _tool_search_kb(
            kb_ids,
            args.get("query", ""),
            args.get("top_k", 5),
        )
    elif func_name == "search_kb_text":
        return _tool_search_kb_text(kb_ids, args.get("query", ""))
    elif func_name == "flag_issue":
        action = AgentAction(
            thought="",
            action="flag_issue",
            issue_type=args.get("issue_type"),
            issue_severity=args.get("severity"),
            issue_description=args.get("description"),
            standard_name=args.get("standard_name"),
            standard_clause=args.get("standard_clause"),
            standard_requirement=args.get("standard_requirement"),
            cited_excerpt=args.get("cited_excerpt"),
            document_position=args.get("document_position"),
            issue_suggestion=args.get("suggestion"),
        )
        return _tool_flag_issue(action, issues)
    return (
        f"未知工具: {func_name}。"
        f"可用的工具有：read_chapter、search_kb、search_kb_text、flag_issue。"
        f"请从上述工具中选择正确的工具重新调用。"
    )


def _run_native_tool_calling(
    parsed_content: str,
    structure: DocumentStructure | None,
    kb_ids: list[str],
    doc_name: str,
    task_id: str,
    doc_id: str,
    event_callback: Callable[[dict], None] | None = None,
) -> AuditResult:
    """使用 DeepSeek 原生 function calling + thinking 模式执行审核。

    相比 structured_llm 路径的优势：
    - 原生工具调用，LLM 输出更稳定
    - thinking 模式启用，审核判断更准确
    - 支持一次请求内连续调用多个工具
    """
    from openai import OpenAI
    import httpx

    def _emit(event: dict):
        # 存入共享日志
        with _task_log_lock:
            if task_id not in _task_event_logs:
                _task_event_logs[task_id] = []
            _task_event_logs[task_id].append(event)
        # 同时推送给当前 SSE 连接的回调
        if event_callback:
            try:
                event_callback(event)
            except Exception:
                pass

    _emit({"type": "start", "message": "Agentic 审核开始 (DeepSeek thinking 模式)"})

    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

    # 绕过 SOCKS 代理（与 settings.py 中 DeepSeek provider 行为一致）
    http_client = httpx.Client(trust_env=False, timeout=httpx.Timeout(300))
    client = OpenAI(api_key=api_key, base_url=base_url, http_client=http_client)

    issues: list[AuditIssue] = []
    issue_count_before = 0
    # 按文档长度构建初始消息
    DOC_FULL_THRESHOLD = 30000  # 短文档阈值（字符数）
    structure_text = _tool_get_structure(structure, doc_name)
    if len(parsed_content) <= DOC_FULL_THRESHOLD:
        user_content = (
            f"请审核文档《{doc_name}》。\n\n"
            f"文档结构：\n{structure_text}\n\n"
            f"=== 文档全文 ===\n{parsed_content}"
        )
    else:
        user_content = (
            f"请审核文档《{doc_name}》。\n\n"
            f"文档结构：\n{structure_text}\n\n"
            f"=== 文档开头（共{len(parsed_content)}字）===\n"
            f"{parsed_content[:8000]}\n"
            f"\n（文档较长，如需查看更多内容请使用 read_chapter 工具）"
        )

    messages: list[dict] = [
        {"role": "system", "content": NATIVE_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    raw_analysis = ""
    max_iterations = 100
    finished = False  # True = agent 自行结束；False = 异常/超限

    for iteration in range(max_iterations):
        # 检查任务是否被取消
        try:
            from storage.audit_task_repo import get_task
            current_task = get_task(task_id)
            if current_task and current_task.status == "cancelled":
                _emit({"type": "cancelled", "message": "审核任务已被取消"})
                _logger.info("agentic audit task %s cancelled at iteration %d", task_id, iteration)
                raw_analysis = f"审核已取消（第 {iteration} 轮），已记录 {len(issues)} 个问题。"
                break
        except Exception:
            pass  # 取消检查失败不阻塞审核

        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=_TOOLS_SPEC,
                extra_body={"thinking": {"type": "enabled"}},
            )
        except Exception as e:
            _emit({"type": "error", "message": f"LLM 调用失败: {e}"})
            _logger.warning("native tool calling: chat.completions failed: %s", e)
            raw_analysis = f"LLM 调用失败第 {iteration} 轮: {e}"
            break

        msg = response.choices[0].message

        # 发送 reasoning 事件
        if msg.reasoning_content:
            rc = msg.reasoning_content
            _emit({"type": "reasoning", "content": rc[:2000]})

        # 追加 assistant 消息
        assistant_msg: dict = {"role": "assistant", "content": msg.content or ""}
        if msg.reasoning_content:
            assistant_msg["reasoning_content"] = msg.reasoning_content
        if msg.tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": tc.type,
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ]
        messages.append(assistant_msg)

        # 没有工具调用 → 模型给出了最终回答
        if not msg.tool_calls:
            raw_analysis = msg.content or "审核完成"
            finished = True
            _emit({"type": "complete", "summary": raw_analysis, "issues_count": len(issues)})
            _logger.info(
                "native agentic audit finished, %d issues found",
                len(issues),
            )
            break

        # 执行工具
        for tc in msg.tool_calls:
            func_name = tc.function.name
            try:
                func_args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                func_args = {}

            _emit({"type": "tool_call", "tool": func_name, "args": func_args})
            _logger.debug("native tool call: %s(%s)", func_name, func_args)

            try:
                tool_result = _execute_native_tool(
                    func_name, func_args,
                    parsed_content, structure, kb_ids, doc_name, issues,
                )
            except Exception as e:
                tool_result = f"工具执行失败: {e}"
                _emit({"type": "error", "message": f"{func_name} 执行失败: {e}"})

            _emit({"type": "tool_result", "tool": func_name, "content": tool_result})

            # 检测 flag_issue 产生的新问题
            if func_name == "flag_issue" and len(issues) > issue_count_before:
                new_issue = issues[-1]
                _emit({
                    "type": "issue_found",
                    "issue": {
                        "id": new_issue.id,
                        "type": new_issue.type,
                        "severity": new_issue.severity,
                        "description": new_issue.description[:300],
                        "standard_name": new_issue.standard_reference.standard_name if new_issue.standard_reference else None,
                        "standard_clause": new_issue.standard_reference.clause if new_issue.standard_reference else None,
                    },
                })
                issue_count_before = len(issues)

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": tool_result,
            })
    else:
        _deg_record("agentic_audit", "native_max_iterations",
                     f"Reached {max_iterations} tool calls, stopping with {len(issues)} issues")
        raw_analysis = (
            f"审核在 {max_iterations} 次工具调用后强制终止，"
            f"已完成 {len(issues)} 个问题的记录。"
        )
        _emit({"type": "complete", "summary": raw_analysis, "issues_count": len(issues)})

    # 持久化完整对话跟踪
    _save_trace(
        task_id, doc_id, doc_name,
        issues_count=len(issues),
        total_iterations=iteration + 1,
        messages=messages,
        provider="deepseek",
        model=model,
        finished=finished,
    )

    return _build_result(task_id, doc_id, doc_name, issues, raw_analysis)


# ═══════════════════════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════════════════════

def _run_structured_llm_loop(
    parsed_content: str,
    structure: DocumentStructure | None,
    kb_ids: list[str],
    doc_name: str,
    task_id: str,
    doc_id: str,
    event_callback: Callable[[dict], None] | None = None,
) -> AuditResult:
    """使用 structured_llm + AgentAction 模型执行审核（降级路径）。

    通过 as_structured_llm 让 LLM 输出 AgentAction JSON 来表达工具调用意图。
    适用非 DeepSeek provider（MiniMax、OpenAI 等）或 DeepSeek 原生路径失败时。
    """
    def _emit(event: dict):
        # 存入共享日志
        with _task_log_lock:
            if task_id not in _task_event_logs:
                _task_event_logs[task_id] = []
            _task_event_logs[task_id].append(event)
        # 同时推送给当前 SSE 连接的回调
        if event_callback:
            try:
                event_callback(event)
            except Exception:
                pass

    _emit({"type": "start", "message": "Agentic 审核开始 (structured_llm 模式)"})

    llm = get_llm()
    try:
        structured_llm = llm.as_structured_llm(output_cls=AgentAction)
    except Exception as e:
        _emit({"type": "error", "message": f"structured_llm 初始化失败: {e}"})
        _logger.warning("as_structured_llm failed: %s, agentic audit unavailable", e)
        return _build_result(
            task_id, doc_id, doc_name, [],
            f"Agentic 审核不可用（structured_llm 初始化失败: {e}）",
        )

    issues: list[AuditIssue] = []
    issue_count_before = 0
    messages = [
        _build_system_msg(),
        _build_init_msg(doc_name, structure, parsed_content),
    ]

    raw_analysis = ""
    consecutive_failures = 0
    finished = False

    for turn in range(MAX_TURNS):
        # 检查任务是否被取消
        try:
            from storage.audit_task_repo import get_task
            current_task = get_task(task_id)
            if current_task and current_task.status == "cancelled":
                _emit({"type": "cancelled", "message": "审核任务已被取消"})
                _logger.info("structured_llm audit task %s cancelled at turn %d", task_id, turn)
                raw_analysis = f"审核已取消（第 {turn} 轮），已记录 {len(issues)} 个问题。"
                break
        except Exception:
            pass

        try:
            response = structured_llm.chat(messages)
            action: AgentAction = response.raw
        except Exception:
            _deg_record("agentic_audit", "structured_llm_failed",
                        f"Turn {turn}: structured_llm failed, trying fallback parse")
            try:
                resp = llm.chat(messages)
                action = _parse_action_fallback(resp.message.content or "")
            except Exception:
                action = None

            if action is None:
                consecutive_failures += 1
                _logger.warning(
                    "agentic audit turn %d: failed to parse action, "
                    "consecutive failures=%d", turn, consecutive_failures,
                )
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    _deg_record("agentic_audit", "too_many_failures",
                                f"Turn {turn}: {consecutive_failures} consecutive parse failures, aborting")
                    raw_analysis = (
                        f"Agentic 审核在 {turn} 轮后因连续解析失败中止，"
                        f"已记录 {len(issues)} 个问题。"
                    )
                    _emit({"type": "error", "message": raw_analysis})
                    break
                continue
            else:
                consecutive_failures = 0

        consecutive_failures = 0

        # 发送 thought 事件
        _emit({"type": "reasoning", "content": action.thought})

        messages.append(ChatMessage(
            role=MessageRole.ASSISTANT,
            content=action.thought,
        ))

        if action.action == "finish":
            raw_analysis = action.final_summary or "审核完成（Agent 未提供总结）"
            finished = True
            _emit({"type": "complete", "summary": raw_analysis, "issues_count": len(issues)})
            _logger.info(
                "agentic audit finished after %d turns, %d issues found",
                turn + 1, len(issues),
            )
            break

        # 执行工具前发送 tool_call 事件
        if action.action != "flag_issue":
            tool_name = action.action
            tool_args = {}
            if tool_name == "read_chapter":
                tool_args = {"chapter_index": action.chapter_index}
            elif tool_name == "search_kb_text":
                tool_args = {"query": action.search_query}
            elif tool_name == "search_kb":
                tool_args = {"query": action.search_query, "top_k": action.search_top_k}
            _emit({"type": "tool_call", "tool": tool_name, "args": tool_args})

        tool_result = _execute_tool(
            action, parsed_content, structure, kb_ids, doc_name, issues,
        )
        messages.append(_build_tool_result_msg(tool_result))

        # 发送 tool_result 或 issue_found 事件
        if action.action == "flag_issue":
            if len(issues) > issue_count_before:
                new_issue = issues[-1]
                _emit({
                    "type": "issue_found",
                    "issue": {
                        "id": new_issue.id,
                        "type": new_issue.type,
                        "severity": new_issue.severity,
                        "description": new_issue.description[:300],
                        "standard_name": new_issue.standard_reference.standard_name if new_issue.standard_reference else None,
                        "standard_clause": new_issue.standard_reference.clause if new_issue.standard_reference else None,
                    },
                })
                issue_count_before = len(issues)
        else:
            _emit({"type": "tool_result", "tool": action.action, "content": tool_result})

    else:
        _deg_record("agentic_audit", "max_turns_exhausted",
                     f"Reached {MAX_TURNS} turns, stopping with {len(issues)} issues")
        raw_analysis = (
            f"审核在 {MAX_TURNS} 轮后强制终止，已完成 {len(issues)} 个问题的记录。"
        )
        _emit({"type": "complete", "summary": raw_analysis, "issues_count": len(issues)})

    # 持久化完整对话跟踪（序列化 ChatMessage 为 dict）
    serializable_messages = []
    for m in messages:
        sm = {"role": str(m.role), "content": m.content or ""}
        if hasattr(m, "additional_kwargs") and m.additional_kwargs:
            sm["additional_kwargs"] = m.additional_kwargs
        serializable_messages.append(sm)
    _save_trace(
        task_id, doc_id, doc_name,
        issues_count=len(issues),
        total_iterations=turn + 1,
        messages=serializable_messages,
        provider=os.environ.get("LLM_PROVIDER", "unknown"),
        finished=finished,
    )

    return _build_result(task_id, doc_id, doc_name, issues, raw_analysis)


def run_agentic_audit(
    parsed_content: str,
    structure: DocumentStructure | None,
    kb_ids: list[str],
    doc_name: str,
    task_id: str,
    doc_id: str,
    event_callback: Callable[[dict], None] | None = None,
) -> AuditResult:
    """Agentic 审核主入口。

    DeepSeek provider → 原生 function calling + thinking 模式（更稳定、更准确）。
    其他 provider   → structured_llm + AgentAction JSON（降级路径）。

    Args:
        event_callback: 流式事件回调，接收 {"type": ..., ...} 字典。
    """
    provider = os.environ.get("LLM_PROVIDER", "").lower()

    if provider == "deepseek":
        _logger.info("Using DeepSeek native function calling path")
        try:
            return _run_native_tool_calling(
                parsed_content, structure, kb_ids,
                doc_name, task_id, doc_id,
                event_callback=event_callback,
            )
        except Exception as e:
            _logger.warning(
                "Native function calling failed (%s), falling back to structured_llm", e,
            )
            if event_callback:
                try:
                    event_callback({"type": "progress", "message": f"原生路径失败，降级到 structured_llm: {e}"})
                except Exception:
                    pass
            _deg_record("agentic_audit", "native_failed_fallback",
                        f"Native path failed: {e}, falling back to structured_llm")

    return _run_structured_llm_loop(
        parsed_content, structure, kb_ids,
        doc_name, task_id, doc_id,
        event_callback=event_callback,
    )
