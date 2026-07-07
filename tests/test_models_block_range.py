"""V8-S1: block_range 数据模型测试。

三组测试:
1. 旧 audit 结果 JSON（缺 ``block_range`` 字段）→ ``AuditResult.model_validate`` 不抛异常，
   ``standard_reference.block_range is None``。
2. ``StandardRef.block_range`` 字段存在，默认 ``None``，显式 tuple 保留。
3. ``IssueResponse.standard_block_range`` 从 ``issue.standard_reference.block_range`` 拷贝；
   ``standard_reference=None`` 时返回 ``None``（向后兼容旧 issue）。

不依赖 GPU / bge-m3 / 真实 LLM：纯 Pydantic schema + FastAPI 路由组装。
``AUDIT_DATA_DIR`` 由 conftest 模块级 fixture 隔离，不污染生产数据。
"""

from datetime import datetime

from models.audit_task import (
    AuditIssue,
    AuditResult,
    IssueLocation,
    ResultSummary,
    StandardRef,
)
from api.routers.audit_tasks import IssueResponse


def _make_issue(standard_reference):
    return AuditIssue(
        id=1,
        type="compliance",
        location=IssueLocation(original_text="原文片段"),
        description="测试问题",
        severity="medium",
        standard_reference=standard_reference,
    )


# ── 1. 旧 JSON 反序列化兼容 ──────────────────────────────────────────────────


def test_legacy_audit_result_json_without_block_range_validates():
    """模拟 pre-V8 旧 audit 结果 JSON（不含任何 ``block_range`` 键）→ 不抛 ValidationError。

    ``Optional[tuple[int, int]] = None`` 默认值应让 ``StandardRef`` / ``AuditIssue`` /
    ``AuditResult`` 三层都容忍字段缺失。
    """
    legacy_dict = {
        "task_id": "task_legacy",
        "document_id": "doc_legacy",
        "document_name": "legacy.pdf",
        "summary": {
            "total_clauses": 1,
            "issues_count": 1,
            "compliance_issues": 1,
            "completeness_issues": 0,
            "consistency_issues": 0,
            "high_severity": 0,
            "medium_severity": 1,
            "low_severity": 0,
        },
        "issues": [
            {
                "id": 1,
                "type": "compliance",
                "location": {"original_text": "旧 chunk 文本"},
                "description": "旧 issue 描述",
                "severity": "medium",
                "standard_reference": {
                    "standard_name": "GB/T 22239-2019",
                    "standard_id": "GB/T 22239-2019",
                    "clause": "8.1.2",
                    "doc_id": "doc_x",
                    "page_number": 5,
                    "chunk_text": "旧 chunk 文本片段",
                    # 注意：没有 "block_range" 键
                },
            }
        ],
        "generated_at": "2026-01-01T00:00:00",
    }

    # 不应抛 ValidationError
    result = AuditResult.model_validate(legacy_dict)

    assert result.issues[0].standard_reference is not None
    assert result.issues[0].standard_reference.block_range is None
    assert result.issues[0].standard_reference.chunk_text == "旧 chunk 文本片段"


def test_legacy_issue_with_no_standard_reference_validates():
    """旧 issue 可能 ``standard_reference`` 整体缺失 → 也应通过校验。"""
    legacy_dict = {
        "task_id": "task_legacy2",
        "document_id": "doc_legacy2",
        "document_name": "legacy2.pdf",
        "summary": {
            "total_clauses": 0,
            "issues_count": 1,
            "compliance_issues": 0,
            "completeness_issues": 0,
            "consistency_issues": 0,
            "high_severity": 0,
            "medium_severity": 0,
            "low_severity": 1,
        },
        "issues": [
            {
                "id": 1,
                "type": "insufficient_evidence",
                "location": {"original_text": "无引用"},
                "description": "无可引用标准",
                "severity": "low",
                # 没有 standard_reference
            }
        ],
        "generated_at": "2026-01-01T00:00:00",
    }
    result = AuditResult.model_validate(legacy_dict)
    assert result.issues[0].standard_reference is None


