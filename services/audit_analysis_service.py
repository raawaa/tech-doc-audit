import json
import os
import httpx
from typing import Optional

from models.audit_task import AuditIssue, AuditType, IssueLocation, StandardRef
from models.audit_document import AuditDocument, Clause
import services.search_service as search_svc


OLLAMA_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
MODEL = os.environ.get("OLLAMA_MODEL", "qwen3.5:0.8b")


def audit_clause(
    clause: Clause,
    kb_ids: list[str],
    audit_types: list[AuditType],
    clause_index: int,
) -> list[AuditIssue]:
    """审核单个条款。"""
    issues = []

    # 获取相关知识库内容
    kb_content = search_svc.get_kb_content_for_audit(kb_ids, clause.text)

    # 构建审核 prompt
    audit_types_str = "、".join({
        "compliance": "合规性",
        "completeness": "完整性",
        "consistency": "一致性",
    }.get(t, t) for t in audit_types)

    prompt = f"""你是一个技术文档审核专家。请对以下技术条款进行审核分析。

【待审核条款】
编号: {clause.number}
内容: {clause.text}

{kb_content}

【审核要求】
请从以下角度进行审核：{audit_types_str}

请输出 JSON 格式的审核结果：
{{
  "issues": [
    {{
      "id": 1,
      "type": "compliance|completeness|consistency",
      "description": "问题描述",
      "severity": "high|medium|low",
      "standard_reference": {{
        "standard_name": "标准名称",
        "standard_id": "标准编号",
        "clause": "条款编号",
        "requirement": "标准要求"
      }},
      "suggestion": "修改建议"
    }}
  ]
}}

如果没有发现问题，返回空的 issues 数组。
请直接输出 JSON，不要包含其他内容。"""

    try:
        response = httpx.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": MODEL,
                "prompt": prompt,
                "stream": False,
            },
            timeout=120,
        )
        response.raise_for_status()
        data = response.json()
        llm_output = data.get("response", "")

        # 解析 LLM 输出
        parsed_issues = _parse_audit_result(llm_output, clause, clause_index)
        issues.extend(parsed_issues)

    except Exception as e:
        # LLM 调用失败时记录但不中断流程
        pass

    return issues


def _parse_audit_result(llm_output: str, clause: Clause, clause_index: int) -> list[AuditIssue]:
    """解析 LLM 审核结果。"""
    import re

    issues = []

    # 提取 JSON
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
            issue = AuditIssue(
                id=clause_index * 100 + i + 1,  # 唯一 ID
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

            issues.append(issue)

    except json.JSONDecodeError:
        pass

    return issues


def analyze_document_clauses(
    doc: AuditDocument,
    kb_ids: list[str],
    audit_types: list[AuditType],
    progress_callback=None,
) -> tuple[list[AuditIssue], str]:
    """分析文档所有条款。"""
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
                # 添加章节信息到位置
                for issue in issues:
                    issue.location.chapter = chapter.title

                all_issues.extend(issues)
                raw_analysis_parts.append(f"条款 {clause.number}: 发现 {len(issues)} 个问题")

            processed += 1

            if progress_callback:
                progress_callback(processed / total_clauses)

    raw_analysis = f"共审核 {total_clauses} 个条款，发现 {len(all_issues)} 个问题\n" + "\n".join(
        raw_analysis_parts[:10]
    )  # 只保留前 10 条

    return all_issues, raw_analysis


def quick_audit_with_llm(
    doc: AuditDocument,
    kb_ids: list[str],
    audit_types: list[AuditType],
) -> tuple[list[AuditIssue], str]:
    """使用 LLM 快速审核整个文档（一次性分析所有条款）。"""
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

    prompt = f"""你是一个严格的技术文档审核专家。请对以下技术文档进行全面的审核分析。

【待审核文档】
名称: {doc.name}
条款列表:
{clauses_str}

{kb_content}

【审核要求】
请从以下角度进行全面审核：
1. {audit_types_str}

对于每个发现的问题，请输出：
- 问题类型（compliance/completeness/consistency）
- 具体位置（条款编号）
- 问题描述
- 严重程度（high/medium/low）
- 参考标准
- 修改建议

输出格式（JSON）：
{{
  "issues": [
    {{
      "id": 1,
      "type": "compliance",
      "clause_number": "2.1.1",
      "description": "...",
      "severity": "high",
      "standard_reference": {{"standard_name": "...", "clause": "...", "requirement": "..."}},
      "suggestion": "..."
    }}
  ],
  "summary": {{
    "total_issues": 5,
    "compliance": 3,
    "completeness": 1,
    "consistency": 1
  }}
}}

如果没有发现问题，返回空的 issues 数组。
请直接输出 JSON。"""

    try:
        response = httpx.post(
            f"{OLLAMA_URL}/api/generate",
            json={{
                "model": MODEL,
                "prompt": prompt,
                "stream": False,
            }},
            timeout=180,
        )
        response.raise_for_status()
        data = response.json()
        llm_output = data.get("response", "")

        # 解析结果
        issues = _parse_quick_audit_result(llm_output, doc)
        return issues, llm_output

    except Exception as e:
        return [], f"LLM 审核失败: {str(e)}"


def _parse_quick_audit_result(llm_output: str, doc: AuditDocument) -> list[AuditIssue]:
    """解析快速审核结果。"""
    import re

    issues = []

    # 提取 JSON
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
