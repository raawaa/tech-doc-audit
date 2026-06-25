import threading
from datetime import datetime
from typing import Callable, Optional

from core.logger import get_logger
from models.audit_task import AuditTask, AuditResult, ResultSummary, AuditType
import storage.audit_task_repo as repo
import storage.audit_doc_repo as doc_repo
import services.audit_doc_service as audit_doc_svc
import services.structure_service as structure_svc

_logger = get_logger(__name__)


def create_task(
    document_id: str,
    kb_ids: list[str],
    audit_types: list[AuditType] = None,
) -> AuditTask:
    """创建审核任务。"""
    # 获取文档信息
    doc = doc_repo.get_doc(document_id)
    if not doc:
        raise ValueError(f"文档不存在: {document_id}")

    if audit_types is None:
        audit_types = ["compliance", "completeness", "consistency"]

    task = AuditTask(
        document_id=document_id,
        document_name=doc.name,
        kb_ids=kb_ids,
        audit_types=audit_types,
        status="pending",
    )

    return repo.save_task(task)


def get_task(task_id: str) -> Optional[AuditTask]:
    """获取审核任务。"""
    return repo.get_task(task_id)


def list_tasks(document_id: Optional[str] = None) -> list[AuditTask]:
    """列出审核任务。"""
    return repo.list_tasks(document_id)


def cancel_task(task_id: str) -> bool:
    """取消审核任务。"""
    task = repo.get_task(task_id)
    if not task:
        return False

    if task.status == "completed" or task.status == "failed":
        return False

    task.status = "cancelled"
    repo.save_task(task)
    return True


def get_result(task_id: str) -> Optional[AuditResult]:
    """获取审核结果。"""
    task = repo.get_task(task_id)
    if not task:
        return None
    return task.result


def _deduplicate_issues(issues: list) -> list:
    """合并描述同一根因的重复问题。

    Agent 可能在不同轮次对同一段文档报出相同问题。
    按 cited_excerpt 相似度分组，每组保留 severity 最高的问题。
    """
    if len(issues) <= 1:
        return issues

    # 按 cited_excerpt 前 80 字符分组
    groups: dict[str, list] = {}
    for issue in issues:
        excerpt = (issue.cited_excerpt or issue.description or "")[:80]
        if excerpt not in groups:
            groups[excerpt] = []
        groups[excerpt].append(issue)

    severity_rank = {"high": 3, "medium": 2, "low": 1}
    deduped: list = []

    for group in groups.values():
        if len(group) == 1:
            deduped.append(group[0])
            continue

        # 取 severity 最高的
        best = max(group, key=lambda i: severity_rank.get(i.severity, 0))
        # 合并 suggestion
        suggestions = [i.suggestion for i in group if i.suggestion]
        if len(suggestions) > 1 and best.suggestion:
            best.suggestion = "；".join(suggestions)
        deduped.append(best)

    return deduped


def run_audit(task_id: str, use_quick_mode: bool = True, event_callback: Callable[[dict], None] | None = None) -> AuditTask:
    """执行审核任务（Agentic ReAct loop）。"""
    task = repo.get_task(task_id)
    if not task:
        raise ValueError(f"任务不存在: {task_id}")

    if task.status not in ("pending", "cancelled"):
        raise ValueError(f"任务状态不允许执行: {task.status}")

    # 更新状态
    task.status = "processing"
    task.started_at = datetime.utcnow()
    task.progress = 0.0
    repo.save_task(task)

    # 初始化降级日志收集
    from core.degradation import record as _deg_record, drain as _deg_drain
    degradation_log: list[dict] = []

    try:
        # 获取文档
        doc = doc_repo.get_doc(task.document_id)
        if not doc:
            raise ValueError(f"文档不存在: {task.document_id}")

        # 确保文档已处理
        if not doc.parsed_content:
            doc = audit_doc_svc.parse_document(doc.id)
        degradation_log.extend(_deg_drain())

        if not doc.structure:
            try:
                doc = structure_svc.analyze_document_structure(doc.id)
            except Exception:
                _deg_record("structure_analysis", "structure_analysis_failed",
                             "Document structure analysis failed, continuing without structure")
                degradation_log.extend(_deg_drain())

        task.progress = 0.1
        repo.save_task(task)

        parsed_content = doc.parsed_content or ""

        # Agentic ReAct 审核
        _logger.info("Running agentic audit for task %s", task_id)
        task.progress_label = "Agentic 审核中"
        repo.save_task(task)

        from services.agentic_audit import run_agentic_audit
        agentic_result = run_agentic_audit(
            parsed_content=parsed_content,
            structure=doc.structure,
            kb_ids=task.kb_ids,
            doc_name=doc.name,
            task_id=task.id,
            doc_id=doc.id,
            event_callback=event_callback,
        )
        all_issues = agentic_result.issues
        raw_parts = [agentic_result.raw_analysis or "Agentic 审核完成"]
        _logger.info("Agentic audit completed: %d issues found", len(all_issues))

        task.progress = 0.9
        repo.save_task(task)

        # 生成结果
        issues = _deduplicate_issues(all_issues)
        raw_analysis = "\n".join(raw_parts)

        summary = ResultSummary(
            total_clauses=doc.structure.total_clauses if doc.structure else 0,
            issues_count=len(issues),
            compliance_issues=sum(1 for i in issues if i.type == "compliance"),
            completeness_issues=sum(1 for i in issues if i.type == "completeness"),
            consistency_issues=sum(1 for i in issues if i.type == "consistency"),
            high_severity=sum(1 for i in issues if i.severity == "high"),
            medium_severity=sum(1 for i in issues if i.severity == "medium"),
            low_severity=sum(1 for i in issues if i.severity == "low"),
        )

        task.result = AuditResult(
            task_id=task.id,
            document_id=doc.id,
            document_name=doc.name,
            summary=summary,
            issues=issues,
            raw_analysis=raw_analysis,
        )

        task.status = "completed"
        task.progress = 1.0
        task.completed_at = datetime.utcnow()

    except Exception as e:
        task.status = "failed"
        task.error_message = str(e)
        task.completed_at = datetime.utcnow()
        _logger.error("audit task %s failed: %s", task_id, e)

    task.degradation_log = degradation_log
    repo.save_task(task)
    return task


def run_audit_async(task_id: str, use_quick_mode: bool = True):
    """异步执行审核任务。"""
    thread = threading.Thread(target=run_audit, args=(task_id, use_quick_mode))
    thread.daemon = True
    thread.start()
    return task_id
