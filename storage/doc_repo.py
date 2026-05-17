import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional
import shutil

from models.document import KBDocument

DATA_DIR = Path(os.environ.get("AUDIT_DATA_DIR", "./data"))


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _kb_docs_dir(kb_id: str) -> Path:
    return DATA_DIR / "kbs" / kb_id / "docs"


def _doc_meta_dir(kb_id: str) -> Path:
    return DATA_DIR / "kbs" / kb_id / "meta"


def _doc_meta_file(kb_id: str, doc_id: str) -> Path:
    return _doc_meta_dir(kb_id) / f"{doc_id}.json"


def _doc_to_json(doc: KBDocument) -> dict:
    """将 KBDocument 转换为 JSON 兼容的字典。"""
    data = doc.to_dict()
    for key in ("created_at", "updated_at"):
        if hasattr(data.get(key), "isoformat"):
            data[key] = data[key].isoformat()
    return data


def save_doc(kb_id: str, original_name: str, content: bytes, file_type: str) -> KBDocument:
    _ensure_dir(_kb_docs_dir(kb_id))
    doc = KBDocument(
        kb_id=kb_id,
        name=original_name,
        original_name=original_name,
        file_type=file_type,
        file_path="",
    )
    # 文件名 = 原名 + ULID（保留原名可提高搜索命中率）
    import re as _re
    _stem = _re.sub(r'[^\w\s一-鿿\-]', '', Path(original_name).stem)[:80] or "doc"
    _stem = _re.sub(r'\s+', '_', _stem)
    doc.file_path = str(_kb_docs_dir(kb_id) / f"{_stem}_{doc.id}.{file_type}")
    with open(doc.file_path, "wb") as f:
        f.write(content)
    _ensure_dir(_doc_meta_dir(kb_id))
    with open(_doc_meta_file(kb_id, doc.id), "w", encoding="utf-8") as f:
        json.dump(_doc_to_json(doc), f, ensure_ascii=False, indent=2)
    return doc


def get_doc(kb_id: str, doc_id: str) -> Optional[KBDocument]:
    path = _doc_meta_file(kb_id, doc_id)
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return KBDocument.from_dict(data)


def list_docs(kb_id: str) -> list[KBDocument]:
    meta_dir = _doc_meta_dir(kb_id)
    if not meta_dir.exists():
        return []
    results = []
    for f in meta_dir.iterdir():
        if f.suffix == ".json" and f.stem != "kb":
            with open(f, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            results.append(KBDocument.from_dict(data))
    return results


def _save_doc_meta(doc: KBDocument) -> None:
    """保存文档元数据到 JSON 文件。"""
    _ensure_dir(_doc_meta_dir(doc.kb_id))
    with open(_doc_meta_file(doc.kb_id, doc.id), "w", encoding="utf-8") as f:
        json.dump(_doc_to_json(doc), f, ensure_ascii=False, indent=2)


def delete_doc(kb_id: str, doc_id: str) -> bool:
    meta_path = _doc_meta_file(kb_id, doc_id)
    if meta_path.exists():
        meta_path.unlink()
    doc = get_doc(kb_id, doc_id)
    if doc and Path(doc.file_path).exists():
        Path(doc.file_path).unlink()
    return True
