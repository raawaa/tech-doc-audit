"""Agent 共享的 KB 搜索工具实现。

审核（agentic_audit）与问答（agentic_qa）共用的两个知识库检索工具：
- search_kb：语义向量搜索（FAISS + bge-m3，via vec_search）
- search_kb_text：精确文本搜索（ripgrep-all，via _run_rga）

read_chapter / flag_issue 是审核文档域、仅审核用，留在 agentic_audit.py，不在此处。

V9 PRD #67：search_kb 输出格式被 SSE 层（api/routers/qa.py）inline 解析，
本模块还提供 `parse_search_kb_tool_output` 作为该格式的事实定义，
agentic_qa._extract_sources 与 qa._parse_tool_sources 都从这里导入。
"""
import re

from core.logger import get_logger

_logger = get_logger(__name__)


# ── V9 PRD #67 — search_kb 输出格式（事实定义）────────────────────────────────
# search_kb 工具返回的格式化文本（见本文件下方 search_kb 实现），以下正则
# 解析该格式。文本按"块（block）"组织：每个编号条目对应一条检索结果，块内
# 包含【名称】、相关度、doc_id、页码（1-based）、block_range（可选）、正文。
# 任何修改都会同时影响 SSE 层（qa.py）与 agentic_qa._extract_sources 的解析。

_SEARCH_KB_BLOCK_START = re.compile(r"^(\d+)\.\s+(.*)$")
_SEARCH_KB_NAME = re.compile(r"【(.+?)】")
_SEARCH_KB_RELEVANCE = re.compile(r"相关度:\s*([\d.]+)")
_SEARCH_KB_DOC_ID = re.compile(r"doc_id:\s*(\S+)")
_SEARCH_KB_PAGE = re.compile(r"页码:\s*第(\d+)页")
_SEARCH_KB_BLOCK_RANGE = re.compile(r"block_range:\s*\(?(\d+)\s*,\s*(\d+)\)?")


def parse_search_kb_tool_output(tool_output: str) -> list[dict]:
    """从 search_kb 工具返回的格式化文本解析 sources 列表。

    仅解析 search_kb 的结构化输出；search_kb_text 文本搜索结果（含
    "文本搜索结果" / "精确匹配" 标记）返回空（无结构化 doc_id）。

    返回每条 source 的字段：kb_id / doc_id / doc_source / content_snippet /
    page_number（0-based）/ relevance / block_range。doc_id 为空者会被保留，
    调用方按需过滤（qa.py 要求 doc_id 非空才 emit source-document chip；
    agentic_qa._extract_sources 兼容旧 KB 无 doc_id 的情况）。
    """
    if not tool_output:
        return []
    if "文本搜索结果" in tool_output or "精确匹配" in tool_output:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    current: dict | None = None
    for line in tool_output.split("\n"):
        bs = _SEARCH_KB_BLOCK_START.match(line)
        if bs:
            if current and current.get("doc_id") and current["doc_id"] not in seen:
                seen.add(current["doc_id"])
                out.append(current)
            label = bs.group(2)
            name_m = _SEARCH_KB_NAME.search(label)
            doc_source = name_m.group(1) if name_m else (label.strip() or "未知来源")
            current = {
                "kb_id": "",
                "doc_id": "",
                "doc_source": doc_source,
                "content_snippet": "",
                "page_number": None,
                "relevance": 0.0,
                "block_range": None,
            }
            continue
        if current is None:
            continue
        if "相关度" in line:
            rel_m = _SEARCH_KB_RELEVANCE.search(line)
            if rel_m:
                current["relevance"] = float(rel_m.group(1))
            did_m = _SEARCH_KB_DOC_ID.search(line)
            if did_m:
                current["doc_id"] = did_m.group(1)
            pg_m = _SEARCH_KB_PAGE.search(line)
            if pg_m:
                current["page_number"] = int(pg_m.group(1)) - 1  # 1-based → 0-based
            br_m = _SEARCH_KB_BLOCK_RANGE.search(line)
            if br_m:
                current["block_range"] = [int(br_m.group(1)), int(br_m.group(2))]
        elif line.strip() and "⚠️" not in line and "来源单一性警告" not in line:
            snippet = line.strip()
            current["content_snippet"] = (
                (current["content_snippet"] + "\n" + snippet).strip()
                if current["content_snippet"]
                else snippet
            )
    if current and current.get("doc_id") and current["doc_id"] not in seen:
        out.append(current)
    return out


