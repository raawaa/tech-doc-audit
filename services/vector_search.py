"""向量检索服务 — 本地 embedding + cosine similarity。

段落感知分块 → bge-m3 embedding → numpy 余弦相似度 → 稳定快速。

流程：
1. 文档导入 KB 时自动分块（段落感知） + embedding 持久化到 data/kbs/{kb_id}/vectors/
2. 搜索时 embedding query → cosine similarity 召回
3. 纯文本关键词搜索保留为最终降级
"""

import json
import os
import re
import subprocess
import numpy as np
from pathlib import Path
from typing import Optional

import storage.kb_repo as kb_repo

DATA_DIR = Path(os.environ.get("AUDIT_DATA_DIR", "./data"))

# ── Embedding Model 单例（bge-m3，中文优化，CPU 上约 50ms/query）───────

_model = None
_MODEL_NAME = "BAAI/bge-m3"


def _download_model() -> str:
    """从 ModelScope 下载模型，失败则返回模型名让 sentence-transformers 从 HF 拉。"""
    try:
        from modelscope import snapshot_download
        path = snapshot_download(_MODEL_NAME)
        print(f"[vec] 模型已下载到: {path}")
        return path
    except ImportError:
        # modelscope 未安装 → 走 HuggingFace
        return _MODEL_NAME
    except Exception as e:
        print(f"[vec] ModelScope 下载失败 ({e})，切到 HuggingFace")
        return _MODEL_NAME


def _get_model():
    global _model
    if _model is not None:
        return _model
    from sentence_transformers import SentenceTransformer
    model_path = _download_model()
    _model = SentenceTransformer(model_path)
    return _model


# ── 文本分块 ──────────────────────────────────────────────────────────────

# ── 分块参数 ──────────────────────────────────────────────────────────────

MAX_CHUNK_CHARS = 512    # 单个 chunk 最大字符数
MIN_CHUNK_CHARS = 80     # 短 chunk 合并阈值（< 此长度合并到相邻段）
OVERLAP_CHARS = 50       # 相邻 chunk 间重叠字符数


def _chunk_text(text: str) -> list[str]:
    """段落感知分块 — 多遍流水线，保持技术标准条文完整。

    1. 按双换行拆段落（自然段边界）
    2. 合并过短段落（避免碎片）
    3. 过长段落先按单换行拆句，仍超长降级到滑动窗口
    4. 相邻 chunk 间加少量重叠，防止边界处条文编号丢失
    """
    # 1. 按双换行拆段落
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        return [text[:MAX_CHUNK_CHARS]]

    # 2. 合并短段落到下一个
    merged: list[str] = []
    i = 0
    while i < len(paragraphs):
        chunk = paragraphs[i]
        if len(chunk) < MIN_CHUNK_CHARS and i + 1 < len(paragraphs):
            paragraphs[i + 1] = chunk + "\n" + paragraphs[i + 1]
            i += 1
            continue
        merged.append(chunk)
        i += 1

    # 3. 拆分过长 chunk
    refined: list[str] = []
    for chunk in merged:
        if len(chunk) <= MAX_CHUNK_CHARS:
            refined.append(chunk)
            continue
        # 先按单换行拆句
        lines = [line.strip() for line in chunk.split("\n") if line.strip()]
        if not lines:
            refined.append(chunk)
            continue
        current = ""
        for line in lines:
            prospective = (current + "\n" + line).strip() if current else line
            if len(prospective) <= MAX_CHUNK_CHARS:
                current = prospective
            else:
                if current:
                    refined.append(current)
                # 单行仍超长 → 降级滑动窗口
                if len(line) > MAX_CHUNK_CHARS:
                    step = MAX_CHUNK_CHARS - OVERLAP_CHARS
                    refined.extend(
                        line[i : i + MAX_CHUNK_CHARS].strip()
                        for i in range(0, len(line), step)
                        if line[i : i + MAX_CHUNK_CHARS].strip()
                    )
                    current = ""
                else:
                    current = line
        if current:
            refined.append(current)

    # 4. 相邻 chunk 间加重叠
    if not refined:
        return [text[:MAX_CHUNK_CHARS]]
    if len(refined) == 1:
        return refined
    result = [refined[0]]
    for j in range(1, len(refined)):
        prev = refined[j - 1]
        curr = refined[j]
        prefix = prev[-OVERLAP_CHARS:] if len(prev) > OVERLAP_CHARS else prev
        result.append(prefix + "\n" + curr)
    return result




# ── 文本提取（PDF / DOCX → 纯文本）─────────────────────────────────────

_MINERU_BIN: Optional[str] = None


def _find_mineru() -> Optional[str]:
    import shutil
    try:
        path = shutil.which("mineru")
        if path:
            result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                return path
    except Exception:
        pass
    return None


def _mineru_available() -> bool:
    global _MINERU_BIN
    if _MINERU_BIN is None:
        _MINERU_BIN = _find_mineru()
    return _MINERU_BIN is not None


