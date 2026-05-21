import json
from typing import Optional

from models.audit_task import AuditIssue, AuditType, IssueLocation, StandardRef
from models.audit_document import AuditDocument, Clause
import services.search_service as search_svc
from services.llm_client import generate, generate_with_tools
from core.logger import get_logger

_logger = get_logger(__name__)


# Function/tool definition for audit issues
AUDIT_TOOL = {
    "type": "function",
    "function": {
        "name": "report_audit_issues",
        "description": "报告对某个技术条款的审核结果，包括发现的问题列表",
        "parameters": {
            "type": "object",
            "properties": {
                "issues": {
                    "type": "array",
                    "description": "发现的问题列表，如果没有问题则为空数组",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": ["compliance", "completeness", "consistency"],
                                "description": "问题类型：compliance=合规性, completeness=完整性, consistency=一致性",
                            },
                            "description": {
                                "type": "string",
                                "description": "问题描述",
                            },
                            "severity": {
                                "type": "string",
                                "enum": ["high", "medium", "low"],
                                "description": "严重程度",
                            },
                            "standard_reference": {
                                "type": "object",
                                "description": "引用的标准依据",
                                "properties": {
                                    "standard_name": {"type": "string", "description": "标准名称"},
                                    "standard_id": {"type": "string", "description": "标准编号"},
                                    "clause": {"type": "string", "description": "条款编号"},
                                    "requirement": {"type": "string", "description": "标准要求的具体内容"},
                                },
                                "required": ["standard_name", "standard_id"],
                            },
                            "suggestion": {
                                "type": "string",
                                "description": "修改建议",
                            },
                        },
                        "required": ["type", "description", "severity"],
                    },
                },
            },
            "required": ["issues"],
        },
    },
}


def audit_clause(
    clause: Clause,
    kb_ids: list[str],
    audit_types: list[AuditType],
    clause_index: int,
) -> list[AuditIssue]:
    """审核单个条款（Function Calling 方式）。"""
    issues = []

    # 获取相关知识库内容
    kb_content = search_svc.get_kb_content_for_audit(kb_ids, clause.text)

    audit_types_str = "、".join({
        "compliance": "合规性",
        "completeness": "完整性",
        "consistency": "一致性",
    }.get(t, t) for t in audit_types)

    # System prompt 放最前面 → 缓存命中（同批审核调用间不变）
    system_prompt = f"""你是一个严格的技术文档审核专家。你的任务是对技术条款进行审核分析。

【审核要求】
请从以下角度进行审核：{audit_types_str}

对于每个发现的问题，请提供：
- 合规性（compliance）：是否满足标准要求
- 完整性（completeness）：是否有遗漏要求
- 一致性（consistency）：前后是否矛盾

请使用 report_audit_issues 函数报告审核结果。"""

    # User prompt 是动态部分（条款内容）→ 每次不同，不影响缓存
    user_prompt = f"""【待审核条款】
编号: {clause.number}
内容: {clause.text}

{kb_content}

请审核此条款是否存在问题。如果没有问题，返回空 issues 数组。"""

    try:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        result = generate_with_tools(
            messages=messages,
            tools=[AUDIT_TOOL],
            tool_choice={"type": "function", "function": {"name": "report_audit_issues"}},
            timeout=120,
        )

        if result["type"] == "tool_calls":
            args = result["tool_calls"][0]["arguments"]
            issues = _tool_args_to_issues(args, clause, clause_index)
        else:
            # Fallback: model returned text
            parsed = _parse_audit_result(result.get("content", ""), clause, clause_index)
            issues.extend(parsed)

    except Exception as e:
        _logger.warning("audit clause failed (clause %s): %s", clause.number, e)

    return issues


