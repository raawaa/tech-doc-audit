import concurrent.futures
import os
import threading
from datetime import datetime
from typing import Optional

from core.logger import get_logger
from models.audit_task import AuditTask, AuditIssue, AuditResult, ResultSummary, AuditType
import storage.audit_task_repo as repo
import storage.audit_doc_repo as doc_repo
import services.topic_audit as topic_audit
import services.agent_audit as agent_audit
import services.audit_doc_service as audit_doc_svc
import services.structure_service as structure_svc

_logger = get_logger(__name__)

# 限制并发 LLM 调用数，防止压垮本地 Ollama 或触发 API 限流
_LLM_SEMAPHORE = threading.Semaphore(4)


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


def _run_topic_audit_pipeline(
    topics: list[dict],
    parsed_content: str,
    task: AuditTask,
    repo,
    degradation_log: list[dict],
) -> tuple[list[AuditIssue], list[str], AuditTask]:
    """执行主题审核管线（并行），返回 (issues, raw_parts, updated_task)。"""
    all_issues = []
    raw_parts = []
    success_count = 0
    fail_count = 0

    from core.degradation import drain as _deg_drain

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(8, len(topics)) if topics else 1
    ) as executor:
        future_map = {
            executor.submit(
                _audit_single_topic, topic, parsed_content, task.kb_ids, i
            ): topic
            for i, topic in enumerate(topics)
        }
        total = len(future_map)
        for future in concurrent.futures.as_completed(future_map):
            task = repo.get_task(task.id)
            if task and task.status == "cancelled":
                for f in future_map:
                    f.cancel()
                break
            topic = future_map[future]
            result = future.result()
            if result["success"]:
                success_count += 1
            else:
                fail_count += 1
            all_issues.extend(result["issues"])
            degradation_log.extend(result.get("degradation_events", []))
            raw_parts.append(
                f"{result['name']}: 发现 {len(result['issues'])} 个问题"
                if result["issues"]
                else f"{result['name']}: 无问题"
            )
            completed = success_count + fail_count
            task.progress = 0.15 + (completed / total) * 0.75
            task.progress_label = result["name"] if result["success"] else f"失败：{result['name']}"
            repo.save_task(task)

    topic_progress = sum(1 for r in raw_parts)
    task.progress_label = f"已完成 {topic_progress}/{total} 个主题"
    task.progress = 0.9
    repo.save_task(task)

    if fail_count > 0:
        raw_parts.append(f"⚠️ {fail_count}/{len(topics)} 个主题审核失败")

    return all_issues, raw_parts, task


def _audit_single_topic(
    topic: dict,
    parsed_content: str,
    kb_ids: list[str],
    topic_index: int,
) -> dict:
    """审核单个主题（供并行执行使用，异常不会透出）。"""
    from core.degradation import drain as _deg_drain
    acquired = _LLM_SEMAPHORE.acquire(timeout=300)
    try:
        if not acquired:
            return {
                "name": topic.get("name", f"主题{topic_index}"),
                "issues": [],
                "success": False,
                "error": "LLM semaphore timeout",
                "degradation_events": _deg_drain(),
            }
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
                "degradation_events": _deg_drain(),
            }
        except Exception as e:
            _logger.warning("topic audit failed (%s): %s", topic.get("id", topic_index), e)
            return {
                "name": topic.get("name", f"主题{topic_index}"),
                "issues": [],
                "success": False,
                "error": str(e),
                "degradation_events": _deg_drain(),
            }
    finally:
        if acquired:
            _LLM_SEMAPHORE.release()


