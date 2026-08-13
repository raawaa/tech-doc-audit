import shutil
from typing import Optional, Literal

from models.knowledge_base import KnowledgeBase
import storage.kb_repo as kb_repo


def create_kb(name: str, description: str = "", category: Literal["national", "industry", "enterprise"] = "national") -> KnowledgeBase:
    """创建知识库 —— 同时落盘 ``vectors/index.meta.json``(issues/144 AC#3)。

    meta 在 KB 创建时立即写入,值为当前生产 provider 体系(``BAAI/bge-m3`` /
    ``dim=1024``)。后续 ``index_document`` 会读取并断言,无需每条 KB 手工写。

    对存量 KB(本接口出现之前已落盘)由 ``scripts/backfill_kb_meta.py``
    一次性回填。
    """
    kb = KnowledgeBase(name=name, description=description, category=category)
    kb = kb_repo.create(kb)
    from core.index_manager import _write_index_meta
    _write_index_meta(
        kb.id, model_id="BAAI/bge-m3", dim=1024, force=True,
    )
    return kb


def get_kb(kb_id: str) -> Optional[KnowledgeBase]:
    return kb_repo.get(kb_id)


def list_kbs(category: Optional[str] = None) -> list[KnowledgeBase]:
    kbs = kb_repo.list_all()
    if category:
        kbs = [kb for kb in kbs if kb.category == category]
    return kbs


def update_kb(kb: KnowledgeBase) -> KnowledgeBase:
    """更新知识库。"""
    return kb_repo.update(kb)


def delete_kb(kb_id: str) -> bool:
    """级联删除知识库全部数据（docs + meta + vectors）。"""
    return kb_repo.delete(kb_id)