def _tool_args_to_issues(
    data: dict,
    clause: Clause,
    clause_index: int,
) -> list[AuditIssue]:
    """将 Function Calling 的审核结果参数转换为 AuditIssue 列表。"""
    issues = []
    for i, issue_data in enumerate(data.get("issues", [])):
        issue = AuditIssue(
            id=clause_index * 100 + i + 1,
            type=issue_data.get("type", "compliance"),
            location=IssueLocation(
                chapter=None,
                clause_number=clause.number,
                original_text=clause.text[:200],
            ),
            description=issue_data.get("description", ""),
            severity=issue_data.get("severity", "medium"),
            suggestion=issue_data.get("suggestion"),
        )

        std_ref = issue_data.get("standard_reference")
        if std_ref:
            issue.standard_reference = StandardRef(
                standard_name=std_ref.get("standard_name", ""),
                standard_id=std_ref.get("standard_id", ""),
                clause=std_ref.get("clause"),
                requirement=std_ref.get("requirement"),
            )

        issues.append(issue)

    return issues


def _parse_audit_result(llm_output: str, clause: Clause, clause_index: int) -> list[AuditIssue]:
    """解析 LLM 审核结果（降级方案，无 function calling 时使用）。"""
    import re

    issues = []

    json_match = re.search(r'\[[\s\S]*\]|\{[\s\S]*\}', llm_output)
    if not json_match:
        return issues

    try:
        data = json.loads(json_match.group())
        if isinstance(data, dict):
            data_list = data.get("issues", [])
        elif isinstance(data, list):
            data_list = data
        else:
            data_list = []

        for i, issue_data in enumerate(data_list):
            issue_data_ensured = {
                "type": issue_data.get("type", "compliance"),
                "description": issue_data.get("description", ""),
                "severity": issue_data.get("severity", "medium"),
                "suggestion": issue_data.get("suggestion"),
                "standard_reference": issue_data.get("standard_reference"),
            }
            issues.append(_tool_args_issue_to_audit_issue(
                issue_data_ensured, clause, clause_index, i
            ))

    except json.JSONDecodeError:
        pass

    return issues


def _tool_args_issue_to_audit_issue(
    issue_data: dict,
    clause: Clause,
    clause_index: int,
    sub_index: int,
) -> AuditIssue:
    """将单个 issue dict 转换为 AuditIssue 对象。"""
    issue = AuditIssue(
        id=clause_index * 100 + sub_index + 1,
        type=issue_data.get("type", "compliance"),
        location=IssueLocation(
            clause_number=clause.number,
            original_text=clause.text[:200],
        ),
        description=issue_data.get("description", ""),
        severity=issue_data.get("severity", "medium"),
        suggestion=issue_data.get("suggestion"),
    )

    std_ref = issue_data.get("standard_reference")
    if std_ref:
        issue.standard_reference = StandardRef(
            standard_name=std_ref.get("standard_name", ""),
            standard_id=std_ref.get("standard_id", ""),
            clause=std_ref.get("clause"),
            requirement=std_ref.get("requirement"),
        )

    return issue


def analyze_document_clauses(
    doc: AuditDocument,
    kb_ids: list[str],
    audit_types: list[AuditType],
    progress_callback=None,
) -> tuple[list[AuditIssue], str]:
    """分析文档所有条款（逐个条款审核）。"""
    all_issues = []
    raw_analysis_parts = []

    if not doc.structure or not doc.structure.chapters:
        return all_issues, "未找到可审核的条款"

    total_clauses = sum(len(ch.clauses) for ch in doc.structure.chapters)
    processed = 0

    for chapter in doc.structure.chapters:
        for clause in chapter.clauses:
            issues = audit_clause(clause, kb_ids, audit_types, processed)

            if issues:
                for issue in issues:
                    issue.location.chapter = chapter.title
                all_issues.extend(issues)
                raw_analysis_parts.append(f"条款 {clause.number}: 发现 {len(issues)} 个问题")

            processed += 1
            if progress_callback:
                progress_callback(processed / total_clauses)

    raw_analysis = f"共审核 {total_clauses} 个条款，发现 {len(all_issues)} 个问题\n" + "\n".join(
        raw_analysis_parts[:10]
    )

    return all_issues, raw_analysis


