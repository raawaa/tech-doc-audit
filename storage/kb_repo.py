import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional
import shutil

from models.knowledge_base import KnowledgeBase
from storage import validate_id

DATA_DIR = Path(os.environ.get("AUDIT_DATA_DIR", "./data"))
KBS_DIR = DATA_DIR / "kbs"

_write_lock = threading.Lock()


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _kb_dir(kb_id: str) -> Path:
    validate_id(kb_id, "kb_id")
    return KBS_DIR / kb_id


def _kb_file(kb_id: str) -> Path:
    return _kb_dir(kb_id) / "kb.json"


def create(kb: KnowledgeBase) -> KnowledgeBase:
    _ensure_dir(_kb_dir(kb.id))
    path = _kb_file(kb.id)
    data = kb.to_dict()
    # 转换 datetime 为 ISO 格式
    for key in ("created_at", "updated_at"):
        if hasattr(data.get(key), "isoformat"):
            data[key] = data[key].isoformat()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return kb


def get(kb_id: str) -> Optional[KnowledgeBase]:
    path = _kb_file(kb_id)
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return KnowledgeBase.from_dict(data)


def list_all() -> list[KnowledgeBase]:
    if not KBS_DIR.exists():
        return []
    results = []
    for kb_dir in KBS_DIR.iterdir():
        if kb_dir.is_dir():
            kb_file = kb_dir / "kb.json"
            if kb_file.exists():
                with open(kb_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                results.append(KnowledgeBase.from_dict(data))
    return results


def update(kb: KnowledgeBase) -> KnowledgeBase:
    with _write_lock:
        kb.updated_at = datetime.utcnow()
        return create(kb)


def delete(kb_id: str) -> bool:
    kb_dir = _kb_dir(kb_id)
    if not kb_dir.exists():
        return False
    shutil.rmtree(kb_dir)
    return True
