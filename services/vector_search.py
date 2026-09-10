"""向量检索服务 — LlamaIndex VectorStoreIndex + FAISS。

流程：
1. 文档导入 KB 时自动分块（SentenceSplitter） + embedding（bge-m3）写入 FAISS 索引
2. 搜索时 embedding query → FAISS ANN 召回
3. 纯文本关键词搜索走 ``core.pages_store``（V5 #29）：从 pages/{doc_id}.json 内存 grep 取 page_number

与旧版 numpy 暴力搜索保持相同公开 API，内部改用 LlamaIndex。
"""

import os
import re
import threading
import unicodedata
from pathlib import Path

from llama_index.core import Settings
from llama_index.core.schema import NodeWithScore

import storage.kb_repo as kb_repo
from core.kb_index_status import KbIndexStatusWriter, get_kb_index_built
from core.kb_index_store import KBIndexStore
from core.kb_index_writer import Doc, KBIndexWriter
from core.logger import get_logger
from core.pages_store import load_pages

_logger = get_logger(__name__)


# #27 容错匹配用的剥离集：空白 + ``-`` + ``.`` + ``/``。
# 这四类字符在标准编号里常被互换或漏写（``IEC61547`` / ``IEC 61547``、
# ``GB 7000-.202`` / ``GB 7000.202``、``GBT 20145`` / ``GB/T 20145``）。
# ``/`` 表达"推荐"语义（``GB/T``），但归一化后 ``GB/T`` ≡ ``GBT`` ——
# 既然两边同进归一化，剥掉不会引入误匹配；区分度仍由数字部分承担。
_STD_NORM_STRIP_RE = re.compile(r"[\s　 \-./]+")

# 常见标准前缀集合（与 ``standard_linker._STD_PREFIX_RE`` 共用同一张表），
# 用于 #27 兜底匹配的开关判断。
_STD_PREFIX_NAMES = (
    "IEC", "ISO", "CIE", "GB", "CJJ", "JGJ", "JG",
    "BS", "EN", "NF", "DIN", "JIS", "KS", "TJ",
)
_STD_PREFIX_GROUP = "|".join(_STD_PREFIX_NAMES)
# 前缀与首数字粘连（``IEC61547`` ↔ ``IEC 61547``）→ 启用兜底
_STD_PREFIX_NO_SEP_RE = re.compile(
    rf"^(?:{_STD_PREFIX_GROUP})\d",
    re.IGNORECASE,
)


def _normalize_for_standard_match(s: str) -> str:
    """标准编号容错匹配用的归一化串：NFKC + casefold + 去空白/``-``/``.``/``/``。

    与 ``core.text_norm.norm()`` 的区别：本函数**只**剥离空白与 ``-`` / ``.`` / ``/``，
    不动其它字符 —— chunk↔block 匹配的 ``norm()`` 会把所有标点（含中英括号、
    书名号等）一并吃掉，对标准编号来说过激进。``GB/T 20145-2006`` 经本函数
    归一化后是 ``gbt201452006``，``IEC 61547`` 是 ``iec61547``，
    ``GB 7000.202`` 是 ``gb7000202``。

    用于 #27 标准编号格式不匹配（``IEC61547`` ↔ ``IEC 61547``、
    ``GB 7000-.202`` ↔ ``GB 7000.202``）的兜底匹配。
    """
    if not s:
        return ""
    return _STD_NORM_STRIP_RE.sub("", unicodedata.normalize("NFKC", s).casefold())


