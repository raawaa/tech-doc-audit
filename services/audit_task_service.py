import concurrent.futures
import threading
from datetime import datetime
from typing import Optional

from core.logger import get_logger
from models.audit_task import AuditTask, AuditResult, ResultSummary, AuditType
import storage.audit_task_repo as repo
import storage.audit_doc_repo as doc_repo
import services.topic_audit as topic_audit
import services.agent_audit as agent_audit
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


def _audit_single_topic(
    topic: dict,
    parsed_content: str,
    kb_ids: list[str],
    topic_index: int,
) -> dict:
    """审核单个主题（供并行执行使用，异常不会透出）。"""
    try:
        issues = topic_audit.audit_topic(
            topic=topic,
            doc_nav=None,
            kb_ids=kb_ids,
            topic_index=topic_index,
            parsed_content=parsed_content,
        )
        return {
            "name": topic.get("name", f"主题{topic_index}"),
            "issues": issues,
            "success": True,
        }
    except Exception as e:
        _logger.warning("topic audit failed (%s): %s", topic.get("id", topic_index), e)
        return {
            "name": topic.get("name", f"主题{topic_index}"),
            "issues": [],
            "success": False,
            "error": str(e),
        }


def run_audit(task_id: str, use_quick_mode: bool = True) -> AuditTask:
    """执行审核任务。"""
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

    try:
        # 获取文档
        doc = doc_repo.get_doc(task.document_id)
        if not doc:
            raise ValueError(f"文档不存在: {task.document_id}")

        # 确保文档已处理
        if not doc.parsed_content:
            doc = audit_doc_svc.parse_document(doc.id)

        if not doc.structure:
            try:
                doc = structure_svc.analyze_document_structure(doc.id)
            except Exception:
                pass

        task.progress = 0.1
        repo.save_task(task)

        parsed_content = doc.parsed_content or ""

        # 1. LLM Agent 分析文档 → 确定审核主题
        if hasattr(task, 'audit_topics') and task.audit_topics:
            topics = task.audit_topics
        else:
            topics = agent_audit.determine_audit_topics(parsed_content, task.kb_ids)
            if not topics:
                topics = topic_audit.AUDIT_TOPICS

        task.progress = 0.15
        repo.save_task(task)

        # 2. 并行执行各主题审核
        # 各主题间无依赖，并行化可大幅缩短总耗时
        all_issues = []
        raw_parts = []
        success_count = 0
        fail_count = 0

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(8, len(topics)) if topics else 1
        ) as executor:
            futures = [
                executor.submit(
                    _audit_single_topic, topic, parsed_content, task.kb_ids, i
                )
                for i, topic in enumerate(topics)
            ]
            concurrent.futures.wait(futures)

        for future in futures:
            result = future.result()
            if result["success"]:
                success_count += 1
            else:
                fail_count += 1
            all_issues.extend(result["issues"])
            raw_parts.append(
                f"{result['name']}: 发现 {len(result['issues'])} 个问题"
                if result["issues"]
                else f"{result['name']}: 无问题"
            )

        task.progress = 0.9
        repo.save_task(task)

        # 3. 生成结果（即使部分 topic 失败，已完成的结果也不丢失）
        issues = all_issues
        raw_analysis = "\n".join(raw_parts)
        if fail_count > 0:
            raw_analysis += f"\n\n⚠️ {fail_count}/{len(topics)} 个主题审核失败"

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

    repo.save_task(task)
    return task


def run_audit_async(task_id: str, use_quick_mode: bool = True):
    """异步执行审核任务。"""
    thread = threading.Thread(target=run_audit, args=(task_id, use_quick_mode))
    thread.daemon = True
    thread.start()
    return task_id