def search_kb(
    kb_ids: list[str],
    query: str,
    top_k: int = 5,
    sync_rebuild_for_audit: bool = False,
) -> str:
    """搜索知识库，返回格式化的标准条款。

    Args:
        sync_rebuild_for_audit: 审核路径置 True（同步重建，宁可长时也要保证向量质量）；
                                 QA 路径默认 False（异步降级，避免阻塞 HTTP 线程）。
                                 经 ``vec_search`` 透传到 ``_ensure_kb_index``，
                                 落实 ADR-0002 §决策 3 的"问答 / 审核请求
                                 是否允许触发同步重建"的语义开关。
    """
    if not query or not kb_ids:
        return "（未提供搜索关键词或知识库）"

    from services.vector_search import vec_search

    try:
        results = vec_search(
            kb_ids, query,
            top_k=top_k,
            sync_rebuild_for_audit=sync_rebuild_for_audit,
        )
    except Exception as e:
        _logger.warning("search_kb failed for query '%s': %s", query, e)
        error_msg = str(e)
        return (
            f"（语义搜索失败: {error_msg}。\n"
            f"建议：1) 尝试用更简短的关键词（如去掉修饰词）；"
            f"2) 如果是精确术语或标准编号，改用 search_kb_text；"
            f"3) 如果持续失败，跳过当前搜索点继续审核其他内容）"
        )

    if not results:
        return f"（未找到与「{query}」相关的标准）"

    lines = [f"【知识库搜索结果（搜索词: {query}，共 {len(results)} 条）】"]

    # 统计来源多样性
    unique_doc_ids: set[str] = set()
    unique_sources: set[str] = set()

    for i, r in enumerate(results, 1):
        relevance = r.get("relevance", 0)
        doc = r.get("doc_source", "") or r.get("doc_id", "")
        doc_id = r.get("doc_id", "")
        clause = r.get("clause_number", "")
        section = r.get("section_path", "")
        page_number = r.get("page_number")  # 0-based from metadata
        content = (r.get("content", "") or "")

        if doc_id:
            unique_doc_ids.add(doc_id)
        if doc:
            unique_sources.add(doc)

        label_parts = []
        if doc:
            label_parts.append(f"【{doc}】")
        if clause:
            label_parts.append(f"第{clause}条")
        if section and not clause:
            label_parts.append(section)
        label = " ".join(label_parts) if label_parts else "未知来源"

        meta_parts = [f"相关度: {relevance:.2f}"]
        if doc_id:
            meta_parts.append(f"doc_id: {doc_id}")
        if page_number is not None:
            meta_parts.append(f"页码: 第{page_number + 1}页")  # 0-based → 1-based display
        # V8-S3: 把 block_range 透传到 LLM 可见的工具输出。仅在非空时追加,
        # 避免对旧 KB(无 block_range)的输出加噪音字段——LLM 不需要按字段思考。
        block_range = r.get("block_range")
        if block_range:
            meta_parts.append(f"block_range: {tuple(block_range)}")
        meta_line = " | ".join(meta_parts)

        lines.append(f"\n{i}. {label}\n   {meta_line}\n   {content}")

    # 来源单一性警告：当所有结果来自 ≤1 个文档时提示
    if len(unique_doc_ids) <= 1 and len(results) > 0:
        source_label = "、".join(sorted(unique_sources)) if unique_sources else "未知来源"
        lines.append(
            f"\n⚠️ 来源单一性警告：所有 {len(results)} 条结果均来自同一份标准文档"
            f"（{source_label}）。"
            f"如果该文档的技术领域与当前搜索意图不匹配，"
            f"请换用完全不同的关键词重搜，或转向审核文档的其他主题。"
        )

    return "\n".join(lines)


def search_kb_text(kb_ids: list[str], query: str) -> str:
    """纯文本关键词搜索知识库（V5：pages/{doc_id}.json 内存 grep）。"""
    if not query or not kb_ids:
        return "（未提供搜索关键词或知识库）"

    try:
        from services.vector_search import search_doc_by_text as _search_doc_by_text
        hits = _search_doc_by_text(query, kb_ids)
    except Exception as e:
        _logger.warning("search_kb_text failed for query '%s': %s", query, e)
        return (
            f"（文本搜索失败: {e}。\n"
            f"建议：1) 简化搜索词为更短的关键词；"
            f"2) 如果是概念性要求，改用 search_kb 语义搜索；"
            f"3) 如果持续失败，跳过当前搜索继续审核其他内容）"
        )

    if not hits:
        return f"（未找到与「{query}」匹配的文本）"

    parts = []
    for h in hits[:5]:
        loc = f"doc={h['doc_id']} / page={h['page_number']}"
        parts.append(f"【{loc}】\n{h['content']}")
    body = "\n\n---\n\n".join(parts)
    if len(body) > 5000:
        body = body[:5000] + "\n... [截断]"
    return f"【知识库文本搜索结果（精确匹配: {query}）】\n{body}"
