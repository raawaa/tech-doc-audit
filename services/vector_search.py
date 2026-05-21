"""向量检索服务 — LlamaIndex VectorStoreIndex + FAISS。

流程：
1. 文档导入 KB 时自动分块（SentenceSplitter） + embedding（bge-m3）写入 FAISS 索引
2. 搜索时 embedding query → FAISS ANN 召回
3. 纯文本关键词搜索保留为最终降级

与旧版 numpy 暴力搜索保持相同公开 API，内部改用 LlamaIndex。
"""

import os
import subprocess
from pathlib import Path

import storage.kb_repo as kb_repo
from core.index_manager import (
    search as _vec_search,
    index_document as _index_to_store,
    remove_document as _remove_from_store,
    rebuild_kb_index as _rebuild_store,
    get_kb_index_built,
)
from core.logger import get_logger

_logger = get_logger(__name__)

DATA_DIR = Path(os.environ.get("AUDIT_DATA_DIR", "./data"))

from core.text_extraction import extract_text as _extract_text


# ── 文本降级搜索（ripgrep-all）───────────────────────────────────────────


def _get_kb_search_paths(kb_ids: list[str]) -> list[str]:
    """返回 KB 文档所在目录列表（降级用）。"""
    paths = []
    for kb_id in kb_ids:
        kb = kb_repo.get(kb_id)
        if not kb:
            continue
        kb_dir = DATA_DIR / "kbs" / kb_id / "docs"
        if kb_dir.exists():
            paths.append(str(kb_dir.resolve()))
    return paths


def _run_rga(keyword: str, paths: list[str]) -> str:
    """单个关键词的 ripgrep-all 搜索。"""
    if not keyword or not paths:
        return ""
    try:
        result = subprocess.run(
            ["rga", "-i", "--no-ignore", "--hidden", "-m", "15", "-C", "2", keyword, *paths],
            capture_output=True, text=True, timeout=30,
        )
        return result.stdout.strip()
    except FileNotFoundError:
        # rga not installed — expected in minimal deployments
        return ""
    except Exception as e:
        _logger.warning("rga search failed for keyword %s: %s", keyword, e)
        return ""


def _text_search_fallback(kb_ids: list[str], keywords: list[str]) -> str:
    """向量搜索无结果时的纯文本降级。"""
    paths = _get_kb_search_paths(kb_ids)
    if not paths:
        return ""

    seen = set()
    hits = []
    for kw in keywords:
        snippet = _run_rga(kw, paths)
        if snippet and snippet not in seen:
            seen.add(snippet)
            hits.append(snippet)
        if len(hits) >= 10:
            break

    if not hits:
        # 列出文件作为最后退路
        for root, _, files in os.walk(paths[0]):
            for f in files:
                hits.append(f"文件: {f}")
            break

    content = "\n\n---\n\n".join(hits[:5])
    if content:
        return f"【知识库参考依据（关键词搜索）】\n{content}"
    return ""


def _text_search(paths: list[str], keywords: list[str], max_results: int = 5) -> str:
    """ripgrep-all 纯文本搜索（与旧版 API 兼容）。"""
    if not keywords:
        return ""
    seen = set()
    hits = []
    for kw in keywords:
        snippet = _run_rga(kw, paths)
        if snippet and snippet not in seen:
            seen.add(snippet)
            hits.append(snippet)
        if len(hits) >= max_results:
            break
    return "\n\n---\n\n".join(hits[:max_results])


# ── 向量搜索 ─────────────────────────────────────────────────────────────


def _ensure_kb_index(kb_id: str):
    """确保 KB 索引存在，必要时重建。"""
    if not get_kb_index_built(kb_id):
        _rebuild_store(kb_id)


def vec_search(kb_ids: list[str], query: str, top_k: int = 5) -> list[dict]:
    """向量搜索主干 — 内部调用 LlamaIndex VectorStoreIndex。"""
    if not query or not kb_ids:
        return []
    for kb_id in kb_ids:
        _ensure_kb_index(kb_id)
    return _vec_search(kb_ids, query, top_k)


# ── 文档索引管理（公开 API）───────────────────────────────────────────────


def index_document(kb_id: str, doc_id: str, file_path: str, source_name: str = ""):
    """对单篇 KB 文档分块 + embedding 并写入 FAISS 索引。

    source_name: 来源标签，为空时自动从文件名提取。
    """
    text = _extract_text(file_path)
    if not text or len(text) < 20:
        return
    source_name = source_name or Path(file_path).stem
    _index_to_store(kb_id, doc_id, text, source_name)


def remove_document_index(kb_id: str, doc_id: str):
    """删除 KB 文档的向量索引。"""
    _remove_from_store(kb_id, doc_id)


def rebuild_kb_index(kb_id: str):
    """遍历 KB 全部文档重建向量索引。"""
    _rebuild_store(kb_id)


# ── 搜索接口 ─────────────────────────────────────────────────────────────


def search(kb_ids: list[str], query: str, max_results: int = 5) -> list[dict]:
    """向量搜索（与旧版兼容）。"""
    return vec_search(kb_ids, query, max_results)


def search_by_keywords(kb_ids: list[str], keywords: list[str], topic_name: str = "") -> str:
    """向量搜索 → 低分降级到纯文本。"""
    query = topic_name or " ".join(k for k in keywords if k)[:200]
    results = vec_search(kb_ids, query, top_k=3)
    if results and any(r.get("relevance", 0) > 0.35 for r in results):
        parts = ["【知识库参考依据（向量检索）】"]
        for i, r in enumerate(results, 1):
            label = f"【{r.get('doc_source', '')}】" if r.get("doc_source") else ""
            parts.append(f"\n{i}. {label}\n{r.get('content', '')[:1000]}")
        return "\n".join(parts)
    return _text_search_fallback(kb_ids, keywords or [topic_name])


def get_kb_content(kb_ids: list[str], query: str) -> str:
    """获取格式化 KB 内容（供审核使用）。"""
    results = vec_search(kb_ids, query, top_k=3)
    if not results:
        return "未找到相关标准依据。"
    parts = ["【参考标准依据（向量检索）】"]
    for i, r in enumerate(results, 1):
        label = f"【{r.get('doc_source', '')}】" if r.get("doc_source") else ""
        parts.append(f"\n{i}. {label}\n{r.get('content', '')[:1000]}")
    return "\n".join(parts)
