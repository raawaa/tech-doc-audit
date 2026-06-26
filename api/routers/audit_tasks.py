from typing import Optional
from pydantic import BaseModel

import asyncio
import json
import queue
import threading

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

import services.audit_task_service as audit_task_svc
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
    progress_label: Optional[str] = None
    created_at: str

    @classmethod
    def from_task(cls, task):
        return cls(
            id=task.id,
            document_id=task.document_id,
            document_name=task.document_name,
            status=task.status,
            progress=task.progress,
            progress_label=task.progress_label,
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
    # 补齐 ——
    cited_excerpt: str | None = None
    document_position: str | None = None
    # PDF 跳转溯源 ——
    standard_doc_id: str | None = None
    standard_page_number: int | None = None
    standard_chunk_text: str | None = None
    standard_file_type: str | None = None


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

    issues = []
    for issue in result.issues:
        std_ref = issue.standard_reference
        # 根据 doc_id 查询 file_type
        file_type = None
        if std_ref and std_ref.doc_id:
            import storage.doc_repo as _doc_repo
            doc = _doc_repo.find_doc_by_id(std_ref.doc_id)
            if doc:
                file_type = doc.file_type

        issues.append(IssueResponse(
            id=issue.id,
            type=issue.type,
            clause_number=issue.location.clause_number,
            description=issue.description,
            severity=issue.severity,
            standard_name=std_ref.standard_name if std_ref else None,
            standard_clause=std_ref.clause if std_ref else None,
            suggestion=issue.suggestion,
            cited_excerpt=issue.cited_excerpt or None,
            document_position=issue.document_position or None,
            standard_doc_id=std_ref.doc_id if std_ref else None,
            standard_page_number=std_ref.page_number if std_ref else None,
            standard_chunk_text=std_ref.chunk_text if std_ref else None,
            standard_file_type=file_type,
        ))

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


@router.get("/{task_id}/stream")
async def stream_audit_progress(task_id: str):
    """流式返回 Agentic 审核的实时进度（SSE）。

    - pending: 启动审核，通过 event_callback 推送详细事件
    - processing: 任务已在执行，轮询等待完成
    - completed/failed: 直接返回结果
    """
    task = audit_task_svc.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    event_queue: queue.Queue = queue.Queue()

    if task.status == "pending":
        # 等待一小段时间，让 /run 触发的线程有机会将状态转为 processing，
        # 避免重复 spawn 线程导致竞态报错。
        import time as _time
        _time.sleep(0.3)
        task = audit_task_svc.get_task(task_id)

    if task and task.status == "pending":
        def run_with_stream():
            try:
                audit_task_svc.run_audit(task_id, event_callback=event_queue.put)
            except Exception as e:
                event_queue.put({"type": "error", "message": str(e)})
            finally:
                from services.agentic_audit import clear_task_events
                clear_task_events(task_id)
                event_queue.put(None)

        thread = threading.Thread(target=run_with_stream, daemon=True)
        thread.start()

    elif task and task.status == "processing":
        # 任务已在执行，从共享事件日志读取新事件
        import time
        from services.agentic_audit import get_task_events_since, clear_task_events

        def read_from_log():
            event_index = 0
            last_check = time.time()
            for _ in range(600):  # 300s 超时，为 LLM 提取和 KB 搜索留足时间
                new_events, event_index = get_task_events_since(task_id, event_index)
                for evt in new_events:
                    event_queue.put(evt)

                t = audit_task_svc.get_task(task_id)
                if not t:
                    event_queue.put({"type": "error", "message": "任务丢失"})
                    event_queue.put(None)
                    return
                if t.status == "completed":
                    result = t.result
                    event_queue.put({
                        "type": "complete",
                        "summary": result.raw_analysis if result else "审核完成",
                        "issues_count": result.summary.issues_count if result else 0,
                    })
                    event_queue.put(None)
                    clear_task_events(task_id)
                    return
                if t.status == "failed" or t.status == "cancelled":
                    event_queue.put({
                        "type": "cancelled" if t.status == "cancelled" else "error",
                        "message": t.error_message or "审核失败" if t.status == "failed" else "审核任务已被取消",
                    })
                    event_queue.put(None)
                    clear_task_events(task_id)
                    return

                # 每 5 秒无新事件发一次心跳
                if not new_events and time.time() - last_check > 5:
                    event_queue.put({"type": "progress", "message": t.progress_label or "审核进行中"})
                    last_check = time.time()

                time.sleep(0.5)
            event_queue.put({"type": "error", "message": "审核超时"})
            event_queue.put(None)

        thread = threading.Thread(target=read_from_log, daemon=True)
        thread.start()

    else:
        result = task.result
        event_queue.put({
            "type": "complete",
            "summary": result.raw_analysis if result else "审核完成",
            "issues_count": result.summary.issues_count if result else 0,
        })
        event_queue.put(None)

    async def event_generator():
        while True:
            try:
                event = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: event_queue.get(timeout=120)
                )
            except queue.Empty:
                yield f"data: {json.dumps({'type': 'error', 'message': '审核超时'}, ensure_ascii=False)}\n\n"
                break

            if event is None:
                break

            # 截断过长的 tool_result 内容（仅前端显示，不影响 LLM）
            if isinstance(event, dict) and event.get("type") == "tool_result":
                content = event.get("content", "")
                if len(content) > 10000:
                    event = {**event, "content": content[:10000] + "\n... [截断]", "truncated": True}

            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
