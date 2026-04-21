import json
import os
import httpx
from typing import Optional

import storage.kb_repo as kb_repo
import storage.doc_repo as doc_repo
import storage.index_repo as index_repo


OLLAMA_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
MODEL = os.environ.get("OLLAMA_MODEL", "qwen3.5:0.8b")


def search_in_knowledge_base(kb_id: str, query: str, max_results: int = 5) -> list[dict]:
    """在单个知识库中检索相关内容。"""
    kb = kb_repo.get(kb_id)
    if not kb:
        return []

    results = []
    docs = doc_repo.list_docs(kb_id)

    for doc in docs:
        if doc.index_status != "ready" or not doc.tree_index_path:
            continue

        # 加载索引
        tree = index_repo.load_index(kb_id, doc.id)
        if not tree:
            continue

        # 在索引中搜索相关内容
        doc_results = _search_in_tree(tree, query, doc.name, max_results // len(docs) + 1)
        results.extend(doc_results)

    return results[:max_results]


def search_multiple_kbs(kb_ids: list[str], query: str, max_results: int = 10) -> list[dict]:
    """在多个知识库中检索。"""
    all_results = []
    for kb_id in kb_ids:
        results = search_in_knowledge_base(kb_id, query, max_results // len(kb_ids) + 2)
        all_results.extend(results)

    # 去重并限制数量
    seen = set()
    unique_results = []
    for r in all_results:
        key = f"{r.get('standard_id', '')}-{r.get('clause', '')}"
        if key not in seen:
            seen.add(key)
            unique_results.append(r)

    return unique_results[:max_results]


def _search_in_tree(tree: dict, query: str, doc_name: str, max_results: int) -> list[dict]:
    """在索引树中搜索。"""
    results = []

    # 检查标题
    if tree.get("title") and _text_matches(query, tree["title"]):
        results.append({
            "source": "tree",
            "doc_name": doc_name,
            "standard_name": tree.get("title", doc_name),
            "standard_id": tree.get("model", ""),
            "content": tree.get("content_summary", ""),
            "relevance": 0.8,
        })

    # 检查结构
    structure = tree.get("structure", {})
    for chapter in structure.get("chapters", []):
        for clause in chapter.get("clauses", []):
            clause_text = clause.get("text", "")
            if _text_matches(query, clause_text):
                results.append({
                    "source": "structure",
                    "doc_name": doc_name,
                    "standard_name": tree.get("title", doc_name),
                    "chapter": chapter.get("title", ""),
                    "clause_number": clause.get("number", ""),
                    "content": clause_text,
                    "relevance": 0.9 if len(query) > 5 else 0.6,
                })

    return results[:max_results]


def _text_matches(query: str, text: str) -> bool:
    """简单的文本匹配检查。"""
    query_lower = query.lower()
    text_lower = text.lower()

    # 关键词匹配
    query_words = query_lower.split()
    match_count = sum(1 for word in query_words if word in text_lower)
    return match_count >= len(query_words) * 0.5


def get_kb_content_for_audit(kb_ids: list[str], clause_text: str) -> str:
    """获取相关知识库内容用于审核分析。"""
    # 检索相关标准
    results = search_multiple_kbs(kb_ids, clause_text, max_results=5)

    if not results:
        return "未找到相关标准依据。"

    # 格式化内容
    content_parts = ["【参考标准依据】\n"]
    for i, r in enumerate(results, 1):
        content_parts.append(f"\n{i}. {r.get('standard_name', '标准')}")
        if r.get("chapter"):
            content_parts.append(f"   章节: {r['chapter']}")
        if r.get("clause_number"):
            content_parts.append(f"   条款: {r['clause_number']}")
        if r.get("content"):
            content_parts.append(f"   内容: {r['content'][:300]}")

    return "\n".join(content_parts)


def use_llm_search(kb_ids: list[str], query: str, max_results: int = 3) -> str:
    """使用 LLM 辅助搜索相关标准。"""
    kb_info = []
    for kb_id in kb_ids:
        kb = kb_repo.get(kb_id)
        if kb:
            kb_info.append(f"- {kb.name} (ID: {kb_id})")

    prompt = f"""根据以下查询，从知识库中检索相关标准条文。

查询内容: {query}

可用知识库:
{chr(10).join(kb_info)}

请列出可能相关的标准名称和条款编号。直接输出标准列表，不需要解释。"""

    try:
        response = httpx.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": MODEL,
                "prompt": prompt,
                "stream": False,
            },
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()
        return data.get("response", "")
    except Exception:
        return ""
