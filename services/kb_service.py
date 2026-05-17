import shutil
from typing import Optional, Literal

from models.knowledge_base import KnowledgeBase
import storage.kb_repo as kb_repo


def create_kb(name: str, description: str = "", category: Literal["national", "industry", "enterprise"] = "national") -> KnowledgeBase:
    kb = KnowledgeBase(name=name, description=description, category=category)
    return kb_repo.create(kb)


def get_kb(kb_id: str) -> Optional[KnowledgeBase]:
    return kb_repo.get(kb_id)


def list_kbs(category: Optional[str] = None) -> list[KnowledgeBase]:
    kbs = kb_repo.list_all()
    if category:
        kbs = [kb for kb in kbs if kb.category == category]
    return kbs


def delete_kb(kb_id: str) -> bool:
    """级联删除知识库全部数据（docs + meta + vectors）。"""
    from pathlib import Path
    from storage.kb_repo import DATA_DIR
    kb_dir = DATA_DIR / "kbs" / kb_id
    if kb_dir.exists():
        shutil.rmtree(kb_dir)
    return kb_repo.delete(kb_id)
