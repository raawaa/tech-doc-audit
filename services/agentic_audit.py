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
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from llama_index.core.llms import ChatMessage, MessageRole

from core.logger import get_logger
from core.settings import get_llm, make_deepseek_client
from services.agent_tools import search_kb, search_kb_text
from services.agent_trace import save_trace
from core.degradation import record as _deg_record
from models.audit_document import DocumentStructure
from models.audit_task import (
    AuditIssue, AuditResult, IssueLocation,
    ResultSummary, StandardRef,
)
from models.llm_schemas import AgentAction, Final, ToolCalls
from services.standard_linker import link_standards

_logger = get_logger(__name__)

# trace 文件存放目录，默认 data/audits/{doc_id}/tasks/traces/
_TRACE_DIR = Path(
    os.environ.get("AUDIT_DATA_DIR", "data")
) / "audits"

MAX_TURNS = 30
CHAPTER_MAX_CHARS = 8000
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


def _make_emitter(
    task_id: str,
    event_callback: Callable[[dict], None] | None,
) -> Callable[[dict], None]:
    """构造 audit loop 共用的事件发射闭包。

    每次调用把事件 append 进 per-task 共享日志（供 SSE 长轮询读取），
    并尽力推给当前 SSE 连接的 callback（失败静默，不阻塞审核）。

    audit 的 native 与 structured 两 loop 曾逐字重复此逻辑，抽到此处复用。
    qa 的 _emit 不写共享日志、结构不同，不复用本函数。
    """
    def _emit(event: dict):
        with _task_log_lock:
            if task_id not in _task_event_logs:
                _task_event_logs[task_id] = []
            _task_event_logs[task_id].append(event)
        if event_callback:
            try:
                event_callback(event)
            except Exception:
                pass

    return _emit


def _check_cancelled(
    task_id: str,
    emit: Callable[[dict], None],
    turn: int,
    issues_count: int,
) -> str | None:
    """检查审核任务是否已被取消。

    已取消：发 cancelled 事件、记日志，返回用于 raw_analysis 的文案。
    未取消：返回 None。
    读取任务状态本身失败时不阻塞审核，返回 None。
    """
    try:
        from storage.audit_task_repo import get_task
        current_task = get_task(task_id)
    except Exception:
        return None

    if current_task and current_task.status == "cancelled":
        emit({"type": "cancelled", "message": "审核任务已被取消"})
        _logger.info("audit task %s cancelled at turn %d", task_id, turn)
        return f"审核已取消（第 {turn} 轮），已记录 {issues_count} 个问题。"
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# System Prompt
# ═══════════════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """你是一个严格的技术文档审核专家。你的任务是对照知识库中的标准规范，审核文档是否合规。
你无法访问互联网或任何外部信息源，只能通过以下 action 操作知识库中的标准文档。

## 审核流程

1. 首先查看下方「知识库概况」，了解知识库中实际有哪些标准文档可用
2. 仔细阅读文档全文（或文档开头部分）
3. 优先审核知识库中有对应标准的领域；对于 KB 中明显缺少标准的领域，
   可记录为 consistency/completeness 类问题（文档内部可发现的问题），
   但不要浪费过多轮次在无法核验的 compliance 问题上
4. 从文档内容中提炼具体的技术关键词，调用 search_kb 或 search_kb_text 搜索相关标准规范
5. 逐条比对文档内容与搜索到的标准条款
6. 发现问题立即调用 flag_issue 记录
7. 对文档的不同主题/技术点，使用不同的关键词多角度搜索
8. 如果文档很长，当前未显示完整内容，可调用 read_chapter 查看更多

## 搜索策略

search_kb（语义向量搜索）vs search_kb_text（精确文本搜索）的选择规则：
- search_kb：适合搜索概念性要求（如"质保期要求"、"验收标准"、"防水等级"），能匹配同义词。速度较慢，占GPU。
- search_kb_text：适合搜索具体术语/编号（如"IP65"、"GB/T 12345"、"镀锌钢管"），精确命中。速度快，不占GPU，但无法匹配同义词。
- 遇到标准编号/参数值/专有名词时优先用 search_kb_text
- 遇到概念描述时优先用 search_kb
- 从文档内容中提取具体技术术语作为 search_query
- 使用不同角度和关键词多次搜索
- ⚠️ 关于 relevance 分数的重要提醒：
  relevance 由向量距离计算，高分不保证真正相关。关键判断标准是：
  返回的条款内容在**技术领域**上是否与你的搜索意图匹配。
  例如：搜索"抗震设防"但 top result 来自"灯具光生物安全"标准，
  即使 relevance > 0.9 也应视为不相关，直接换词重搜。