def _pages_search_doc(keyword: str, kb_ids: list[str], *, max_hits: int = 5) -> list[dict]:
    """遍历所有 KB 的 pages/{doc_id}.json，对每页文本做大小写不敏感的 ``str.find``。

    #27 容错匹配：精确 ``str.find`` 无命中时,退化到归一化匹配
    （needle 与 page text 都过 ``_normalize_for_standard_match``）—— 解决
    ``IEC61547`` ↔ ``IEC 61547``、``GB 7000-.202`` ↔ ``GB 7000.202`` 等格式
    不匹配场景。归一化匹配仅作为兜底,精确路径仍走原大小写不敏感匹配;
    snippet 兜底路径上按 ``len(raw_text)/len(page_norm)`` 比例把归一化坐标
    缩回原文坐标,``±200`` 窗口足以覆盖标准编号的展示。

    Args:
        keyword: 待搜索字符串。
        kb_ids: 限定 KB 列表；空列表 = 不过滤。
        max_hits: 返回最多多少条命中（per doc 取首个命中页）。

    Returns:
        ``[{doc_id, kb_id, page_number, content}]``。
        - ``page_number`` 是 0-based，命中页；找不到页则 None。
        - ``content`` 是该页含关键词的段落（截 500 字符）。
    """
    if not keyword or not kb_ids:
        return []

    target_kbs = [kb_id for kb_id in kb_ids if kb_id]
    if not target_kbs:
        return []

    needle = keyword.lower()
    needle_norm = _normalize_for_standard_match(keyword)
    # 容错匹配兜底：仅当精确匹配失败才付归一化开销。启用条件：
    # - needle 本身含可剥离字符（空白 / ``-`` / ``.``）；或
    # - needle 形似标准编号且前缀与首数字之间缺分隔（``IEC61547`` ↔ ``IEC 61547``）。
    # 正常路径只多算一次 needle 归一化。
    has_strippable = bool(_STD_NORM_STRIP_RE.search(keyword))
    looks_like_std_no_sep = (
        not has_strippable
        and bool(needle_norm)
        and _STD_PREFIX_NO_SEP_RE.match(keyword) is not None
    )
    fallback_enabled = bool(needle_norm) and (has_strippable or looks_like_std_no_sep)
    fallback_needle = needle_norm if fallback_enabled else ""

    hits: list[dict] = []

    for kb_id in target_kbs:
        kb = kb_repo.get(kb_id)
        if not kb:
            continue
        for doc_id in (kb.document_ids or []):
            pages = load_pages(kb_id, doc_id)
            if not pages:
                continue
            by_page = pages.get("by_page") or []
            for entry in by_page:
                page = entry.get("page")
                raw_text = entry.get("text") or ""
                page_text = raw_text.lower()
                idx = page_text.find(needle)
                # 精确命中失败 → 走归一化兜底
                if idx == -1 and fallback_needle:
                    page_norm = _normalize_for_standard_match(raw_text)
                    norm_idx = page_norm.find(fallback_needle)
                    if norm_idx == -1:
                        continue
                    # 把归一化坐标按比例缩回原文坐标 —— 原文 / 归一化串 的
                    # 长度比 = 单位字符对应的原文字符数。snippet 是 ``±200`` 宽
                    # 窗口,几十字符内的偏移不影响展示标准编号。
                    ratio = len(raw_text) / max(len(page_norm), 1)
                    idx = int(norm_idx * ratio)
                elif idx == -1:
                    continue
                # 取上下文窗口（取原始字符串中含命中处左右 200 字符）
                lo = max(0, idx - 80)
                hi = min(len(raw_text), idx + len(keyword) + 200)
                snippet = raw_text[lo:hi]
                hits.append({
                    "doc_id": doc_id,
                    "kb_id": kb_id,
                    "page_number": page,  # 0-based
                    "content": snippet.strip()[:500],
                })
                break  # 每个 doc 仅取首个命中页
            if len(hits) >= max_hits:
                return hits
    return hits


def _text_search_fallback(kb_ids: list[str], keywords: list[str]) -> str:
    """向量搜索无结果时的纯文本降级（V5：pages/{doc_id}.json grep，不再依赖 rga）。"""
    hits: list[str] = []
    seen: set[str] = set()
    for kw in keywords or []:
        for entry in _pages_search_doc(kw, kb_ids):
            chunk = (
                f"【{entry['kb_id']} / doc={entry['doc_id']} / page={entry['page_number']}】\n"
                f"{entry['content']}"
            )
            if chunk in seen:
                continue
            seen.add(chunk)
            hits.append(chunk)
            if len(hits) >= 5:
                break
        if len(hits) >= 5:
            break
    if not hits:
        return ""
    body = "\n\n---\n\n".join(hits)
    return f"【知识库参考依据（关键词搜索）】\n{body}"


def _all_docs_have_vectors(kb_id: str) -> bool:
    """检查该 KB 关联的所有文档是否都有 .npy 向量缓存（fast path 判定）。"""
    kb = kb_repo.get(kb_id)
    if kb is None or not kb.document_ids:
        return False
    vectors_dir = KBIndexStore.open(kb_id).vectors_dir
    return all((vectors_dir / f"{did}.npy").exists() for did in kb.document_ids)


