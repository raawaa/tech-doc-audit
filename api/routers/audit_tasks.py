from typing import Optional
from pydantic import BaseModel

from fastapi import APIRouter, HTTPException

import services.audit_task_service as audit_task_svc
import services.audit_analysis_service as analysis_svc
import storage.audit_doc_repo as doc_repo

router = APIRouter(prefix="/api/v1/audit-tasks", tags=["audit-tasks"])


class CreateTaskRequest(BaseModel):
    document_id: str
    kb_ids: list[str]
    audit_types: list[str] = ["compliance", "completeness", "consistency"]
    async_mode: bool = True


class TaskResponse(BaseModel):
    id: str
    document_id: str
    document_name: str
    status: str
    progress: float
    created_at: str

    @classmethod
    def from_task(cls, task):
        return cls(
            id=task.id,
            document_id=task.document_id,
            document_name=task.document_name,
            status=task.status,
            progress=task.progress,
            created_at=str(task.created_at),
        )


class IssueResponse(BaseModel):
    id: int
    type: str
    clause_number: str | None
    description: str
    severity: str
    standard_name: str | None
    standard_clause: str | None
    suggestion: str | None


class ResultResponse(BaseModel):
    task_id: str
    document_id: str
    document_name: str
    summary: dict
    issues: list[IssueResponse]
    generated_at: str


@router.get("", response_model=list[TaskResponse])
def list_audit_tasks(document_id: Optional[str] = None):
    """获取审核任务列表。"""
    tasks = audit_task_svc.list_tasks(document_id)
    return [TaskResponse.from_task(t) for t in tasks]


@router.post("", response_model=TaskResponse)
def create_audit_task(req: CreateTaskRequest):
    """创建审核任务。"""
    # 验证文档存在
    doc = doc_repo.get_doc(req.document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")

    # 验证知识库存在
    import storage.kb_repo as kb_repo
    for kb_id in req.kb_ids:
        kb = kb_repo.get(kb_id)
        if not kb:
            raise HTTPException(status_code=404, detail=f"知识库不存在: {kb_id}")

    try:
        task = audit_task_svc.create_task(
            document_id=req.document_id,
            kb_ids=req.kb_ids,
            audit_types=req.audit_types,
        )

        # 如果非异步模式，直接执行
        if not req.async_mode:
            task = audit_task_svc.run_audit(task.id)

        return TaskResponse.from_task(task)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{task_id}", response_model=TaskResponse)
def get_audit_task(task_id: str):
    """获取审核任务详情。"""
    task = audit_task_svc.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return TaskResponse.from_task(task)


@router.delete("/{task_id}")
def cancel_audit_task(task_id: str):
    """取消审核任务。"""
    success = audit_task_svc.cancel_task(task_id)
    if not success:
        raise HTTPException(status_code=400, detail="无法取消任务")
    return {"message": "任务已取消"}


@router.get("/{task_id}/result")
def get_audit_result(task_id: str):
    """获取审核结果。"""
    task = audit_task_svc.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    if task.status != "completed":
        raise HTTPException(status_code=400, detail=f"任务未完成，当前状态: {task.status}")

    result = task.result
    if not result:
        raise HTTPException(status_code=404, detail="结果不存在")

    issues = [
        IssueResponse(
            id=issue.id,
            type=issue.type,
            clause_number=issue.location.clause_number,
            description=issue.description,
            severity=issue.severity,
            standard_name=issue.standard_reference.standard_name if issue.standard_reference else None,
            standard_clause=issue.standard_reference.clause if issue.standard_reference else None,
            suggestion=issue.suggestion,
        )
        for issue in result.issues
    ]

    return ResultResponse(
        task_id=result.task_id,
        document_id=result.document_id,
        document_name=result.document_name,
        summary={
            "total_clauses": result.summary.total_clauses,
            "issues_count": result.summary.issues_count,
            "compliance_issues": result.summary.compliance_issues,
            "completeness_issues": result.summary.completeness_issues,
            "consistency_issues": result.summary.consistency_issues,
            "high_severity": result.summary.high_severity,
            "medium_severity": result.summary.medium_severity,
            "low_severity": result.summary.low_severity,
        },
        issues=issues,
        generated_at=str(result.generated_at),
    )


@router.post("/{task_id}/run")
def run_audit_task(task_id: str, async_mode: bool = True):
    """手动触发审核执行。"""
    task = audit_task_svc.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    if async_mode:
        audit_task_svc.run_audit_async(task_id)
        return {"message": "审核任务已启动", "task_id": task_id}
    else:
        task = audit_task_svc.run_audit(task_id)
        return TaskResponse.from_task(task)
