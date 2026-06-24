"""Agentic 审核管线。

Agent 自主调用工具逐章审核文档：
1. get_structure — 了解文档结构
2. read_chapter — 读取指定章节
3. search_kb   — 搜索知识库标准
4. flag_issue  — 记录审核问题
5. finish      — 审核完毕

对比 topic_audit（固定关键词 + 单次 LLM）：
- Agent 自主决定搜什么、搜多深
- 逐章推进，有上下文记忆
- 可迭代深挖可疑条款
"""

import json
import os
import re
from typing import Optional

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

MAX_TURNS = 30
CHAPTER_MAX_CHARS = 4000
MAX_CONSECUTIVE_FAILURES = 3


# ═══════════════════════════════════════════════════════════════════════════════
# System Prompt
# ═══════════════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """你是一个严格的技术文档审核专家。你的任务是对照知识库中的标准规范，逐章审核文档，发现不合规、不完整、不一致的问题。

## 必须遵守的审核流程

1. 首先调用 get_structure 了解文档结构
2. 逐章审核（从第1章到最后一章），不要跳过任何章节：
   a. 调用 read_chapter 读取本章全文
   b. 根据章节具体内容提炼 2-3 个不同的搜索关键词，逐次调用 search_kb 搜索相关标准
   c. 仔细比对文档内容与搜索到的标准条款
   d. 发现问题立即调用 flag_issue 记录
3. 全部章节审核完毕后，调用 finish 输出审核总结

## 搜索策略
- 从章节内容中提取具体技术术语作为 search_query（如"防护等级IP65"而非章节名"技术规格"）
- 每个章节至少搜索 2 次，使用不同角度/关键词
- 搜索结果 relevance < 0.3 可视为不相关，换词重搜
- 如果连续 2 次搜索均无相关结果，该章节可能与标准无关，继续下一章

## 判断标准
- compliance: 文档内容违反标准规定（数值不达标、方法错误等）
- completeness: 文档缺少标准要求的内容（缺失必要条款、参数未明确等）
- consistency: 文档内部数据矛盾，或与标准条文不一致
- insufficient_evidence: 证据不足，无法做出确定判断
- 无问题的章节不需要强行找问题

## flag_issue 要求
- cited_excerpt 必须从文档原文逐字引用作为证据
- standard_name 和 standard_clause 必须来自 search_kb 的返回结果
- document_position 注明章节名称
- issue_description 清晰说明问题和标准依据