- 如果多次搜索（≥3次）的 top results 都来自同一份标准文档的同一领域，
  且该领域与当前审核内容明显不匹配，说明知识库中缺少相关标准。
  此时应停止当前方向的搜索，转向文档中下一个可审核的主题。
- 搜索结果末尾的 ⚠️ 来源单一性警告是重要信号——如果出现，应立即换关键词

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
- 建议提供 standard_doc_id、standard_page_number、standard_chunk_text（来自 search_kb 返回结果），
  用于在审核报告中生成可点击跳转到标准 PDF 原文的链接
- ⚠️ 调用 flag_issue 前，先确认同一问题没有在之前的轮次中已标记过。系统会自动拒绝重复标记。

## 注意事项
- 每次只执行一个操作（一个 action）
- thought 中简要说明当前推理：在审哪个章节、看到了什么、为什么选择这个操作
- 对于 compliance 类型问题，不要在没有搜索到相关标准时就调用 flag_issue
- consistency/completeness 类问题可在文档内部直接发现，不强制要求外部标准
- 直接输出 JSON 格式，不要用 Markdown 包裹
- 调用 finish 时在 final_summary 中总结发现的所有问题数量和主要类型
- 如果知识库中缺少文档引用的大部分标准（如文档引用了30个标准但KB只有1-2个），
  在完成可直接发现的问题后应尽快调用 finish 结束审核，不要反复搜索"""


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


def _get_kb_docs_summary(kb_ids: list[str]) -> str:
    """构建知识库文档概况，帮助 Agent 了解 KB 中实际有哪些标准可用。"""
    import storage.kb_repo as kb_repo

    if not kb_ids:
        return "（未选择知识库）"

    lines = ["## 知识库概况\n"]
    lines.append("以下是你可以在本次审核中搜索的标准文档：\n")

    total_docs = 0
    for kb_id in kb_ids:
        kb = kb_repo.get(kb_id)
        if not kb:
            continue
        lines.append(f"### {kb.name}（{kb.category}）")
        # 从 KB 的 document_ids 获取文档列表
        doc_ids = getattr(kb, "document_ids", []) or []
        if not doc_ids:
            lines.append("  （无文档）")
            continue
        for did in doc_ids[:30]:  # 最多显示30个
            # 尝试读取 meta 文件获取文档名称
            meta_path = DATA_DIR / "kbs" / kb_id / "meta" / f"{did}.json"
            doc_name = did  # fallback
            if meta_path.exists():
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.loads(f.read())
                    doc_name = meta.get("name", did)
                except Exception:
                    pass
            lines.append(f"  - {doc_name} (id: {did})")
            total_docs += 1
        lines.append("")

    lines.insert(2, f"（共 {total_docs} 份标准文档）\n")
    lines.append(
        "⚠️ 重要提示：如上所示，知识库中的标准文档可能有限。"
        "审核时请优先关注知识库中有对应标准的领域。"
        "对于知识库中明显缺少相关标准的领域，"
        "可记录文档内部可发现的 consistency/completeness 问题，"
        "但不要在 compliance 审核上反复搜索不存在的标准。"
    )
    return "\n".join(lines)


DATA_DIR = Path(
    os.environ.get("AUDIT_DATA_DIR", "data")
)


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


def _tool_flag_issue(action: AgentAction, issues: list[AuditIssue]) -> str:
    """记录审核问题（含去重检查）。"""
    warnings = []
    if not action.cited_excerpt:
        warnings.append("缺少 cited_excerpt（原文引用），建议补充以增强证据力度")
    if not action.standard_name:
        warnings.append("缺少 standard_name（标准名称），建议补充以标明依据来源")
    if not action.issue_description:
        warnings.append("缺少 description（问题描述），这是必填项")

    # ── 去重检查 ──
    for existing in issues:
        # 检查1：相同的原文引用（标准化后比较）
        if action.cited_excerpt and existing.cited_excerpt:
            new_excerpt = " ".join(action.cited_excerpt.strip().split())
            old_excerpt = " ".join(existing.cited_excerpt.strip().split())
            if new_excerpt == old_excerpt and len(new_excerpt) >= 10:
                return (
                    f"⚠️ 重复标记：该原文引用已在问题 #{existing.id} 中记录"
                    f"（类型: {existing.type}, 严重程度: {existing.severity}），"
                    f"跳过本次标记。请勿对同一位置/同一问题重复调用 flag_issue。"
                )
        # 检查2：相同章节位置 + 相同原文引用（即使引用较短）
        if (action.cited_excerpt and existing.cited_excerpt
                and action.document_position and existing.document_position):
            new_excerpt = " ".join(action.cited_excerpt.strip().split())
            old_excerpt = " ".join(existing.cited_excerpt.strip().split())
            new_pos = " ".join(action.document_position.strip().split())
            old_pos = " ".join(existing.document_position.strip().split())
            if new_excerpt == old_excerpt and new_pos == old_pos:
                return (
                    f"⚠️ 重复标记：相同位置（{action.document_position}）"
                    f"且相同原文引用的问题已在问题 #{existing.id} 中记录，"
                    f"跳过本次标记。"
                )
        # 检查3：相同章节位置 + 相同描述
        if (action.issue_description and existing.description
                and action.document_position and existing.document_position):
            new_desc = " ".join(action.issue_description.strip().split())
            old_desc = " ".join(existing.description.strip().split())
            new_pos = " ".join(action.document_position.strip().split())
            old_pos = " ".join(existing.document_position.strip().split())
            if new_desc == old_desc and new_pos == old_pos:
                return (
                    f"⚠️ 重复标记：相同位置（{action.document_position}）"
                    f"且相同描述的问题已在问题 #{existing.id} 中记录，"
                    f"跳过本次标记。"
                )

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
            doc_id=action.standard_doc_id,
            page_number=action.standard_page_number,
            chunk_text=action.standard_chunk_text,
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
    kb_ids: list[str] | None = None,
) -> ChatMessage:
    DOC_FULL_THRESHOLD = 30000
    structure_text = _tool_get_structure(structure, doc_name)
    kb_summary = _get_kb_docs_summary(kb_ids or [])

    if len(parsed_content) <= DOC_FULL_THRESHOLD:
        content = (
            f"{kb_summary}\n\n"
            f"请审核文档《{doc_name}》。\n\n"
            f"文档结构：\n{structure_text}\n\n"
            f"=== 文档全文 ===\n{parsed_content}"
        )
    else:
        content = (
            f"{kb_summary}\n\n"
            f"请审核文档《{doc_name}》。\n\n"
            f"文档结构：\n{structure_text}\n\n"
            f"=== 文档开头（共{len(parsed_content)}字）===\n"
            f"{parsed_content[:8000]}\n"
            f"\n（文档较长，如需查看更多内容请使用 read_chapter 工具）"
        )
    return ChatMessage(role=MessageRole.USER, content=content)


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
                    "standard_doc_id": {
                        "type": "string",
                        "description": (
                            "标准文档的 ID，从 search_kb 返回结果的 doc_id 字段获取。"
                            "可选，但强烈建议提供——使审核结果可跳转到标准 PDF 原文。"
                        ),
                    },
                    "standard_page_number": {
                        "type": "integer",
                        "description": (
                            "标准条款所在页码，从 search_kb 返回结果的页码字段获取。"
                            "从1开始计数。可选。"
                        ),
                    },
                    "standard_chunk_text": {
                        "type": "string",
                        "description": (
                            "标准条款的原文片段，从 search_kb 返回的内容中摘录。"
                            "可选，用于在 PDF 中高亮定位。"
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

1. 首先查看下方「知识库概况」，了解知识库中实际有哪些标准文档可用
2. 仔细阅读文档全文（或文档开头部分）
3. 优先审核知识库中有对应标准的领域；对于 KB 中明显缺少标准的领域，
   可记录为 consistency/completeness 类问题（文档内部可发现的问题），
   但不要浪费过多轮次在无法核验的 compliance 问题上
4. 从文档内容中提炼具体的技术关键词，调用搜索工具查找相关标准规范
5. 逐条比对文档内容与搜索到的标准条款
6. 发现问题立即调用 flag_issue 记录
7. 对文档的不同章节/主题，使用不同的关键词多角度搜索
8. 如果文档很长，当前未显示的部分需要查看更多时，调用 read_chapter 读取

## 搜索策略

- 使用不同角度和关键词多次搜索，覆盖文档涉及的各个技术领域
- ⚠️ 关于 relevance 分数的重要提醒：
  relevance 由向量距离计算，高分不保证真正相关。关键判断标准是：
  返回的条款内容在**技术领域**上是否与你的搜索意图匹配。
  例如：搜索"抗震设防"但 top result 来自"灯具光生物安全"标准，
  即使 relevance > 0.9 也应视为不相关，直接换词重搜。
- 如果多次搜索（≥3次）的 top results 都来自同一份标准文档的同一领域，
  且该领域与当前审核内容明显不匹配，说明知识库中缺少相关标准。
  此时应停止当前方向的搜索，转向文档中下一个可审核的主题。
- 搜索结果末尾的 ⚠️ 来源单一性警告是重要信号——如果出现，应立即换关键词
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
- 建议提供 standard_doc_id、standard_page_number、standard_chunk_text
  来自搜索工具返回结果中的 doc_id、页码、内容字段
- ⚠️ 调用 flag_issue 前，先确认同一问题（相同 cited_excerpt 或相同章节+相同描述）
  没有在之前的轮次中已标记过。系统会自动拒绝重复标记。

## 注意事项
- compliance 类型问题必须先搜索到相关标准才能调用 flag_issue
- consistency/completeness 类问题可在文档内部直接发现，不强制外部标准
- 不要在文档内容合规时强行找问题
- 工具调用失败时，根据返回的错误提示调整参数重试，不要放弃
- 如果知识库中缺少文档引用的大部分标准（如文档引用了30个标准但KB只有1-2个），
  在完成可直接发现的问题后应尽快给出审核总结，不要反复搜索"""


