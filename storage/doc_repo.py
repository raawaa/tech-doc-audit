import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional
import shutil

from core.logger import get_logger
from models.document import KBDocument
from storage import atomic_write_json, validate_id

_logger = get_logger(__name__)


def get_data_dir() -> Path:
    """解析数据根目录；每次调用读取 env（issue #137 per-test 隔离）。"""
    return Path(os.environ.get("AUDIT_DATA_DIR", "./data"))


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _kb_docs_dir(kb_id: str) -> Path:
    validate_id(kb_id, "kb_id")
    return get_data_dir() / "kbs" / kb_id / "docs"


def _doc_meta_dir(kb_id: str) -> Path:
    return get_data_dir() / "kbs" / kb_id / "meta"


def _doc_meta_file(kb_id: str, doc_id: str) -> Path:
    validate_id(doc_id, "doc_id")
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
    # issue #157：原子写，与 _save_doc_meta 同源走 storage.atomic_write_json
    atomic_write_json(_doc_meta_file(kb_id, doc.id), _doc_to_json(doc))
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
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                results.append(KBDocument.from_dict(data))
            except Exception:
                continue
    return results


def _save_doc_meta(doc: KBDocument) -> None:
    """保存文档元数据到 JSON 文件。"""
    _ensure_dir(_doc_meta_dir(doc.kb_id))
    # issue #157：原子写，避免 POST /kb-documents/{id}/reparse 期间 doc/kb 同时更新
    # 撞半截 → JSONDecodeError（详见 storage.atomic_write_json 文档）
    atomic_write_json(_doc_meta_file(doc.kb_id, doc.id), _doc_to_json(doc))


def find_doc_by_id(doc_id: str) -> Optional[KBDocument]:
    """跨所有 KB 查找指定 ID 的文档。

    扫描 data/kbs/ 下所有 KB 目录的 meta 文件。
    """
    validate_id(doc_id, "doc_id")
    for kb_dir in get_data_dir().glob("kbs/*"):
        if not kb_dir.is_dir():
            continue
        kb_id = kb_dir.name
        doc = get_doc(kb_id, doc_id)
        if doc:
            return doc
    return None


def mark_doc_embedding_failed(
    kb_id: str, doc_id: str, err: Optional[BaseException] = None,
) -> None:
    """把单篇 doc 的 ``embedding_status`` 标 ``failed``(ADR-0007 §3)。

    这是该状态转移的**唯一公开入口**(issue #167)——历史上它藏在
    ``core.index_manager._mark_doc_embedding_failed`` 里,放到 repo 层后
    "谁在写 failed" 一次 grep 即可穷举。

    失败原因写进 ``doc.metadata["embedding_error"]``,形如 ``TypeName: message``。
    ``err=None`` 表示"标失败但没有具体异常"——此时**清掉**该键而不是留着上一次
    的原因:``embedding_error`` 描述的是本次失败,留旧值会把运维引向错误的方向。

    全程 best-effort:doc 不在 repo(脚本直调 ``index_documents_batch`` 等
    场景)、读盘/写盘失败,一律 log warning 后返回,**不**抛——避免让批量流程
    挂在 doc 元数据写不上。调用方 ``index_documents_batch`` 拿到 embedding
    失败后用本函数记账,然后 ``continue`` 跳过这一稿、其余稿按正常流程跑。
    """
    try:
        doc = get_doc(kb_id, doc_id)
    except Exception as e:
        _logger.warning(
            "mark_doc_embedding_failed: failed to load doc %s for failure mark: %s",
            doc_id, e,
        )
        return
    if doc is None:
        _logger.warning(
            "mark_doc_embedding_failed: doc %s not in doc_repo; "
            "cannot persist embedding_status=failed (caller may be a script)",
            doc_id,
        )
        return
    doc.embedding_status = "failed"
    if not isinstance(doc.metadata, dict):
        doc.metadata = {}
    if err is None:
        doc.metadata.pop("embedding_error", None)
    else:
        doc.metadata["embedding_error"] = f"{type(err).__name__}: {err}"
    try:
        _save_doc_meta(doc)
    except Exception as e:
        _logger.warning(
            "mark_doc_embedding_failed: failed to persist failure mark for %s: %s",
            doc_id, e,
        )


def delete_doc(kb_id: str, doc_id: str) -> bool:
    meta_path = _doc_meta_file(kb_id, doc_id)
    if meta_path.exists():
        meta_path.unlink()
    doc = get_doc(kb_id, doc_id)
    if doc and Path(doc.file_path).exists():
        Path(doc.file_path).unlink()
    return True
