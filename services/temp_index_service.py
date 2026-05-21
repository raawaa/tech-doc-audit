import json
import os
from pathlib import Path
from typing import Optional

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


def load_temp_index(doc_id: str) -> Optional[dict]:
    """加载临时索引。"""
    index_path = _index_file(doc_id)
    if not index_path.exists():
        return None
    with open(index_path, "r", encoding="utf-8") as f:
        return json.load(f)


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


def search_in_document(doc_id: str, query: str) -> list[dict]:
    """在文档中搜索相关内容（简单关键词匹配）。"""
    doc = repo.get_doc(doc_id)
    if not doc or not doc.parsed_content:
        return []

    results = []
    content = doc.parsed_content.lower()
    query_lower = query.lower()

    # 简单关键词匹配
    if query_lower in content:
        # 找到匹配位置
        start_idx = content.find(query_lower)
        # 提取周围上下文
        context_start = max(0, start_idx - 100)
        context_end = min(len(content), start_idx + len(query) + 200)
        snippet = doc.parsed_content[context_start:context_end]

        results.append({
            "doc_id": doc_id,
            "doc_name": doc.name,
            "query": query,
            "snippet": snippet,
            "position": start_idx,
        })

    # 如果有结构信息，也搜索条款
    if doc.structure:
        for chapter in doc.structure.chapters:
            for clause in chapter.clauses:
                if query_lower in clause.text.lower():
                    results.append({
                        "doc_id": doc_id,
                        "doc_name": doc.name,
                        "chapter": chapter.title,
                        "clause_number": clause.number,
                        "clause_text": clause.text[:300],
                        "query": query,
                    })

    return results