def _ensure_kb_index(kb_id: str, sync_rebuild_for_audit: bool = False) -> bool:
    """确保 KB 索引可检索（按 ADR-0002 §3 分层）。

    快路（fast path）：所有文档 .npy 缓存齐全 → 同步重建（秒级、纯 CPU）
    慢路（slow path）：有文档缺向量（需 GPU 重算） → 按调用方意图：
      - ``sync_rebuild_for_audit=True``（审核路径）：同步阻塞重建
      - ``sync_rebuild_for_audit=False``（问答默认）：后台异步重建，
        当前调用立即返回 False，让调用方走文本降级 / 轮询

    Returns:
        True if ``kb.index_status`` 可被当前调用视作 'searchable'；
        False 表示仍在 'building' 或重建失败，调用方应降级或等待。

    重建写回字段由 ``KBIndexWriter.rebuild_kb_index`` 按内置契约完成
    （ADR-0002 §决策 2），本函数不重复写。
    """
    if get_kb_index_built(kb_id):
        return True

    # 在 per-KB 锁内二次检查 + 触发重建
    # Issue #168: per-KB 锁已被 ``KBIndexStore`` 内部封装;外部不能直接拿到
    # RLock 对象,只能通过 ``KBIndexStore.acquire_write_lock()`` 拿一个
    # contextmanager —— 这是 issue #168 AC #3 "no external symbol exposes
    # the lock" 的兑现。
    with KBIndexStore.open(kb_id).acquire_write_lock():
        if get_kb_index_built(kb_id):
            return True  # 双检：另一线程可能刚完成

        writer = KBIndexWriter(kb_id)
        if _all_docs_have_vectors(kb_id):
            # 快路：秒级同步重建
            kb_writer = KbIndexStatusWriter(kb_id)
            kb_writer.begin()
            writer.rebuild_kb_index()
            return get_kb_index_built(kb_id)

        # 慢路：缺向量。按调用方意图决定同步 / 异步
        if sync_rebuild_for_audit:
            kb_writer = KbIndexStatusWriter(kb_id)
            kb_writer.begin()
            writer.rebuild_kb_index()  # 同步：宁可请求挂几分钟也不让审核缺向量
            return get_kb_index_built(kb_id)

        # 异步：QA 默认。当前请求立即返回 False，让 QA 走文本降级
        def _async_rebuild():
            kb_writer = KbIndexStatusWriter(kb_id)
            kb_writer.begin()
            writer.rebuild_kb_index()

        thread = threading.Thread(target=_async_rebuild, daemon=True)
        thread.start()
        return False


def _cross_kb_search(
    kb_ids: list[str], query: str, top_k: int = 5, use_reranker: bool = True,
) -> list[dict]:
    """跨 KB 向量搜索(issue #171 / PR-4:从 ``core.index_manager.search`` 迁入)。

    返回格式与旧版 ``vec_search()`` 兼容:
    ``[{source, kb_id, doc_id, content, doc_source, relevance, page_number, block_range}, ...]``

    当 reranker 可用时,用 cross-encoder 对候选结果重排序提升精度。

    拆分理由(issue #165):原函数与 ``KBIndexStore.search``(单 KB)、``search``
    在同一文件、且把"读也持锁 + 跨 KB 聚合 + reranker + 格式化为 hit dict"四
    件不同理由变化的事揉在一起。读路径是 service 责任(决策要不要重排 / 决定
    返回形状),放进 ``services.vector_search`` 让 caller 拿到的就是"已
    reranker、已格式化"的最终结果。
    """
    if not query or not kb_ids:
        return []

    from core.settings import get_embed_model, get_gpu_inference_lock, run_reranker
    get_embed_model()

    # 一次 query embedding(原代码在每个 KB retriever.retrieve 内各做一次,
    # 现在提到 search 入口做一次——同一 embedder、同一 query、同一结果,
    # 跨 KB 复用)。Query embedding 不走 ``embed_batch_with_retry``——
    # ADR-0007 §2:查询路径零附加重试。
    query_embedding = Settings.embed_model.get_query_embedding(query)

    gpu_lock = get_gpu_inference_lock()

    with gpu_lock:
        all_nodes: list[NodeWithScore] = []
        for kb_id in kb_ids:
            if not get_kb_index_built(kb_id):
                continue
            try:
                nodes = KBIndexStore.open(kb_id).search(query_embedding, top_k)
                for node in nodes:
                    node.node.metadata["kb_id"] = kb_id
                    all_nodes.append(node)
            except Exception as e:
                _logger.warning("vector search failed for kb %s: %s", kb_id, e)
                continue

        if not all_nodes:
            return []

        all_nodes.sort(key=lambda n: n.score or 0, reverse=True)
        all_nodes = all_nodes[: top_k * 2]

        if use_reranker:
            try:
                reranked = run_reranker(all_nodes, query)
                if reranked:
                    all_nodes = reranked
            except Exception as e:
                _logger.warning("reranker failed in search, using raw ranking: %s", e)

    hits = []
    for node in all_nodes[:top_k]:
        meta = node.metadata or {}
        hits.append({
            "source": "vec_search",
            "kb_id": meta.get("kb_id", ""),
            "doc_id": meta.get("doc_id", ""),
            "content": node.text,
            "doc_source": meta.get("source", ""),
            "section_path": meta.get("section_path", ""),
            "clause_number": meta.get("clause_number", ""),
            "page_number": meta.get("page_number"),
            "block_range": meta.get("block_range"),
            "relevance": round(node.get_score() or 0, 4),
        })

    return hits


