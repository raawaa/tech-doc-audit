import shutil
from typing import Optional, Literal

from models.knowledge_base import KnowledgeBase
import storage.kb_repo as kb_repo
import storage.doc_repo as doc_repo
import storage.index_repo as index_repo


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
    # cascade: documents, index, meta
    for doc in doc_repo.list_docs(kb_id):
        doc_repo.delete_doc(kb_id, doc.id)
        index_repo.delete_index(kb_id, doc.id)
    # 删除原始文件目录
    import os
    from pathlib import Path
    from storage.doc_repo import _kb_docs_dir
    docs_dir = _kb_docs_dir(kb_id)
    if docs_dir.exists():
        shutil.rmtree(docs_dir)
    from storage.index_repo import _kb_index_dir
    idx_dir = _kb_index_dir(kb_id)
    if idx_dir.exists():
        shutil.rmtree(idx_dir)
    return kb_repo.delete(kb_id)