def _extract_text(file_path: str) -> str:
    """从文件提取纯文本用于索引。

    优先使用 MinerU（子进程，内存隔离），降级到 pdfplumber 流式提取。
    """
    ext = Path(file_path).suffix.lower()
    try:
        if ext == ".pdf" and _mineru_available():
            text = _extract_with_mineru(file_path)
            if text:
                return text
        if ext == ".pdf":
            return _extract_pdf_streaming(file_path)
        if ext in (".docx", ".doc"):
            from docx import Document
            parts = [p.text for p in Document(file_path).paragraphs if p.text.strip()]
            return "\n".join(parts) if parts else ""
        return Path(file_path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def _extract_with_mineru(file_path: str) -> str:
    """用 MinerU 解析 PDF（子进程，内存隔离），从 ModelScope 下载模型。"""
    import tempfile
    _bin = _MINERU_BIN or _find_mineru()
    if not _bin:
        return ""
    env = os.environ.copy()
    env["MINERU_MODEL_SOURCE"] = "modelscope"
    with tempfile.TemporaryDirectory() as tmpdir:
        result = subprocess.run(
            [_bin, "-p", file_path, "-o", tmpdir, "-b", "pipeline"],
            capture_output=True, text=True, timeout=600, env=env,
        )
        if result.returncode != 0:
            return ""
        base = Path(file_path).stem
        candidates = list(Path(tmpdir).rglob(f"{base}.md"))
        if not candidates:
            return ""
        text = candidates[0].read_text(encoding="utf-8", errors="ignore")
        return text if len(text) > 20 else ""


def _extract_pdf_streaming(file_path: str) -> str:
    """流式提取 PDF 文本，每 50 页 flush 一次释放内存。"""
    import pdfplumber
    parts = []
    with pdfplumber.open(file_path) as pdf:
        for i, page in enumerate(pdf.pages):
            t = page.extract_text() or ""
            if t:
                parts.append(t)
            page.flush_cache()
            if i > 0 and i % 50 == 0:
                parts = ["\n\n".join(parts)]
    return "\n\n".join(parts) if parts else ""


# ── 向量索引管理 ──────────────────────────────────────────────────────────

def _vec_dir(kb_id: str) -> Path:
    return DATA_DIR / "kbs" / kb_id / "vectors"


def _ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)


def _idx_file(kb_id: str) -> Path:
    return _vec_dir(kb_id) / "indexes.json"


def _load_idx(kb_id: str) -> dict:
    f = _idx_file(kb_id)
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    return {"docs": []}


def _save_idx(kb_id: str, idx: dict):
    _ensure_dir(_vec_dir(kb_id))
    _idx_file(kb_id).write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")


def _index_is_built(kb_id: str) -> bool:
    idx = _load_idx(kb_id)
    return len(idx.get("docs", [])) > 0


def index_document(kb_id: str, doc_id: str, file_path: str, source_name: str = ""):
    """对单篇 KB 文档分块 + embedding 并持久化。

    source_name: 来源标签，为空时自动从文件名提取。
    """
    text = _extract_text(file_path)
    if not text or len(text) < 20:
        return
    chunks_text = _chunk_text(text)
    source_name = source_name or Path(file_path).stem
    chunks_sources = [source_name] * len(chunks_text)

    model = _get_model()
    embs = model.encode(chunks_text, normalize_embeddings=True, show_progress_bar=False)

    d = _vec_dir(kb_id)
    _ensure_dir(d)
    (d / f"{doc_id}_chunks.json").write_text(
        json.dumps({
            "doc_id": doc_id,
            "file_path": file_path,
            "chunks": chunks_text,
            "sources": chunks_sources,
        }, ensure_ascii=False),
        encoding="utf-8")
    np.save(str(d / f"{doc_id}_emb.npy"), embs)

    idx = _load_idx(kb_id)
    if doc_id not in idx["docs"]:
        idx["docs"].append(doc_id)
    _save_idx(kb_id, idx)


def remove_document_index(kb_id: str, doc_id: str):
    """删除 KB 文档的向量索引。"""
    d = _vec_dir(kb_id)
    for name in [f"{doc_id}_chunks.json", f"{doc_id}_emb.npy"]:
        p = d / name
        if p.exists():
            p.unlink()
    idx = _load_idx(kb_id)
    idx["docs"] = [did for did in idx["docs"] if did != doc_id]
    _save_idx(kb_id, idx)


def rebuild_kb_index(kb_id: str):
    """遍历 KB 全部文档重建向量索引。"""
    kbs = kb_repo.list_all()
    kb = next((k for k in kbs if k.id == kb_id), None)
    if not kb:
        return
    for doc_id in kb.document_ids:
        from storage.doc_repo import get_doc
        doc = get_doc(kb_id, doc_id)
        if doc and doc.file_path and Path(doc.file_path).exists():
            try:
                index_document(kb_id, doc_id, doc.file_path)
            except Exception as e:
                print(f"  [skip] {doc_id}: {e}")