# ═══════════════════════════════════════════════════════════════════════════════
# 统一 agent loop（ADR-0001）
# ═══════════════════════════════════════════════════════════════════════════════


def _dispatch_tool(
    tool_name: str,
    tool_args: dict,
    parsed_content: str,
    structure: DocumentStructure | None,
    kb_ids: list[str],
    doc_name: str,
    issues: list[AuditIssue],
) -> str:
    """统一工具分发：将归一化的 tool_call 路由到对应实现。"""
    if tool_name == "read_chapter":
        return _tool_read_chapter(
            parsed_content, structure,
            tool_args.get("chapter_index", 1),
        )
    elif tool_name == "search_kb":
        return search_kb(
            kb_ids,
            tool_args.get("query", ""),
            tool_args.get("top_k", 5),
        )
    elif tool_name == "search_kb_text":
        return search_kb_text(kb_ids, tool_args.get("query", ""))
    elif tool_name == "flag_issue":
        action = AgentAction(
            thought="",
            action="flag_issue",
            issue_type=tool_args.get("issue_type"),
            issue_severity=tool_args.get("severity"),
            issue_description=tool_args.get("description"),
            standard_name=tool_args.get("standard_name"),
            standard_clause=tool_args.get("standard_clause"),
            standard_requirement=tool_args.get("standard_requirement"),
            cited_excerpt=tool_args.get("cited_excerpt"),
            document_position=tool_args.get("document_position"),
            issue_suggestion=tool_args.get("suggestion"),
            standard_doc_id=tool_args.get("standard_doc_id"),
            standard_page_number=tool_args.get("standard_page_number"),
            standard_chunk_text=tool_args.get("standard_chunk_text"),
        )
        return _tool_flag_issue(action, issues)
    return (
        f"未知工具: {tool_name}。"
        f"可用的工具有：read_chapter、search_kb、search_kb_text、flag_issue。"
        f"请从上述工具中选择正确的工具重新调用。"
    )