def quick_audit_with_llm(
    doc: AuditDocument,
    kb_ids: list[str],
    audit_types: list[AuditType],
) -> tuple[list[AuditIssue], str]:
    """使用 LLM 快速审核整个文档（Function Calling 方式）。"""
    # 获取相关知识库内容
    kb_content = search_svc.get_kb_content_for_audit(kb_ids, doc.name)

    audit_types_str = "、".join({
        "compliance": "合规性（是否符合标准）",
        "completeness": "完整性（是否有遗漏）",
        "consistency": "一致性（前后是否矛盾）",
    }.get(t, t) for t in audit_types)

    # 构建条款列表
    clauses_text = []
    if doc.structure:
        for chapter in doc.structure.chapters:
            clauses_text.append(f"\n{chapter.title}:")
            for clause in chapter.clauses:
                clauses_text.append(f"  {clause.number}. {clause.text}")

    clauses_str = "\n".join(clauses_text) if clauses_text else doc.parsed_content[:3000]

    # System prompt（缓存友好）
    system_prompt = f"""你是一个严格的技术文档审核专家。请对以下技术文档进行全面的审核分析。

【审核要求】
请从以下角度进行全面审核：
1. {audit_types_str}

对于每个发现的问题，请使用 report_audit_issues 函数报告：
- 问题类型（compliance/completeness/consistency）
- 具体位置（条款编号）
- 问题描述
- 严重程度（high/medium/low）
- 参考标准
- 修改建议"""

    # User prompt（动态内容）
    user_prompt = f"""【待审核文档】
名称: {doc.name}
条款列表:
{clauses_str}

{kb_content}

请审核此文档并报告发现的问题。"""

    try:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        result = generate_with_tools(
            messages=messages,
            tools=[AUDIT_TOOL],
            tool_choice={"type": "function", "function": {"name": "report_audit_issues"}},
            timeout=180,
        )

        if result["type"] == "tool_calls":
            args = result["tool_calls"][0]["arguments"]
            issues = _tool_args_to_issues_flat(args, doc)
            return issues, json.dumps(args, ensure_ascii=False, indent=2)

        return [], f"LLM 未返回 tool call: {result.get('content', '')[:200]}"

    except Exception as e:
        return [], f"LLM 审核失败: {str(e)}"


def _tool_args_to_issues_flat(
    data: dict,
    doc: AuditDocument,
) -> list[AuditIssue]:
    """将 Function Calling 参数转换为 AuditIssue 列表（快速审核用，无 clause_index）。"""
    issues = []
    for i, issue_data in enumerate(data.get("issues", [])):
        issue = AuditIssue(
            id=i + 1,
            type=issue_data.get("type", "compliance"),
            location=IssueLocation(
                clause_number=issue_data.get("clause_number"),
                original_text=issue_data.get("original_text", "")[:200],
            ),
            description=issue_data.get("description", ""),
            severity=issue_data.get("severity", "medium"),
            suggestion=issue_data.get("suggestion"),
        )

        std_ref = issue_data.get("standard_reference")
        if std_ref:
            issue.standard_reference = StandardRef(
                standard_name=std_ref.get("standard_name", ""),
                standard_id=std_ref.get("standard_id", ""),
                clause=std_ref.get("clause"),
                requirement=std_ref.get("requirement"),
            )

        issues.append(issue)

    return issues


def _parse_quick_audit_result(llm_output: str, doc: AuditDocument) -> list[AuditIssue]:
    """解析快速审核结果（降级方案）。"""
    import re

    issues = []
    json_match = re.search(r'\{[\s\S]*"issues"[\s\S]*\}', llm_output)
    if not json_match:
        return issues

    try:
        data = json.loads(json_match.group())
        data_list = data.get("issues", [])

        for i, issue_data in enumerate(data_list):
            issue = AuditIssue(
                id=i + 1,
                type=issue_data.get("type", "compliance"),
                location=IssueLocation(
                    clause_number=issue_data.get("clause_number"),
                    original_text=issue_data.get("original_text", "")[:200],
                ),
                description=issue_data.get("description", ""),
                severity=issue_data.get("severity", "medium"),
                suggestion=issue_data.get("suggestion"),
            )

            std_ref = issue_data.get("standard_reference")
            if std_ref:
                issue.standard_reference = StandardRef(
                    standard_name=std_ref.get("standard_name", ""),
                    standard_id=std_ref.get("standard_id", ""),
                    clause=std_ref.get("clause"),
                    requirement=std_ref.get("requirement"),
                )

            issues.append(issue)

    except json.JSONDecodeError:
        pass

    return issues