# ── 向量搜索 ─────────────────────────────────────────────────────────────

_SEARCH_SIMILARITY_THRESHOLD = 0.2


def vec_search(kb_ids: list[str], query: str, top_k: int = 5) -> list[dict]:
    """向量搜索主干。"""
    if not query or not kb_ids:
        return []
    model = _get_model()
    q_emb = model.encode(query, normalize_embeddings=True, show_progress_bar=False)

    hits = []
    for kb_id in kb_ids:
        if not _index_is_built(kb_id):
            rebuild_kb_index(kb_id)
            if not _index_is_built(kb_id):
                continue
        idx = _load_idx(kb_id)
        d = _vec_dir(kb_id)
        for doc_id in idx["docs"]:
            emb_file = d / f"{doc_id}_emb.npy"
            chunk_file = d / f"{doc_id}_chunks.json"
            if not emb_file.exists() or not chunk_file.exists():
                continue
            embs = np.load(str(emb_file))
            scores = embs @ q_emb  # cosine similarity（已 normalize）
            best = np.argsort(-scores)[:top_k]
            chunks_data = json.loads(chunk_file.read_text(encoding="utf-8"))
            for i in best:
                if float(scores[i]) < _SEARCH_SIMILARITY_THRESHOLD:
                    continue
                src_label = (chunks_data.get("sources") or [""] * len(chunks_data["chunks"]))[i]
                hits.append({
                    "source": "vec_search",
                    "kb_id": kb_id,
                    "doc_id": doc_id,
                    "content": chunks_data["chunks"][i],
                    "doc_source": src_label,
                    "relevance": round(float(scores[i]), 4),
                })

    hits.sort(key=lambda x: -x["relevance"])
    return hits[:top_k]


# ── 纯文本搜索（降级）────────────────────────────────────────────────────

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


def _text_search(paths: list[str], keywords: list[str], max_results: int = 5) -> str:
    """ripgrep-all 纯文本搜索（降级）。"""
    if not keywords:
        return ""
    seen = set()
    hits = []
    for kw in keywords:
        for p in paths:
            try:
                result = subprocess.run(
                    ["rga", "-i", "--no-ignore", "--hidden", "-m", "15", "-C", "2", kw, p],
                    capture_output=True, text=True, timeout=30,
                )
                snippet = result.stdout.strip()
                if snippet and snippet not in seen:
                    seen.add(snippet)
                    hits.append(snippet)
            except Exception:
                pass
            if len(hits) >= max_results * 2:
                break
        if len(hits) >= max_results * 2:
            break
    if not hits:
        for p in paths:
            for root, dirs, files in os.walk(p):
                for f in files:
                    hits.append(f"文件: {f}")
                break
        return "\n".join(hits[:max_results])
    return "\n\n---\n\n".join(hits[:max_results])


def _text_search_fallback(kb_ids: list[str], keywords: list[str]) -> str:
    """向量搜索无结果时的纯文本降级。"""
    paths = _get_kb_search_paths(kb_ids)
    if not paths:
        return ""
    content = _text_search(paths, keywords)
    if content:
        return f"【知识库参考依据（关键词搜索）】\n{content}"
    return ""


# ═══════════════════════════════════════════════════════════════════════════
# 公共接口
# ═══════════════════════════════════════════════════════════════════════════


def search(kb_ids: list[str], query: str, max_results: int = 5) -> list[dict]:
    """向量搜索。"""
    return vec_search(kb_ids, query, max_results)


def search_by_keywords(kb_ids: list[str], keywords: list[str], topic_name: str = "") -> str:
    """向量搜索 → 低分降级到纯文本。"""
    query = topic_name or " ".join(k for k in keywords if k)[:200]
    results = vec_search(kb_ids, query, top_k=3)
    if results and any(r["relevance"] > 0.35 for r in results):
        parts = ["【知识库参考依据（向量检索）】"]
        for i, r in enumerate(results, 1):
            label = f"【{r['doc_source']}】" if r.get("doc_source") else ""
            parts.append(f"\n{i}. {label}\n{r['content'][:1000]}")
        return "\n".join(parts)
    return _text_search_fallback(kb_ids, keywords or [topic_name])


def get_kb_content(kb_ids: list[str], query: str) -> str:
    """获取格式化 KB 内容（供审核使用）。"""
    results = vec_search(kb_ids, query, top_k=3)
    if not results:
        return "未找到相关标准依据。"
    parts = ["【参考标准依据（向量检索）】"]
    for i, r in enumerate(results, 1):
        label = f"【{r['doc_source']}】" if r.get("doc_source") else ""
        parts.append(f"\n{i}. {label}\n{r['content'][:1000]}")
    return "\n".join(parts)
