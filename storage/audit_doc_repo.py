import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from models.audit_document import AuditDocument

DATA_DIR = Path(os.environ.get("AUDIT_DATA_DIR", "./data"))


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _doc_dir(doc_id: str) -> Path:
    return DATA_DIR / "audits" / doc_id / "doc"


def _doc_file(doc_id: str, file_type: str) -> Path:
    return _doc_dir(doc_id) / f"original.{file_type}"


def _meta_file(doc_id: str) -> Path:
    return DATA_DIR / "audits" / doc_id / "meta.json"


def _doc_to_json(doc: AuditDocument) -> dict:
    """将 AuditDocument 转换为 JSON 兼容的字典。"""
    data = doc.model_dump()
    for key in ("created_at", "updated_at"):
        if hasattr(data.get(key), "isoformat"):
            data[key] = data[key].isoformat()
    return data


def save_doc(doc: AuditDocument) -> AuditDocument:
    """保存文档元数据。"""
    with open(_meta_file(doc.id), "w", encoding="utf-8") as f:
        json.dump(_doc_to_json(doc), f, ensure_ascii=False, indent=2)
    return doc


def get_doc(doc_id: str) -> Optional[AuditDocument]:
    """获取文档元数据。"""
    path = _meta_file(doc_id)
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return AuditDocument.from_dict(data)


def list_docs() -> list[AuditDocument]:
    """列出所有待审核文档。"""
    audits_dir = DATA_DIR / "audits"
    _ensure_dir(audits_dir)
    results = []
    for d in audits_dir.iterdir():
        meta = d / "meta.json"
        if meta.exists():
            with open(meta, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            results.append(AuditDocument.from_dict(data))
    results.sort(key=lambda d: d.created_at, reverse=True)
    return results


def update_doc(doc: AuditDocument) -> AuditDocument:
    """更新文档元数据。"""
    doc.updated_at = datetime.utcnow()
    return save_doc(doc)


def delete_doc(doc_id: str) -> bool:
    """删除文档及其文件。"""
    audit_dir = DATA_DIR / "audits" / doc_id
    if audit_dir.exists():
        import shutil
        shutil.rmtree(audit_dir)
    return True
