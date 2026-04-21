import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from models.audit_task import AuditTask

DATA_DIR = Path(__file__).parent.parent / "data"
AUDIT_TASKS_DIR = DATA_DIR / "audit_tasks"


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _task_file(task_id: str) -> Path:
    _ensure_dir(AUDIT_TASKS_DIR)
    return AUDIT_TASKS_DIR / f"{task_id}.json"


def save_task(task: AuditTask) -> AuditTask:
    """保存审核任务。"""
    task.updated_at = datetime.utcnow()
    data = task.to_dict()
    with open(_task_file(task.id), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return task


def get_task(task_id: str) -> Optional[AuditTask]:
    """获取审核任务。"""
    path = _task_file(task_id)
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return AuditTask.from_dict(data)


def list_tasks(document_id: Optional[str] = None) -> list[AuditTask]:
    """列出审核任务。"""
    _ensure_dir(AUDIT_TASKS_DIR)
    results = []
    for f in AUDIT_TASKS_DIR.iterdir():
        if f.suffix == ".json":
            with open(f, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            task = AuditTask.from_dict(data)
            if document_id is None or task.document_id == document_id:
                results.append(task)
    # 按创建时间倒序
    results.sort(key=lambda t: t.created_at, reverse=True)
    return results


def delete_task(task_id: str) -> bool:
    """删除审核任务。"""
    path = _task_file(task_id)
    if path.exists():
        path.unlink()
        return True
    return False