## 注意事项
- 每次只执行一个操作（一个 action）
- thought 中简要说明当前进度和推理
- 直接输出 JSON 格式，不要用 Markdown 包裹"""


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
    label = ch.number or f"第{chapter_index}章"
    title = ch.title or ""

    text = _find_chapter_text(parsed_content, structure, chapter_index - 1)
    return _format_chapter_text(text, chapter_index, f"{label} {title}".strip())


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
        f"…（章节共 {len(text)} 字符，已显示前 {CHAPTER_MAX_CHARS} 字符，"
        f"剩余约 {remaining} 字符。如需继续阅读，请再次调用 read_chapter({index})）"
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
        return f"（搜索失败: {e}）"

    if not results:
        return f"（未找到与「{query}」相关的标准）"

    lines = [f"【知识库搜索结果（搜索词: {query}，共 {len(results)} 条）】"]
    for i, r in enumerate(results, 1):
        relevance = r.get("relevance", 0)
        doc = r.get("doc_source", "") or r.get("doc_id", "")
        clause = r.get("clause_number", "")
        section = r.get("section_path", "")
        content = (r.get("content", "") or "")[:800]

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


def _tool_flag_issue(action: AgentAction, issues: list[AuditIssue]) -> str:
    """记录审核问题。"""
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
    return f"问题 #{len(issues)} 已记录"


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

    if tool_name == "get_structure":
        return _tool_get_structure(structure, doc_name)

    elif tool_name == "read_chapter":
        idx = action.chapter_index or 1
        return _tool_read_chapter(parsed_content, structure, idx)

    elif tool_name == "search_kb":
        query = action.search_query or ""
        top_k = action.search_top_k or 5
        return _tool_search_kb(kb_ids, query, top_k)

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
) -> ChatMessage:
    structure_preview = _tool_get_structure(structure, doc_name)
    content = (
        f"请审核文档《{doc_name}》。\n\n"
        f"文档结构如下（也可调用 get_structure 重新获取）：\n"
        f"{structure_preview}\n\n"
        f"请从 get_structure 开始审核流程。"
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


# ═══════════════════════════════════════════════════════════════════════════════
# 原生 Function Calling 路径（DeepSeek thinking 模式）
# ═══════════════════════════════════════════════════════════════════════════════

_TOOLS_SPEC = [
    {
        "type": "function",
        "function": {
            "name": "get_structure",
            "description": "获取文档的章节结构（章名、条款数），了解文档全貌",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_chapter",
            "description": "读取指定章节的全文内容",
            "parameters": {
                "type": "object",
                "properties": {
                    "chapter_index": {
                        "type": "integer",
                        "description": "章节序号，从 1 开始",
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
            "description": "在知识库中搜索相关标准规范条款",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词，从章节内容中提取具体技术术语",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "返回结果数量，默认 5",
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
                "记录一个审核发现的问题。必须包含文档原文引用(cited_excerpt)和"
                "标准出处(standard_name + standard_clause)"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "issue_type": {
                        "type": "string",
                        "enum": ["compliance", "completeness", "consistency",
                                 "insufficient_evidence"],
                        "description": "问题类型",
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                        "description": "严重程度",
                    },
                    "description": {
                        "type": "string",
                        "description": "问题描述，清晰说明文档何处不符合标准",
                    },
                    "standard_name": {
                        "type": "string",
                        "description": "标准名称（来自 search_kb 结果）",
                    },
                    "standard_clause": {
                        "type": "string",
                        "description": "标准条款编号（如 3.2.1）",
                    },
                    "standard_requirement": {
                        "type": "string",
                        "description": "标准原文要求",
                    },
                    "cited_excerpt": {
                        "type": "string",
                        "description": "从文档原文逐字引用的证据",
                    },
                    "document_position": {
                        "type": "string",
                        "description": "引用在文档中的位置（章节名）",
                    },
                    "suggestion": {
                        "type": "string",
                        "description": "修改建议",
                    },
                },
                "required": ["issue_type", "severity", "description"],
            },
        },
    },
]

NATIVE_SYSTEM_PROMPT = """你是一个严格的技术文档审核专家。你的任务是对照知识库中的标准规范，逐章审核文档，发现不合规、不完整、不一致的问题。

## 必须遵守的审核流程

1. 首先调用 get_structure 了解文档结构
2. 逐章审核（从第 1 章到最后一章），不要跳过任何章节：
   a. 调用 read_chapter 读取本章全文
   b. 根据章节具体内容提炼 2-3 个不同的搜索关键词，逐次调用 search_kb 搜索相关标准
   c. 仔细比对文档内容与搜索到的标准条款
   d. 发现问题立即调用 flag_issue 记录
3. 全部章节审核完毕后，直接输出审核总结（不再调用工具）

## 搜索策略
- 从章节内容中提取具体技术术语作为 search_query（如"防护等级IP65"而非章节名"技术规格"）
- 每个章节至少搜索 2 次，使用不同角度/关键词
- 搜索结果 relevance < 0.3 可视为不相关，换词重搜
- 如果连续 2 次搜索均无相关结果，该章节可能与标准无关，继续下一章

## 判断标准
- compliance: 文档内容违反标准规定（数值不达标、方法错误等）
- completeness: 文档缺少标准要求的内容（缺失必要条款、参数未明确等）
- consistency: 文档内部数据矛盾，或与标准条文不一致
- insufficient_evidence: 证据不足，无法做出确定判断
- 无问题的章节不需要强行找问题