def vec_search(
    kb_ids: list[str],
    query: str,
    top_k: int = 5,
    rebuild_if_missing: bool = True,
    sync_rebuild_for_audit: bool = False,
) -> list[dict]:
    """向量搜索主干 — 内部调用 LlamaIndex VectorStoreIndex。

    Args:
        rebuild_if_missing: 索引不在 'searchable' 状态时是否自动重建。
                            False 时直接返回空（用于 QA 走文本降级）。
        sync_rebuild_for_audit: True 时慢路也阻塞同步（审核质量优先）；
                                 False 时慢路异步降级（QA 默认，避免阻塞）。
    """
    if not query or not kb_ids:
        return []
    for kb_id in kb_ids:
        if rebuild_if_missing:
            _ensure_kb_index(kb_id, sync_rebuild_for_audit=sync_rebuild_for_audit)
    return _cross_kb_search(kb_ids, query, top_k)


# ── 文档索引管理（公开 API）───────────────────────────────────────────────


def index_document(kb_id: str, doc_id: str, file_path: str, source_name: str = "",
                   by_page=None):
    """对单篇 KB 文档分块 + embedding 并写入 FAISS 索引（V6 走 parse_document）。

    source_name: 来源标签，为空时自动从文件名提取。
    by_page: ``ParseResult.by_page`` 同构（list[PageText]）。若 None，则
        ``parse_document`` 内部解析以获得 by_page（pages 文件入口路径）。

    V8-S2 增 by_layout 透传：parse_result.layout 传给底层 ``KBIndexWriter``,
    让 ``_inject_block_range`` 能为每个 chunk 写入 block_range。非 PDF KB
    (layout=[]) → block_range 全 None,走 fallback 高亮。
    """
    from core.parse_document import parse_document as _parse_document

    parse_result = _parse_document(file_path)
    text = parse_result.full_text
    if not text or len(text) < 20:
        return
    src = source_name or Path(file_path).stem
    # V6: by_page 来自 parse_result（pages 文件已落地，kb_files / reparse 共用一份）
    # V8-S2: by_layout 同样透传,让 chunk → block 区间自动落到 metadata
    KBIndexWriter(kb_id).index_documents([Doc(
        doc_id=doc_id,
        text=text,
        source_name=src,
        by_page=by_page if by_page is not None else parse_result.by_page,
        by_layout=parse_result.layout,
    )])


def remove_document_index(kb_id: str, doc_id: str):
    """删除 KB 文档的向量索引。"""
    KBIndexStore.open(kb_id).remove_doc(doc_id)


def rebuild_kb_index(kb_id: str, progress_callback=None):
    """遍历 KB 全部文档重建向量索引。

    走 ``KBIndexWriter.rebuild_kb_index``:调用方已 begin() 过(issue #155),
    本函数内不 begin()。
    """
    writer = KBIndexWriter(kb_id)
    kb_writer = KbIndexStatusWriter(kb_id)
    kb_writer.begin()
    writer.rebuild_kb_index(progress_callback=progress_callback)


# ── 搜索接口 ─────────────────────────────────────────────────────────────


def search(kb_ids: list[str], query: str, top_k: int = 5, rebuild_if_missing: bool = True) -> list[dict]:
    """向量搜索(issue #171 / PR-4:取代 ``core.index_manager.search`` 的位置)。

    与旧版兼容,返回 hit dict 列表。``top_k`` 取代 ``max_results`` 以对齐
    ``vec_search`` 形参(也便于 tests 直接传 ``top_k=N``,无需翻译)。
    """
    return vec_search(kb_ids, query, top_k, rebuild_if_missing=rebuild_if_missing)


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
    """精确文本搜索 KB 文档原文（V5 #29）。

    适用于搜索标准编号（如 ``GB/T 20145-2006``）等在文档正文中精确出现的字符串。
    走 ``pages/{doc_id}.json`` 内存 grep：大小写不敏感，命中页即返回 page_number（0-based）。

    Returns:
        ``[{doc_id, kb_id, page_number, content}]``。``page_number`` 为 None 表示该 KB
        没有 pages 文件（旧数据，V6 之前不会发生回填；调用方应按"无法跳转"处理）。
    """
    return _pages_search_doc(keyword, kb_ids)
