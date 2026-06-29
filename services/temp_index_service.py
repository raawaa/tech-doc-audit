import json
import os
from pathlib import Path

from models.audit_document import AuditDocument
import storage.audit_doc_repo as repo


DATA_BASE = Path(os.environ.get("AUDIT_DATA_DIR", "./data"))
AUDITS_DIR = DATA_BASE / "audits"


def _index_file(doc_id: str) -> Path:
    return AUDITS_DIR / doc_id / "tree_index.json"


def build_temp_index(doc: AuditDocument) -> AuditDocument:
    """为待审核文档构建临时索引。"""
    try:
        tree = _generate_index_tree(doc)
        index_path = _index_file(doc.id)
        index_path.parent.mkdir(parents=True, exist_ok=True)

        with open(index_path, "w", encoding="utf-8") as f:
            json.dump(tree, f, ensure_ascii=False, indent=2)

        doc.tree_index_path = str(index_path)
        doc.status = "indexed"
    except Exception as e:
        doc.status = "failed"
        doc.error_message = f"索引构建失败: {str(e)}"

    return repo.update_doc(doc)


def delete_temp_index(doc_id: str) -> bool:
    """删除临时索引。"""
    index_path = _index_file(doc_id)
    if index_path.exists():
        index_path.unlink()
    return True


def _generate_index_tree(doc: AuditDocument) -> dict:
    """生成索引树。"""
    tree = {
        "doc_id": doc.id,
        "doc_name": doc.name,
        "generated_at": doc.updated_at.isoformat() if hasattr(doc.updated_at, 'isoformat') else str(doc.updated_at),
        "source": "audit_document",
    }

    # 如果有结构信息，加入索引树
    if doc.structure:
        tree["structure"] = {
            "title": doc.structure.title,
            "chapters": [
                {
                    "number": ch.number,
                    "title": ch.title,
                    "clauses": [{"number": c.number, "text": c.text[:200]} for c in ch.clauses]
                }
                for ch in doc.structure.chapters
            ],
            "total_clauses": doc.structure.total_clauses,
        }

    # 如果有解析内容，加入文本摘要
    if doc.parsed_content:
        tree["content_summary"] = doc.parsed_content[:5000]

    return tree