# ── 2. StandardRef 字段存在 + 默认值 ─────────────────────────────────────────


def test_standard_ref_block_range_default_is_none():
    """``StandardRef`` 不传 ``block_range`` → 默认 ``None``。"""
    ref = StandardRef(
        standard_name="GB/T 22239-2019",
        standard_id="GB/T 22239-2019",
    )
    assert hasattr(ref, "block_range"), "StandardRef 应声明 block_range 字段"
    assert ref.block_range is None


def test_standard_ref_block_range_explicit_tuple_preserved():
    """``StandardRef`` 显式传入 ``block_range=(start, end)`` → 原样保留。"""
    ref = StandardRef(
        standard_name="GB/T 22239-2019",
        standard_id="GB/T 22239-2019",
        block_range=(3, 7),
    )
    assert ref.block_range == (3, 7)


# ── 3. IssueResponse.standard_block_range 暴露 ────────────────────────────────


def test_issue_response_copies_standard_block_range_from_issue():
    """``IssueResponse.standard_block_range`` 从 ``issue.standard_reference.block_range`` 拷贝。"""
    std_ref = StandardRef(
        standard_name="GB/T 22239-2019",
        standard_id="GB/T 22239-2019",
        block_range=(3, 7),
    )
    issue = _make_issue(std_ref)

    # IssueResponse 在 api/routers/audit_tasks.py 里靠手工字段拷贝组装（不是 from_issue 工厂），
    # 这里 mirror 该组装路径：直接构造并验证字段存在/值正确。
    std_ref2 = issue.standard_reference
    resp = IssueResponse(
        id=issue.id,
        type=issue.type,
        clause_number=issue.location.clause_number,
        description=issue.description,
        severity=issue.severity,
        standard_name=std_ref2.standard_name,
        standard_clause=std_ref2.clause,
        suggestion=issue.suggestion,
        standard_block_range=std_ref2.block_range,
    )
    assert hasattr(resp, "standard_block_range"), (
        "IssueResponse 应声明 standard_block_range 字段（API 返回 null when 旧 issue）"
    )
    assert resp.standard_block_range == (3, 7)


def test_issue_response_standard_block_range_none_when_no_standard_reference():
    """``issue.standard_reference=None``（旧 issue 兜底）→ ``standard_block_range`` 为 ``None``。"""
    issue = _make_issue(standard_reference=None)

    std_ref = issue.standard_reference
    resp = IssueResponse(
        id=issue.id,
        type=issue.type,
        clause_number=issue.location.clause_number,
        description=issue.description,
        severity=issue.severity,
        standard_name=std_ref.standard_name if std_ref else None,
        standard_clause=std_ref.clause if std_ref else None,
        suggestion=issue.suggestion,
        standard_block_range=std_ref.block_range if std_ref else None,
    )
    assert resp.standard_block_range is None


def test_issue_response_round_trip_with_block_range_serializes_field():
    """``IssueResponse`` 经 ``model_dump_json`` / ``model_validate`` 往返后 ``standard_block_range`` 仍存在。

    验证 API 暴露给前端的 JSON shape 不丢字段（前端读取时不需要 try/catch）。
    """
    std_ref = StandardRef(
        standard_name="GB/T 22239-2019",
        standard_id="GB/T 22239-2019",
        block_range=(12, 18),
    )
    issue = _make_issue(std_ref)
    std_ref2 = issue.standard_reference

    resp = IssueResponse(
        id=issue.id,
        type=issue.type,
        clause_number=issue.location.clause_number,
        description=issue.description,
        severity=issue.severity,
        standard_name=std_ref2.standard_name,
        standard_clause=std_ref2.clause,
        suggestion=issue.suggestion,
        standard_block_range=std_ref2.block_range,
    )

    dumped = resp.model_dump()
    assert "standard_block_range" in dumped
    assert dumped["standard_block_range"] == (12, 18)

    # 序列化 → JSON → 反序列化（模拟前端拿到的 JSON）
    json_str = resp.model_dump_json()
    resp2 = IssueResponse.model_validate_json(json_str)
    assert resp2.standard_block_range == (12, 18)