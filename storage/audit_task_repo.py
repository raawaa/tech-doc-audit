import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from models.audit_task import AuditTask
from storage import validate_id

DATA_DIR = Path(os.environ.get("AUDIT_DATA_DIR", "./data"))


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _task_dir(doc_id: str) -> Path:
    validate_id(doc_id, "document_id")
    return DATA_DIR / "audits" / doc_id / "tasks"


def _task_file(doc_id: str, task_id: str) -> Path:
    validate_id(task_id, "task_id")
    return _task_dir(doc_id) / f"{task_id}.json"


def _locate_task(task_id: str) -> Optional[Path]:
    """Search audits/*/tasks/ for a task by ID."""
    audits_dir = DATA_DIR / "audits"
    if not audits_dir.exists():
        return None
    for d in audits_dir.iterdir():
        p = d / "tasks" / f"{task_id}.json"
        if p.exists():
            return p
    return None


def save_task(task: AuditTask) -> AuditTask:
    """保存审核任务。"""
    task.updated_at = datetime.utcnow()
    _ensure_dir(_task_dir(task.document_id))
    data = task.to_dict()
    with open(_task_file(task.document_id, task.id), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return task


def get_task(task_id: str) -> Optional[AuditTask]:
    """获取审核任务。"""
    path = _locate_task(task_id)
    if not path:
        return None
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return AuditTask.from_dict(data)


def list_tasks(document_id: Optional[str] = None) -> list[AuditTask]:
    """列出审核任务。"""
    audits_dir = DATA_DIR / "audits"
    _ensure_dir(audits_dir)
    results = []
    if document_id:
        tasks_dir = _task_dir(document_id)
        if tasks_dir.exists():
            for f in tasks_dir.iterdir():
                if f.suffix == ".json":
                    with open(f, "r", encoding="utf-8") as fh:
                        data = json.load(fh)
                    results.append(AuditTask.from_dict(data))
    else:
        for d in audits_dir.iterdir():
            tasks_dir = d / "tasks"
            if tasks_dir.exists():
                for f in tasks_dir.iterdir():
                    if f.suffix == ".json":
                        with open(f, "r", encoding="utf-8") as fh:
                            data = json.load(fh)
                        results.append(AuditTask.from_dict(data))
    results.sort(key=lambda t: t.created_at, reverse=True)
    return results


def delete_task(task_id: str) -> bool:
    """删除审核任务。"""
    path = _locate_task(task_id)
    if path:
        path.unlink()
        return True
    return False
