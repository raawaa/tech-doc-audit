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
    # 原子写：先写 .tmp 再 rename，避免 read 端在 truncate / write 之间读到空文件
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)
    return kb


def get(kb_id: str) -> Optional[KnowledgeBase]:
    return get_atomic(kb_id)


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


def get_atomic(kb_id: str) -> Optional[KnowledgeBase]:
    """读取 KB 元数据，宽容瞬时截断（防 daemon 线程抢写期间读到空文件）。

    kb_repo.update 内部用 ``open(... 'w')``——这是 truncate-then-write，
    read 端在两者之间读到空文件会 JSONDecodeError。
    这里加 retries 处理并发写竞争：1ms/2ms/4ms 退避，最多 5 次。
    """
    path = _kb_file(kb_id)
    if not path.exists():
        return None
    last_err: Optional[Exception] = None
    for delay in (0.0, 0.001, 0.002, 0.004, 0.008):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return KnowledgeBase.from_dict(data)
        except Exception as e:
            last_err = e
            if delay > 0:
                import time as _t
                _t.sleep(delay)
    # 5 次都失败，重抛最后一次错误（可能是真的文件损坏）
    raise last_err  # type: ignore[misc] 


def delete(kb_id: str) -> bool:
    kb_dir = _kb_dir(kb_id)
    if not kb_dir.exists():
        return False
    shutil.rmtree(kb_dir)
    return True
