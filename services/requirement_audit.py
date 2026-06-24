"""需求锚定审核管线（v2）。

不做原子需求提取。审核时将待审核文档按结构分块后，
对每块搜索 FAISS 定位最相关的标准条款原文，
由 LLM 直接判断：文档所述是否符合标准要求。

流程：
1. 待审核文档 → chunk_by_structure() 分块
2. 每块 → vec_search(kb_ids, chunk.text, top_k=3) → 搜索标准条款
3. 每对(文档块, 标准条款) → LLM prompt → 判断 verdict
4. 汇总 AuditResult

对比 v1（需求提取方案）：不再需要 `requirement_extractor` 预处理，
不再需要 `load_requirements()`，索引即知识。
"""

import concurrent.futures
import itertools
import os
from typing import Optional

from llama_index.core.llms import ChatMessage, MessageRole
from llama_index.core.prompts import ChatPromptTemplate

from core.logger import get_logger
from core.settings import get_llm
from core.degradation import record as _deg_record
from models.audit_task import (
    AuditIssue, AuditResult, IssueLocation,
    ResultSummary, StandardRef,
)
from models.audit_document import DocumentStructure
from services.chunking import chunk_by_structure
from services.vector_search import vec_search

_logger = get_logger(__name__)

# ── LLM Prompt ─────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """你是一个严格的技术文档审核专家。你的任务是将标准/制度文件中的具体要求与待审核文档中的对应内容进行比对，判断文档是否满足标准要求。

请输出以下 verdict 之一：
- compliant: 文档内容满足标准要求
- deviation: 文档内容偏离了标准要求（部分不符或完全不符）
- insufficient: 文档内容与该要求的对应关系不确定，证据不足
- not_applicable: 该要求不适用于这段文档内容（请说明原因）

每个判断必须包含：
- verdict: 判断结论
- reason: 判断理由（50字以内）
- confidence: 置信度（0.0-1.0）
- cited_excerpt: 从文档段落中引用的具体文本片段作为证据（如果有）
- document_position: 引用文本在文档中的位置描述（如果有）

没有问题的段落不要强行找问题。"""


_prompt = ChatPromptTemplate(
    message_templates=[
        ChatMessage(role=MessageRole.SYSTEM, content=SYSTEM_PROMPT),
        ChatMessage(
            role=MessageRole.USER,
            content="""【标准条款】
标准名称：{standard_name}
条款编号：{clause_number}
原文：{standard_text}

【待审核文档段落】
位置：{doc_position}
原文：{doc_text}