def _dict_to_chat_message(d: dict) -> ChatMessage:
    """将统一 loop 的消息 dict 转回 LlamaIndex ChatMessage（供 structured adapter）。"""
    role_map = {
        "system": MessageRole.SYSTEM,
        "user": MessageRole.USER,
        "assistant": MessageRole.ASSISTANT,
        "tool": MessageRole.USER,
    }
    role = role_map.get(d.get("role", "user"), MessageRole.USER)
    content = d.get("content", "")
    if d.get("role") == "tool":
        content = f"[工具结果]\n{content}"
    return ChatMessage(role=role, content=content)


class NativeLLMStep:
    """原生 function calling adapter：OpenAI tools + thinking 模式。"""

    def __init__(self) -> None:
        self.client = make_deepseek_client()
        self.model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

    def step(
        self, messages: list[dict], emit: Callable[[dict], None],
    ) -> Final | ToolCalls:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=_TOOLS_SPEC,
            extra_body={"thinking": {"type": "enabled"}},
        )
        msg = response.choices[0].message

        if msg.reasoning_content:
            emit({"type": "reasoning", "content": msg.reasoning_content[:2000]})

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

        if not msg.tool_calls:
            return Final(answer=msg.content or "")

        return ToolCalls(calls=[
            {
                "name": tc.function.name,
                "args": _safe_json_parse(tc.function.arguments),
                "id": tc.id,
            }
            for tc in msg.tool_calls
        ])


