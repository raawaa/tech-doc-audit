import threading
from datetime import datetime
from typing import Optional

from models.audit_task import AuditTask, AuditResult, ResultSummary, AuditType
import storage.audit_task_repo as repo
import storage.audit_doc_repo as doc_repo
import services.audit_analysis_service as analysis_svc
import services.topic_audit as topic_audit


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
            from services.audit_doc_service import parse_document
            doc = parse_document(doc.id)

        if not doc.structure:
            from services.structure_service import analyze_document_structure
            try:
                doc = analyze_document_structure(doc.id)
            except Exception:
                pass

        task.progress = 0.1
        repo.save_task(task)

        # 确保 parsed_content 先于任何使用定义
        parsed_content = doc.parsed_content or ""

        # 执行审核 — 主题式批量审核
        # 1. LLM Agent 分析文档 → 确定审核主题
        # 2. 关键词定位段落 + 搜 KB → 1 次 LLM 审核
        if hasattr(task, 'audit_topics') and task.audit_topics:
            topics = task.audit_topics  # 用户指定的主题
        else:
            # Agent 动态主题选择，降级到固定主题
            from services.agent_audit import determine_audit_topics
            topics = determine_audit_topics(parsed_content, task.kb_ids)
            if not topics:
                topics = topic_audit.AUDIT_TOPICS

        all_issues = []
        raw_parts = []
        total = max(len(topics), 1)

        for i, topic in enumerate(topics):
            task.progress = 0.1 + 0.8 * (i / total)
            repo.save_task(task)

            topic_issues = topic_audit.audit_topic(
                topic=topic,
                doc_nav=None,
                kb_ids=task.kb_ids,
                topic_index=i,
                parsed_content=parsed_content,
            )
            all_issues.extend(topic_issues)
            raw_parts.append(
                f"{topic['name']}: 发现 {len(topic_issues)} 个问题"
                if topic_issues else f"{topic['name']}: 无问题"
            )

        issues = all_issues
        raw_analysis = "\n".join(raw_parts)

        # 生成结果
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

    repo.save_task(task)
    return task


def run_audit_async(task_id: str, use_quick_mode: bool = True):
    """异步执行审核任务。"""
    thread = threading.Thread(target=run_audit, args=(task_id, use_quick_mode))
    thread.daemon = True
    thread.start()
    return task_id