# ── 按章批量审核 ─────────────────────────────────────────────────────────────

CHAPTER_AUDIT_TOOL = {
    "type": "function",
    "function": {
        "name": "report_chapter_audit_issues",
        "description": "报告对某一章节所有技术条款的综合审核结果",
        "parameters": {
            "type": "object",
            "properties": {
                "issues": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": ["compliance", "completeness", "consistency"],
                            },
                            "clause_number": {
                                "type": "string",
                                "description": "出问题的条款编号",
                            },
                            "description": {"type": "string"},
                            "severity": {
                                "type": "string",
                                "enum": ["high", "medium", "low"],
                            },
                            "standard_reference": {
                                "type": "object",
                                "properties": {
                                    "standard_name": {"type": "string"},
                                    "standard_id": {"type": "string"},
                                    "clause": {"type": "string"},
                                    "requirement": {"type": "string"},
                                },
                                "required": ["standard_name", "standard_id"],
                            },
                            "suggestion": {"type": "string"},
                        },
                        "required": ["type", "description", "severity"],
                    },
                },
            },
            "required": ["issues"],
        },
    },
}


def _build_chapter_from_clauses(clauses: list[Clause], markdown: str) -> str:
    """从原始 markdown 中按条款编号拼接章节上下文。"""
    parts = []
    for c in clauses:
        raw = _get_raw_context(c.number, markdown) if markdown else ""
        parts.append(f"---\n条款 {c.number}: {c.text}")
        if raw and len(raw) > len(c.text) + 20:
            parts.append(f"【原始内容】\n{raw[:500]}")
    return "\n".join(parts)


def _get_raw_context(clause_number: str, markdown: str) -> str:
    """从原始 Markdown 中提取指定条款的完整上下文。

    找到条款编号在 Markdown 中的位置，返回从该处到下一个同级标题之前的所有内容。
    包括表格 HTML、列表、段落等。
    """
    import re

    # 构造匹配模式：### 2.2.1. 或 2.2.1 或 2.2.1)
    patterns = [
        rf'^###\s*{re.escape(clause_number)}\.?\s',
        rf'^{re.escape(clause_number)}\)\s',
        rf'^{re.escape(clause_number)}\.\s',
        rf'^{re.escape(clause_number)}、',
    ]

    lines = markdown.split("\n")
    start_idx = -1

    for i, line in enumerate(lines):
        stripped = line.strip()
        for pat in patterns:
            if re.match(pat, stripped):
                start_idx = i
                break
        if start_idx >= 0:
            break

    if start_idx < 0:
        return ""

    # 找到下一个同级或上级标题
    end_idx = len(lines)
    for i in range(start_idx + 1, len(lines)):
        stripped = lines[i].strip()
        if re.match(r'^#{1,3}\s', stripped):
            end_idx = i
            break

    raw = "\n".join(lines[start_idx:end_idx]).strip()
    # 限制长度避免撑爆上下文
    if len(raw) > 3000:
        raw = raw[:3000] + "\n... [截断]"
    return raw