class StructuredLLMStep:
    """structured_llm adapter：LlamaIndex as_structured_llm → AgentAction。"""

    def __init__(self, llm, structured_llm) -> None:
        self.llm = llm
        self.structured_llm = structured_llm

    def step(
        self, messages: list[dict], emit: Callable[[dict], None],
    ) -> Final | ToolCalls:
        chat_messages = [_dict_to_chat_message(m) for m in messages]

        try:
            response = self.structured_llm.chat(chat_messages)
            action: AgentAction = response.raw
        except Exception:
            _deg_record("agentic_audit", "structured_llm_failed",
                        "structured_llm.chat failed, trying fallback parse")
            try:
                resp = self.llm.chat(chat_messages)
                action = _parse_action_fallback(resp.message.content or "")
            except Exception:
                action = None

            if action is None:
                raise RuntimeError("structured_llm parse failed after fallback")

        emit({"type": "reasoning", "content": action.thought})

        messages.append({"role": "assistant", "content": action.thought})

        if action.action == "finish":
            return Final(answer=action.final_summary or "审核完成")

        args = _agent_action_to_args(action)
        return ToolCalls(calls=[{"name": action.action, "args": args, "id": ""}])


class StreamingLLMStep:
    """流式 function calling adapter：OpenAI tools + streaming + 增量事件发射。

    与 NativeLLMStep 不同：使用 stream=True，在 LLM 调用期间逐 token 发送
    reasoning_start / reasoning_delta / reasoning_end 和
    text_start / text_delta / text_end 事件，并将增量到达的 tool_call
    参数累积后返回 ToolCalls。
    """

    def __init__(self, tools_spec: list[dict]) -> None:
        self.client = make_deepseek_client()
        self.model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
        self.tools_spec = tools_spec

    def step(
        self, messages: list[dict], emit: Callable[[dict], None],
    ) -> Final | ToolCalls:
        try:
            stream = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=self.tools_spec,
                stream=True,
                extra_body={"thinking": {"type": "enabled"}},
            )
        except Exception as e:
            raise RuntimeError(f"LLM streaming call failed: {e}")

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_call_accumulators: dict[int, dict] = {}
        reasoning_started = False
        reasoning_ended = False
        text_started = False

        try:
            for chunk in stream:
                delta = chunk.choices[0].delta

                rc = getattr(delta, 'reasoning_content', None)
                if rc:
                    if not reasoning_started:
                        reasoning_started = True
                        emit({"type": "reasoning_start"})
                    reasoning_parts.append(rc)
                    emit({"type": "reasoning_delta", "content": rc})

                if delta.content:
                    if reasoning_started and not reasoning_ended:
                        emit({"type": "reasoning_end"})
                        reasoning_ended = True
                    if not text_started:
                        text_started = True
                        emit({"type": "text_start"})
                    content_parts.append(delta.content)
                    emit({"type": "text_delta", "content": delta.content})

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
            raise RuntimeError(f"流式响应中断: {e}")

        if reasoning_started and not reasoning_ended:
            emit({"type": "reasoning_end"})
        if text_started:
            emit({"type": "text_end"})

        content = "".join(content_parts)
        reasoning_content = "".join(reasoning_parts)

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

        assistant_msg: dict = {"role": "assistant", "content": content}
        if reasoning_content:
            assistant_msg["reasoning_content"] = reasoning_content
        if tool_calls_list:
            assistant_msg["tool_calls"] = tool_calls_list
        messages.append(assistant_msg)

        if not tool_calls_list:
            return Final(answer=content)

        return ToolCalls(calls=[
            {
                "name": tc["function"]["name"],
                "args": _safe_json_parse(tc["function"]["arguments"]),
                "id": tc["id"],
            }
            for tc in tool_calls_list
        ])