def _deduplicate_issues(issues: list) -> list:
    """合并描述同一根因的重复问题。

    不同审核主题可能对同一段文档报出相同问题。
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

    # 语义去重：不同但相似的 excerpt 可能描述同一问题
    # 策略：如果两个 issue 的 document_position 和 clause_number 相同
    # 且 description 文本相似度 > 60%，视为重复
    merged = list(groups.values())
    deduped: list = []
    seen_keys = set()

    severity_rank = {"high": 3, "medium": 2, "low": 1}

    for group in merged:
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

        # 1. LLM Agent 分析文档 → 确定审核主题
        if hasattr(task, 'audit_topics') and task.audit_topics:
            topics = task.audit_topics
        else:
            topics = agent_audit.determine_audit_topics(parsed_content, task.kb_ids)
            degradation_log.extend(_deg_drain())
            if not topics:
                _deg_record("agent_audit", "agent_topics_empty",
                             "LLM agent returned no topics, falling back to 8 fixed topics")
                degradation_log.extend(_deg_drain())
                topics = topic_audit.AUDIT_TOPICS

        task.progress = 0.15
        repo.save_task(task)

        # 2. 执行审核
        all_issues = []
        raw_parts = []

        # 2a. Agentic 审核（优先）
        if os.environ.get("USE_AGENTIC_AUDIT", "").lower() in ("true", "1", "yes"):
            _logger.info("Running agentic audit for task %s", task_id)
            task.progress_label = "Agentic 审核中"
            repo.save_task(task)
            try:
                from services.agentic_audit import run_agentic_audit
                agentic_result = run_agentic_audit(
                    parsed_content=parsed_content,
                    structure=doc.structure,
                    kb_ids=task.kb_ids,
                    doc_name=doc.name,
                    task_id=task.id,
                    doc_id=doc.id,
                )
                all_issues = agentic_result.issues
                raw_parts.append(
                    agentic_result.raw_analysis or "Agentic 审核完成"
                )
                _logger.info(
                    "Agentic audit completed: %d issues found",
                    len(all_issues),
                )
            except Exception as e:
                _logger.warning("Agentic audit failed, falling back to topic audit: %s", e)
                degradation_log.append({
                    "source": "agentic_audit",
                    "event": "agentic_failed",
                    "detail": str(e),
                })
                raw_parts.append(f"Agentic 审核: 失败（{e}），降级到主题审核")
                # 降级到 topic_audit
                if not topics:
                    topics = topic_audit.AUDIT_TOPICS
                all_issues, raw_parts, _ = _run_topic_audit_pipeline(
                    topics, parsed_content, task, repo, degradation_log,
                )
            task.progress = 0.9
            repo.save_task(task)
        else:
            # 2b. 主题审核管线（默认）
            if not topics:
                _deg_record("agent_audit", "agent_topics_empty",
                             "LLM agent returned no topics, falling back to 8 fixed topics")
                degradation_log.extend(_deg_drain())
                topics = topic_audit.AUDIT_TOPICS
            all_issues, raw_parts, task = _run_topic_audit_pipeline(
                topics, parsed_content, task, repo, degradation_log,
            )
            task.progress = 0.9
            repo.save_task(task)

            # 2c. 可选：需求锚定审核
            if os.environ.get("USE_REQUIREMENT_AUDIT", "").lower() in ("true", "1", "yes"):
                try:
                    _logger.info("Running requirement-anchored audit for task %s", task_id)
                    from services.requirement_audit import run_requirement_audit

                    req_result = run_requirement_audit(
                        task_id=task.id,
                        doc_id=doc.id,
                        document_name=doc.name,
                        parsed_content=parsed_content,
                        structure=doc.structure,
                        kb_ids=task.kb_ids,
                    )
                    if req_result.issues:
                        _logger.info(
                            "Requirement audit found %d additional issues",
                            len(req_result.issues),
                        )
                        all_issues.extend(req_result.issues)
                        raw_parts.append(
                            f"需求锚定审核: 发现 {len(req_result.issues)} 个问题"
                        )
                except Exception as e:
                    _logger.warning("Requirement audit failed: %s", e)
                    raw_parts.append(f"需求锚定审核: 失败（{e}）")

        # 3. 生成结果（即使部分 topic 失败，已完成的结果也不丢失）
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