def audit_chapter(
    chapter_title: str,
    clauses: list[Clause],
    kb_ids: list[str],
    audit_types: list[AuditType],
    chapter_index: int,
    chapter_text: str = "",
    markdown: str = "",
) -> list[AuditIssue]:
    """审核一个章节，一次 LLM 调用。

    优先用 chapter_text（章节原文片段），fallback 到从 clauses + markdown
    拼接。KB 检索以章节标题为查询，命中更精准。
    """
    # 构建本章的待审核文本
    if chapter_text:
        chapter_body = chapter_text[:15000]  # 每章最多 15000 字，适配 128K context
    elif markdown:
        # 降级：从 markdown 中按 clause 编号取上下文
        chapter_body = _build_chapter_from_clauses(clauses, markdown)
    else:
        chapter_body = "\n".join(f"条款 {c.number}: {c.text}" for c in clauses)

    # 用章节标题检索 KB（更聚焦）
    kb_query = chapter_title if chapter_title and chapter_title != "前言" else chapter_body[:200]
    kb_content = search_svc.get_kb_content_for_audit(kb_ids, kb_query)

    audit_types_str = "、".join({
        "compliance": "合规性",
        "completeness": "完整性",
        "consistency": "一致性",
    }.get(t, t) for t in audit_types)

    system_prompt = f"""你是一个严格的技术文档审核专家。你的任务是对指定章节的所有技术条款进行批量审核分析。

【审核要求】
请从以下角度逐条审核：{audit_types_str}

对于每个发现问题的条款，请详细说明：
- 问题类型（compliance/completeness/consistency）
- 对应条款编号
- 问题描述
- 严重程度
- 参考标准依据
- 修改建议

请使用 report_chapter_audit_issues 函数逐一报告发现的每个问题。"""

    user_prompt = f"""【待审核章节】
编号: {chapter_index + 1}
标题: {chapter_title}

【本章原文】
{chapter_body}

{kb_content}

请逐条审核本章所有条款，报告发现的问题。没有问题的条款不需要报告。"""

    try:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        result = generate_with_tools(
            messages=messages,
            tools=[CHAPTER_AUDIT_TOOL],
            tool_choice={"type": "function", "function": {"name": "report_chapter_audit_issues"}},
            timeout=180,
        )

        if result["type"] == "tool_calls":
            args = result["tool_calls"][0]["arguments"]
            return _chapter_args_to_issues(args, chapter_title, chapter_index)

        return []

    except Exception as e:
        _logger.warning("chapter audit failed (%s): %s", chapter_title, e)
        return []


def _chapter_args_to_issues(
    data: dict,
    chapter_title: str,
    chapter_index: int,
) -> list[AuditIssue]:
    """将批量审核结果转换为 AuditIssue 列表。"""
    issues = []
    for i, issue_data in enumerate(data.get("issues", [])):
        issue = AuditIssue(
            id=chapter_index * 1000 + i + 1,
            type=issue_data.get("type", "compliance"),
            location=IssueLocation(
                chapter=chapter_title,
                clause_number=issue_data.get("clause_number"),
                original_text=issue_data.get("description", "")[:200],
            ),
            description=issue_data.get("description", ""),
            severity=issue_data.get("severity", "medium"),
            suggestion=issue_data.get("suggestion"),
        )

        std_ref = issue_data.get("standard_reference")
        if std_ref:
            issue.standard_reference = StandardRef(
                standard_name=std_ref.get("standard_name", ""),
                standard_id=std_ref.get("standard_id", ""),
                clause=std_ref.get("clause"),
                requirement=std_ref.get("requirement"),
            )

        issues.append(issue)

    return issues


def analyze_document_by_chapter(
    doc: AuditDocument,
    kb_ids: list[str],
    audit_types: list[AuditType],
    progress_callback=None,
) -> tuple[list[AuditIssue], str]:
    """按章节批量审核文档所有章节。

    每个章节一次 LLM 调用，大幅减少调用次数。
    """
    all_issues = []
    raw_parts = []

    if not doc.structure or not doc.structure.chapters:
        return all_issues, "未找到可审核的章节"

    total = len(doc.structure.chapters)

    for idx, chapter in enumerate(doc.structure.chapters):
        if not chapter.clauses:
            raw_parts.append(f"{chapter.title}: 无条款，跳过")
            if progress_callback:
                progress_callback((idx + 1) / total)
            continue

        issues = audit_chapter(
            chapter_title=chapter.title,
            clauses=chapter.clauses,
            kb_ids=kb_ids,
            audit_types=audit_types,
            chapter_index=idx,
            markdown=doc.parsed_content or "",
        )

        if issues:
            all_issues.extend(issues)
            raw_parts.append(f"{chapter.title}: 发现 {len(issues)} 个问题")

        if progress_callback:
            progress_callback((idx + 1) / total)

    raw = f"共审核 {total} 个章节，发现 {len(all_issues)} 个问题\n" + "\n".join(raw_parts[:20])
    return all_issues, raw