def _safe_json_parse(s: str) -> dict:
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return {}


def _agent_action_to_args(action: AgentAction) -> dict:
    """将 AgentAction 字段映射为统一的 tool_calls args dict。"""
    if action.action == "read_chapter":
        return {"chapter_index": action.chapter_index}
    elif action.action == "search_kb":
        return {"query": action.search_query, "top_k": action.search_top_k}
    elif action.action == "search_kb_text":
        return {"query": action.search_query}
    elif action.action == "flag_issue":
        return {
            "issue_type": action.issue_type,
            "severity": action.issue_severity,
            "description": action.issue_description,
            "standard_name": action.standard_name,
            "standard_clause": action.standard_clause,
            "standard_requirement": action.standard_requirement,
            "cited_excerpt": action.cited_excerpt,
            "document_position": action.document_position,
            "suggestion": action.issue_suggestion,
            "standard_doc_id": action.standard_doc_id,
            "standard_page_number": action.standard_page_number,
            "standard_chunk_text": action.standard_chunk_text,
        }
    return {}


@dataclass
class LoopOutput:
    """统一 agent loop 的原始输出，由调用方按场景做后处理。"""
    raw_analysis: str
    messages: list[dict]
    finished: bool
    issues: list  # list[AuditIssue] in audit mode, [] in QA mode


