"""向量检索服务 — LlamaIndex VectorStoreIndex + FAISS。

流程：
1. 文档导入 KB 时自动分块（SentenceSplitter） + embedding（bge-m3）写入 FAISS 索引
2. 搜索时 embedding query → FAISS ANN 召回
3. 纯文本关键词搜索保留为最终降级

与旧版 numpy 暴力搜索保持相同公开 API，内部改用 LlamaIndex。
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import storage.kb_repo as kb_repo
from core.index_manager import (
    search as _vec_search,
    index_document as _index_to_store,
    remove_document as _remove_from_store,
    rebuild_kb_index as _rebuild_store,
    get_kb_index_built,
    _get_index_lock,
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
    """单个关键词的文本搜索。优先 rga（支持 PDF/DOCX），降级到 rg（纯文本）。"""
    if not keyword or not paths:
        return ""
    # 优先 rga（ripgrep-all，支持二进制文件格式）
    rga_bin = shutil.which("rga")
    if rga_bin:
        try:
            result = subprocess.run(
                [rga_bin, "-i", "--no-ignore", "--hidden", "-n", "-m", "15", "-C", "2", keyword, *paths],
                capture_output=True, text=True, timeout=30,
            )
            if result.stdout.strip():
                return result.stdout.strip()
        except Exception as e:
            _logger.warning("rga search failed for keyword %s: %s", keyword, e)

    # 降级到 rg（ripgrep，搜索纯文本文件如 .md / .txt）
    rg_bin = shutil.which("rg")
    if rg_bin:
        try:
            result = subprocess.run(
                [rg_bin, "-i", "--no-ignore", "-n", "-m", "10", "-C", "3",
                 "-g", "*.md", "-g", "*.txt", "-g", "*.markdown",
                 keyword, *paths],
                capture_output=True, text=True, timeout=15,
            )
            return result.stdout.strip()
        except Exception as e:
            _logger.warning("rg search failed for keyword %s: %s", keyword, e)

    from core.degradation import record as _deg_record
    _deg_record("vector_search", "text_search_unavailable",
                 f"No text search tool (rga/rg) available for keyword: {keyword}")
    _logger.warning("no text search tool available for keyword: %s", keyword)
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
        # 最终退路：直接从 KB 文档中读取前几个文件的内容片段
        from core.degradation import record as _deg_record
        _deg_record("vector_search", "text_search_empty",
                     "No text search hits, reading raw file snippets as last resort")
        for root, _, files in os.walk(paths[0]):
            for f in files[:5]:
                fpath = os.path.join(root, f)
                try:
                    snippet = Path(fpath).read_text(encoding="utf-8", errors="ignore")[:500]
                    if snippet.strip():
                        hits.append(f"文件: {f}\n{snippet}")
                except Exception:
                    pass
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
    """确保 KB 索引存在，必要时重建（线程安全）。"""
    if get_kb_index_built(kb_id):
        return
    # 在 per-KB 锁内二次检查，避免多个线程同时触发冗余重建
    with _get_index_lock(kb_id):
        if not get_kb_index_built(kb_id):
            _rebuild_store(kb_id)


def vec_search(kb_ids: list[str], query: str, top_k: int = 5, rebuild_if_missing: bool = True) -> list[dict]:
    """向量搜索主干 — 内部调用 LlamaIndex VectorStoreIndex。

    rebuild_if_missing: 索引文件不存在时是否自动重建。QA 请求应设为 False
                        以避免同步重建阻塞 HTTP 线程。
    """
    if not query or not kb_ids:
        return []
    for kb_id in kb_ids:
        if rebuild_if_missing:
            _ensure_kb_index(kb_id)
    return _vec_search(kb_ids, query, top_k)


# ── 文档索引管理（公开 API）───────────────────────────────────────────────


def index_document(kb_id: str, doc_id: str, file_path: str, source_name: str = "",
                   page_texts: list[str] | None = None):
    """对单篇 KB 文档分块 + embedding 并写入 FAISS 索引。

    source_name: 来源标签，为空时自动从文件名提取。
    page_texts: 逐页文本列表（page_texts[0] = 第1页），用于按页创建 chunk 以保留页码信息。
    """
    text = _extract_text(file_path)
    if not text or len(text) < 20:
        return
    source_name = source_name or Path(file_path).stem
    _index_to_store(kb_id, doc_id, text, source_name, page_texts=page_texts)


def remove_document_index(kb_id: str, doc_id: str):
    """删除 KB 文档的向量索引。"""
    _remove_from_store(kb_id, doc_id)


def rebuild_kb_index(kb_id: str, progress_callback=None):
    """遍历 KB 全部文档重建向量索引。"""
    _rebuild_store(kb_id, progress_callback)


# ── 搜索接口 ─────────────────────────────────────────────────────────────


def search(kb_ids: list[str], query: str, max_results: int = 5, rebuild_if_missing: bool = True) -> list[dict]:
    """向量搜索（与旧版兼容）。"""
    return vec_search(kb_ids, query, max_results, rebuild_if_missing=rebuild_if_missing)


def _format_kb_results(results: list[dict], prefix: str = "知识库参考依据（向量检索）") -> str:
    """统一格式化 KB 向量搜索结果（用于注入 LLM prompt）。

    格式示例：
    【知识库参考依据】
    1. 【CJJ101-2016】第 3.2.1 条
       原文内容...

    2. 【GB/T XXXX】第 5.2 条
       原文内容...
    """
    if not results:
        return ""
    parts = [f"【{prefix}】"]
    for i, r in enumerate(results, 1):
        doc_label = r.get("doc_source", "")
        clause = r.get("clause_number", "")
        section = r.get("section_path", "")
        label_parts = []
        if doc_label:
            label_parts.append(f"【{doc_label}】")
        if clause:
            label_parts.append(f"第{clause}条")
        if section and not clause:
            label_parts.append(section)
        label = " ".join(label_parts) if label_parts else ""
        parts.append(f"\n{i}. {label}\n{r.get('content', '')[:1000]}")
    return "\n".join(parts)


def search_by_keywords(kb_ids: list[str], keywords: list[str], topic_name: str = "") -> str:
    """向量搜索 → 低分降级到纯文本。"""
    query = topic_name or " ".join(k for k in keywords if k)[:200]
    results = vec_search(kb_ids, query, top_k=6)
    if results and any(r.get("relevance", 0) > 0.35 for r in results):
        return _format_kb_results(results)
    return _text_search_fallback(kb_ids, keywords or [topic_name])


def get_kb_content_for_audit(kb_ids: list[str], clause_text: str) -> str:
    """获取相关知识库内容用于审核分析。"""
    try:
        return get_kb_content(kb_ids, clause_text)
    except Exception as e:
        _logger.warning("vector kb content failed: %s", e)
        return "未找到相关标准依据。"


def get_kb_content(kb_ids: list[str], query: str) -> str:
    """获取格式化 KB 内容（供审核使用）。"""
    results = vec_search(kb_ids, query, top_k=3)
    if not results:
        return "未找到相关标准依据。"
    return _format_kb_results(results, prefix="参考标准依据（向量检索）")


def search_doc_by_text(keyword: str, kb_ids: list[str]) -> list[dict]:
    """用 rga 精确搜索 KB 文档原文，返回结构化结果。

    适用于搜索标准编号（如 GB/T 20145-2006）等在文档正文中
    精确出现的字符串。不依赖文件名，搜的是文档内容。

    Returns:
        [{doc_id, page_number, content}]
        page_number 为 None 表示无法确定页码（非 PDF 或解析失败）。
    """
    if not keyword or not kb_ids:
        return []

    paths = _get_kb_search_paths(kb_ids)
    if not paths:
        return []

    raw = _run_rga(keyword, paths)
    if not raw:
        return []

    # 构建 file_path -> (kb_id, doc_id) 映射
    import storage.doc_repo as doc_repo
    file_to_doc: dict[str, tuple[str, str]] = {}  # resolved_path -> (kb_id, doc_id)
    for kb_id in kb_ids:
        for doc in doc_repo.list_docs(kb_id):
            fp = str(Path(doc.file_path).resolve())
            file_to_doc[fp] = (kb_id, doc.id)

    # 解析 rga 输出，提取文件路径和匹配行
    # rga 输出格式 (with -n): /path/to/file:123:text (match), /path/to/file-123-text (context)
    # 注意：文件路径可能包含 - 字符，不能直接用简单正则从上下文行解析
    # 改为：用已知文件路径列表来匹配行首
    # 优先使用匹配行（:分隔符），无匹配行时才用上下文行
    hits: list[dict] = []
    # doc_id -> best_content，优先存匹配行的内容
    doc_best: dict[str, dict] = {}

    # 预计算文件路径映射，用于逐行匹配
    resolved_fps: set[str] = set(file_to_doc.keys())

    for line in raw.split("\n"):
        if not line or line == "--":
            continue

        # 找出匹配哪个已知文件路径
        matched_fp = None
        for fp in resolved_fps:
            if line.startswith(fp):
                matched_fp = fp
                break

        if matched_fp is None:
            continue

        suffix = line[len(matched_fp):]
        if not suffix:
            continue

        # 分隔符可以是 :（匹配行）或 -（上下文行）
        sep = suffix[0]
        if sep not in (":", "-"):
            continue
        rest = suffix[1:]

        # 提取行号和内容
        # 格式: line_num:text 或 line_num-text
        line_match = re.match(r"^(\d+)[:\-](.*)", rest)
        if not line_match:
            continue

        is_match_line = sep == ":"
        text = line_match.group(2).strip()

        kb_id, doc_id = file_to_doc[matched_fp]

        # 已有该 doc_id 的条目：仅当当前是匹配行且之前是上下文行时替换
        if doc_id in doc_best:
            prev = doc_best[doc_id]
            if is_match_line and not prev.get("is_match_line"):
                doc_best[doc_id] = {
                    "doc_id": doc_id,
                    "kb_id": kb_id,
                    "page_number": None,
                    "content": text[:500],
                    "is_match_line": True,
                }
            continue

        doc_best[doc_id] = {
            "doc_id": doc_id,
            "kb_id": kb_id,
            "page_number": None,
            "content": text[:500],
            "is_match_line": is_match_line,
        }

    # 从 doc_best 转成 hits，只保留对外字段
    for entry in doc_best.values():
        hits.append({
            "doc_id": entry["doc_id"],
            "kb_id": entry["kb_id"],
            "page_number": entry["page_number"],
            "content": entry["content"],
        })

    return hits[:5]