请判断：该文档段落是否满足上述标准要求？""",
        ),
    ]
)


# ── 主入口 ─────────────────────────────────────────────────────────────────────


def run_requirement_audit(
    task_id: str,
    doc_id: str,
    document_name: str,
    parsed_content: str,
    structure: Optional[DocumentStructure],
    kb_ids: list[str],
) -> AuditResult:
    """需求锚定审核：分块 → 搜索标准 → LLM 比对。

    不依赖于预提取的原子需求。
    """
    # 1. 文档分块
    chunks = chunk_by_structure(parsed_content, structure, doc_id=doc_id)
    if not chunks:
        _logger.warning("No chunks from document %s", doc_id)
        return _empty_result(task_id, doc_id, document_name, kb_ids)

    _logger.info("Document chunked into %d segments", len(chunks))

    # 2. 对每块搜索标准
    pairs = _search_pairs(chunks, kb_ids)
    if not pairs:
        _logger.info("No matching standard clauses found for document")
        return _empty_result(task_id, doc_id, document_name, kb_ids)

    _logger.info("Found %d (chunk, clause) match pairs", len(pairs))

    # 3. 逐对 LLM 判断
    issues = _judge_pairs(pairs, task_id)

    # 4. 汇总结果
    return _build_result(task_id, doc_id, document_name, issues)


# ── 搜索匹配 ───────────────────────────────────────────────────────────────────


def _search_pairs(
    chunks: list,
    kb_ids: list[str],
    top_k: int = 3,
) -> list[dict]:
    """对每块搜索 FAISS，找出最相关的标准条款。

    Returns:
        [{chunk, clause_text, clause_number, standard_name, kb_id}, ...]
    """
    pairs = []
    seen = set()

    for chunk in chunks:
        if not chunk.text or len(chunk.text.strip()) < 20:
            continue

        results = vec_search(kb_ids, chunk.text, top_k=top_k)
        if not results:
            continue

        for r in results:
            key = (r.get("doc_id", ""), r.get("clause_number", ""))
            if key in seen or not key[1]:
                continue
            seen.add(key)
            pairs.append({
                "chunk": chunk,
                "clause_text": r.get("content", ""),
                "clause_number": r.get("clause_number", ""),
                "standard_name": r.get("doc_source", ""),
                "kb_id": r.get("kb_id", ""),
                "section_path": r.get("section_path", ""),
                "relevance": r.get("relevance", 0),
            })

    return pairs


# ── LLM 判断 ───────────────────────────────────────────────────────────────────


def _judge_pairs(
    pairs: list[dict],
    task_id: str,
) -> list[AuditIssue]:
    """对所有(文档段, 标准条款)对做 LLM 判断。"""
    issues: list[AuditIssue] = []
    llm = get_llm()

    for idx, pair in enumerate(pairs):
        chunk = pair["chunk"]
        try:
            messages = _prompt.format_messages(
                standard_name=pair["standard_name"],
                clause_number=pair["clause_number"],
                standard_text=pair["clause_text"][:2000],
                doc_position=chunk.section_path,
                doc_text=chunk.text[:2000],
            )

            response = llm.chat(messages)
            verdict = _parse_verdict(response.message.content or "", chunk)
        except Exception as e:
            _logger.warning("Failed to judge pair %d: %s", idx, e)
            verdict = {
                "verdict": "insufficient",
                "reason": "LLM 调用失败",
                "confidence": 0.0,
                "cited_excerpt": "",
                "document_position": chunk.section_path,
            }

        if verdict["verdict"] == "deviation":
            issues.append(_make_issue(
                idx, pair, verdict, task_id,
                issue_type="compliance",
            ))
        elif verdict["verdict"] == "insufficient":
            issues.append(_make_issue(
                idx, pair, verdict, task_id,
                issue_type="insufficient_evidence",
            ))

    return issues


def _parse_verdict(text: str, chunk) -> dict:
    """从 LLM 回复中解析 verdict。"""
    text_lower = text.lower()
    for keyword, result in [
        ("deviation", "deviation"),
        ("偏离", "deviation"),
        ("不符", "deviation"),
        ("not_applicable", "not_applicable"),
        ("不适", "not_applicable"),
        ("不适用", "not_applicable"),
        ("insufficient", "insufficient"),
        ("不足", "insufficient"),
        ("不确定", "insufficient"),
        ("compliant", "compliant"),
        ("合规", "compliant"),
        ("满足", "compliant"),
    ]:
        if keyword in text_lower:
            return {
                "verdict": result,
                "reason": text[:150],
                "confidence": 0.7 if result in ("compliant", "deviation") else 0.4,
                "cited_excerpt": "",
                "document_position": chunk.section_path,
            }
    return {"verdict": "compliant", "reason": text[:150], "confidence": 0.5,
            "cited_excerpt": "", "document_position": chunk.section_path}


def _make_issue(
    idx: int,
    pair: dict,
    verdict: dict,
    task_id: str,
    issue_type: str,
) -> AuditIssue:
    """构造 AuditIssue。"""
    chunk = pair["chunk"]
    return AuditIssue(
        id=idx + 1,
        type=issue_type,
        location=IssueLocation(
            clause_number=pair["clause_number"],
            original_text=chunk.text[:200],
        ),
        description=(
            f"标准「{pair['standard_name']}」第{pair['clause_number']}条要求："
            f"{verdict.get('reason', '')}"
        ),
        severity="medium",
        standard_reference=StandardRef(
            standard_name=pair["standard_name"],
            standard_id=pair["standard_name"],
            clause=pair["clause_number"],
            requirement=pair["clause_text"][:200],
        ),
        suggestion=f"建议按 {pair['standard_name']} 第{pair['clause_number']}条的要求调整",
        cited_excerpt=verdict.get("cited_excerpt", ""),
        document_position=verdict.get("document_position", chunk.section_path),
    )


def _build_result(
    task_id: str,
    doc_id: str,
    document_name: str,
    issues: list[AuditIssue],
) -> AuditResult:
    """构造 AuditResult。"""
    return AuditResult(
        task_id=task_id,
        document_id=doc_id,
        document_name=document_name,
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
        raw_analysis=f"需求锚定审核完成: 搜索到 {len(issues)} 个相关标准条款，发现 {len(issues)} 个问题",
    )


def _empty_result(
    task_id: str,
    doc_id: str,
    document_name: str,
    kb_ids: list[str],
) -> AuditResult:
    """无可匹配标准时的空结果。"""
    return AuditResult(
        task_id=task_id,
        document_id=doc_id,
        document_name=document_name,
        summary=ResultSummary(total_clauses=0, issues_count=0),
        issues=[],
        raw_analysis=f"在知识库 {kb_ids} 中未找到与文档匹配的标准条款",
    )