def run_agent_loop(
    llm_step,
    initial_messages: list[dict],
    parsed_content: str = "",
    structure: DocumentStructure | None = None,
    kb_ids: list[str] | None = None,
    doc_name: str = "",
    task_id: str = "",
    doc_id: str = "",
    event_callback: Callable[[dict], None] | None = None,
    max_turns: int = MAX_TURNS,
    start_event_msg: str = "Agentic 审核开始",
    model_provider: str = "",
    model_name: str = "",
    *,
    emitter: Callable[[dict], None] | None = None,
    tool_executor: Callable[[str, dict], str] | None = None,
    cancel_checker: Callable[[], str | None] | None = None,
    max_consecutive_failures: int | None = None,
) -> LoopOutput:
    """统一 agent loop：迭代、cancel、工具分发。

    llm_step 是实现 LLMStep 协议的对象：step(messages, emit) -> StepResult。
    native / structured / streaming 各提供一个 adapter，loop 主体无分支。

    audit 场景：不传 emitter / tool_executor / cancel_checker，
        使用内建的 _make_emitter / _dispatch_tool / _check_cancelled，
        跟踪 issues，发射 issue_found。
    QA 场景：传入 emitter / tool_executor，不传 cancel_checker，
        跳过 issue 跟踪，所有工具结果统一发射 tool_result。

    Returns:
        LoopOutput：调用方据此做 save_trace / link_standards / 结果构建。
    """
    default_emit = _make_emitter(task_id, event_callback)
    _emit = emitter or default_emit

    if start_event_msg:
        _emit({"type": "start", "message": start_event_msg})

    issues: list[AuditIssue] = []
    issue_count_before = 0
    messages = initial_messages

    raw_analysis = ""
    consecutive_failures = 0
    finished = False

    # 是否处于 audit 模式（有自己的 emitter + tool dispatch）
    is_audit = tool_executor is None
    kb_ids = kb_ids or []

    def _audit_tool_executor(name: str, args: dict) -> str:
        return _dispatch_tool(
            name, args, parsed_content, structure, kb_ids, doc_name, issues,
        )

    _exec_tool = tool_executor or _audit_tool_executor
    _max_failures = max_consecutive_failures if max_consecutive_failures is not None else MAX_CONSECUTIVE_FAILURES

    for turn in range(max_turns):
        # cancel check
        if is_audit and cancel_checker is None:
            cancelled = _check_cancelled(task_id, _emit, turn, len(issues))
        elif cancel_checker is not None:
            cancelled = cancel_checker()
        else:
            cancelled = None

        if cancelled is not None:
            raw_analysis = cancelled
            break

        try:
            result = llm_step.step(messages, _emit)
        except Exception as e:
            consecutive_failures += 1
            _logger.warning("agent loop turn %d step failed: %s", turn, e)
            if consecutive_failures >= _max_failures:
                _deg_record("agentic_audit", "too_many_failures",
                            f"Turn {turn}: {consecutive_failures} consecutive step failures, aborting")
                if is_audit:
                    raw_analysis = (
                        f"审核在 {turn} 轮后因连续失败中止，"
                        f"已记录 {len(issues)} 个问题。"
                    )
                else:
                    raw_analysis = (
                        f"问答在 {turn} 轮后因连续失败中止。"
                    )
                _emit({"type": "error", "message": raw_analysis})
                break
            continue

        consecutive_failures = 0

        if isinstance(result, Final):
            raw_analysis = result.answer or ("" if not is_audit else "审核完成")
            finished = True
            _emit({"type": "complete", "summary": raw_analysis, "issues_count": len(issues)})
            _logger.info("agent loop finished, %d issues found", len(issues))
            break

        # result is ToolCalls
        for tc in result.calls:
            func_name = tc.get("name", "")
            func_args = tc.get("args", {})
            tc_id = tc.get("id", "")

            _emit({"type": "tool_call", "tool": func_name, "args": func_args})
            _logger.debug("agent tool call: %s(%s)", func_name, func_args)

            try:
                tool_result = _exec_tool(func_name, func_args)
            except Exception as e:
                tool_result = f"工具执行失败: {e}"
                _emit({"type": "error", "message": f"{func_name} 执行失败: {e}"})

            if is_audit and func_name == "flag_issue" and len(issues) > issue_count_before:
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
                        "standard_doc_id": new_issue.standard_reference.doc_id if new_issue.standard_reference else None,
                        "standard_page_number": new_issue.standard_reference.page_number if new_issue.standard_reference else None,
                        "standard_chunk_text": new_issue.standard_reference.chunk_text if new_issue.standard_reference else None,
                    },
                })
                issue_count_before = len(issues)
            else:
                _emit({"type": "tool_result", "tool": func_name, "content": tool_result})

            messages.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": tool_result,
            })
    else:
        if is_audit:
            _deg_record("agentic_audit", "max_turns_exhausted",
                         f"Reached {max_turns} turns, stopping with {len(issues)} issues")
            raw_analysis = (
                f"审核在 {max_turns} 轮后强制终止，"
                f"已完成 {len(issues)} 个问题的记录。"
            )
        else:
            _deg_record("agentic_qa", "max_turns_exhausted",
                         f"Reached {max_turns} turns")
            raw_analysis = "抱歉，问答搜索次数已达上限，请尝试缩小问题范围。"
        _emit({"type": "complete", "summary": raw_analysis, "issues_count": len(issues)})

    return LoopOutput(
        raw_analysis=raw_analysis,
        messages=messages,
        finished=finished,
        issues=issues,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════════════════════


def _build_native_initial_messages(
    parsed_content: str,
    structure: DocumentStructure | None,
    kb_ids: list[str],
    doc_name: str,
) -> list[dict]:
    kb_summary = _get_kb_docs_summary(kb_ids)
    DOC_FULL_THRESHOLD = 30000
    structure_text = _tool_get_structure(structure, doc_name)
    if len(parsed_content) <= DOC_FULL_THRESHOLD:
        user_content = (
            f"{kb_summary}\n\n"
            f"请审核文档《{doc_name}》。\n\n"
            f"文档结构：\n{structure_text}\n\n"
            f"=== 文档全文 ===\n{parsed_content}"
        )
    else:
        user_content = (
            f"{kb_summary}\n\n"
            f"请审核文档《{doc_name}》。\n\n"
            f"文档结构：\n{structure_text}\n\n"
            f"=== 文档开头（共{len(parsed_content)}字）===\n"
            f"{parsed_content[:8000]}\n"
            f"\n（文档较长，如需查看更多内容请使用 read_chapter 工具）"
        )
    return [
        {"role": "system", "content": NATIVE_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def _build_structured_initial_messages(
    doc_name: str,
    structure: DocumentStructure | None,
    parsed_content: str,
    kb_ids: list[str],
) -> list[dict]:
    sys_msg = _build_system_msg()
    init_msg = _build_init_msg(doc_name, structure, parsed_content, kb_ids)
    return [
        {"role": "system", "content": sys_msg.content},
        {"role": "user", "content": init_msg.content},
    ]


def _audit_post_process(
    loop_out: LoopOutput,
    task_id: str,
    doc_id: str,
    doc_name: str,
    kb_ids: list[str],
    model_provider: str,
    model_name: str,
) -> AuditResult:
    """audit 后处理：save_trace + link_standards + _build_result。"""
    provider = model_provider or os.environ.get("LLM_PROVIDER", "unknown")
    mname = model_name or os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
    save_trace(
        _TRACE_DIR / doc_id / "tasks" / "traces" / f"{task_id}_trace.json",
        loop_out.messages,
        metadata={
            "task_id": task_id,
            "doc_id": doc_id,
            "doc_name": doc_name,
            "provider": provider,
            "model": mname,
            "finished": loop_out.finished,
            "total_iterations": len(loop_out.messages),
            "issues_count": len(loop_out.issues),
        },
    )

    link_standards(loop_out.issues, kb_ids)
    return _build_result(task_id, doc_id, doc_name, loop_out.issues, loop_out.raw_analysis)


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

    内部通过统一 run_agent_loop 执行，native / structured 各有 LLMStep adapter。
    """
    provider = os.environ.get("LLM_PROVIDER", "").lower()

    if provider == "deepseek":
        _logger.info("Using DeepSeek native function calling path")
        try:
            initial_messages = _build_native_initial_messages(
                parsed_content, structure, kb_ids, doc_name,
            )
            native_step = NativeLLMStep()
            loop_out = run_agent_loop(
                llm_step=native_step,
                initial_messages=initial_messages,
                parsed_content=parsed_content,
                structure=structure,
                kb_ids=kb_ids,
                doc_name=doc_name,
                task_id=task_id,
                doc_id=doc_id,
                event_callback=event_callback,
                max_turns=100,
                start_event_msg="Agentic 审核开始 (DeepSeek thinking 模式)",
            )
            return _audit_post_process(
                loop_out, task_id, doc_id, doc_name, kb_ids,
                model_provider="deepseek", model_name=native_step.model,
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

    # structured_llm 路径（默认 / 降级）
    llm = get_llm()
    try:
        structured_llm = llm.as_structured_llm(output_cls=AgentAction)
    except Exception as e:
        _logger.warning("as_structured_llm failed: %s, agentic audit unavailable", e)
        _emit = _make_emitter(task_id, event_callback)
        _emit({"type": "error", "message": f"structured_llm 初始化失败: {e}"})
        return _build_result(
            task_id, doc_id, doc_name, [],
            f"Agentic 审核不可用（structured_llm 初始化失败: {e}）",
        )

    initial_messages = _build_structured_initial_messages(
        doc_name, structure, parsed_content, kb_ids,
    )
    structured_step = StructuredLLMStep(llm, structured_llm)
    loop_out = run_agent_loop(
        llm_step=structured_step,
        initial_messages=initial_messages,
        parsed_content=parsed_content,
        structure=structure,
        kb_ids=kb_ids,
        doc_name=doc_name,
        task_id=task_id,
        doc_id=doc_id,
        event_callback=event_callback,
        start_event_msg="Agentic 审核开始 (structured_llm 模式)",
    )
    return _audit_post_process(
        loop_out, task_id, doc_id, doc_name, kb_ids,
        model_provider=os.environ.get("LLM_PROVIDER", "unknown"),
        model_name=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
    )