## flag_issue 要求
- cited_excerpt 必须从文档原文逐字引用作为证据
- standard_name 和 standard_clause 必须来自 search_kb 的返回结果
- document_position 注明章节名称
- description 清晰说明问题和标准依据"""


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
    if func_name == "get_structure":
        return _tool_get_structure(structure, doc_name)
    elif func_name == "read_chapter":
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
    return f"未知工具: {func_name}"


def _run_native_tool_calling(
    parsed_content: str,
    structure: DocumentStructure | None,
    kb_ids: list[str],
    doc_name: str,
    task_id: str,
    doc_id: str,
) -> AuditResult:
    """使用 DeepSeek 原生 function calling + thinking 模式执行审核。

    相比 structured_llm 路径的优势：
    - 原生工具调用，LLM 输出更稳定
    - thinking 模式启用，审核判断更准确
    - 支持一次请求内连续调用多个工具
    """
    from openai import OpenAI

    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

    client = OpenAI(api_key=api_key, base_url=base_url)

    issues: list[AuditIssue] = []
    messages: list[dict] = [
        {"role": "system", "content": NATIVE_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"请审核文档《{doc_name}》。\n\n"
                f"文档结构如下（也可调用 get_structure 重新获取）：\n"
                f"{_tool_get_structure(structure, doc_name)}\n\n"
                f"请从 get_structure 开始审核流程。"
            ),
        },
    ]

    raw_analysis = ""
    max_iterations = 100  # 工具调用总次数上限（一次请求可能多次调用）

    for _ in range(max_iterations):
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=_TOOLS_SPEC,
            extra_body={"thinking": {"type": "enabled"}},
        )

        msg = response.choices[0].message

        # 追加 assistant 消息（含 reasoning_content 和 tool_calls）
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

            _logger.debug("native tool call: %s(%s)", func_name, func_args)
            tool_result = _execute_native_tool(
                func_name, func_args,
                parsed_content, structure, kb_ids, doc_name, issues,
            )

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
) -> AuditResult:
    """使用 structured_llm + AgentAction 模型执行审核（降级路径）。

    通过 as_structured_llm 让 LLM 输出 AgentAction JSON 来表达工具调用意图。
    适用非 DeepSeek provider（MiniMax、OpenAI 等）或 DeepSeek 原生路径失败时。
    """
    llm = get_llm()
    try:
        structured_llm = llm.as_structured_llm(output_cls=AgentAction)
    except Exception as e:
        _logger.warning("as_structured_llm failed: %s, agentic audit unavailable", e)
        return _build_result(
            task_id, doc_id, doc_name, [],
            f"Agentic 审核不可用（structured_llm 初始化失败: {e}）",
        )

    issues: list[AuditIssue] = []
    messages = [
        _build_system_msg(),
        _build_init_msg(doc_name, structure),
    ]

    raw_analysis = ""
    consecutive_failures = 0

    for turn in range(MAX_TURNS):
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
                    break
                continue
            else:
                consecutive_failures = 0

        consecutive_failures = 0

        messages.append(ChatMessage(
            role=MessageRole.ASSISTANT,
            content=action.thought,
        ))

        if action.action == "finish":
            raw_analysis = action.final_summary or "审核完成（Agent 未提供总结）"
            _logger.info(
                "agentic audit finished after %d turns, %d issues found",
                turn + 1, len(issues),
            )
            break

        tool_result = _execute_tool(
            action, parsed_content, structure, kb_ids, doc_name, issues,
        )
        messages.append(_build_tool_result_msg(tool_result))

    else:
        _deg_record("agentic_audit", "max_turns_exhausted",
                     f"Reached {MAX_TURNS} turns, stopping with {len(issues)} issues")
        raw_analysis = (
            f"审核在 {MAX_TURNS} 轮后强制终止，已完成 {len(issues)} 个问题的记录。"
        )

    return _build_result(task_id, doc_id, doc_name, issues, raw_analysis)


def run_agentic_audit(
    parsed_content: str,
    structure: DocumentStructure | None,
    kb_ids: list[str],
    doc_name: str,
    task_id: str,
    doc_id: str,
) -> AuditResult:
    """Agentic 审核主入口。

    DeepSeek provider → 原生 function calling + thinking 模式（更稳定、更准确）。
    其他 provider   → structured_llm + AgentAction JSON（降级路径）。
    """
    provider = os.environ.get("LLM_PROVIDER", "").lower()

    if provider == "deepseek":
        _logger.info("Using DeepSeek native function calling path")
        try:
            return _run_native_tool_calling(
                parsed_content, structure, kb_ids,
                doc_name, task_id, doc_id,
            )
        except Exception as e:
            _logger.warning(
                "Native function calling failed (%s), falling back to structured_llm", e,
            )
            _deg_record("agentic_audit", "native_failed_fallback",
                        f"Native path failed: {e}, falling back to structured_llm")

    return _run_structured_llm_loop(
        parsed_content, structure, kb_ids,
        doc_name, task_id, doc_id,
    )
