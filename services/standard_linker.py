"""标准关联（Standard Linking）—— 审核后处理。

对每个引用了标准的 AuditIssue，在知识库中定位该标准文档，回填
StandardRef（doc_id / page_number / chunk_text）。best-effort：
任何步骤失败都不影响审核结果。

入口：link_standards(issues, kb_ids, *, extractor=None)
默认 extractor 为轻量 DeepSeek 模型（extract_standards_deepseek）；
可注入自定义 extractor，以便在无 LLM 环境下单测关联策略
（搜索 → 精确验证 → 回填），无需真实模型或 FAISS 索引。

详见 CONTEXT.md「标准关联」。
"""

import json
import os
from typing import Callable

from core.logger import get_logger
from models.audit_task import AuditIssue, ExtractedStandard
from services.vector_search import search_doc_by_text, vec_search
import storage.doc_repo as _doc_repo

_logger = get_logger(__name__)

# 标准 extractor 的 interface：从 issue 文本提取标准编号/名称。
# 默认实现 = extract_standards_deepseek。测试可注入返回 canned dict 的假实现，
# 从而脱离 LLM 单测关联策略（搜索→验证→回填）。
ExtractFn = Callable[[list[AuditIssue]], "dict[int, ExtractedStandard]"]


def extract_standards_deepseek(issues: list[AuditIssue]) -> dict[int, ExtractedStandard]:
    """默认标准 extractor：用轻量 DeepSeek 模型批量从 issue 文本提取标准编号和名称。

    Args:
        issues: standard_doc_id 为空的 issue 列表

    Returns:
        {issue.id: ExtractedStandard}；提取不到任何标准的 issue 不出现在结果中。
        任何失败（无 API key / LLM 异常 / 空响应）都返回 {}，绝不抛出——
        保证 best-effort 语义。
    """
    if not issues:
        return {}

    # 构建提取输入
    input_items = []
    for iss in issues:
        item = {"id": iss.id}
        if iss.standard_reference:
            sn = (iss.standard_reference.standard_name or "").strip()
            if sn:
                item["standard_name"] = sn
        item["description"] = iss.description or ""
        item["cited_excerpt"] = iss.cited_excerpt or ""
        item["suggestion"] = iss.suggestion or ""
        input_items.append(item)

    system_prompt = """你是一个标准文献信息提取器。从审核问题的描述文本中提取被引用的标准编号和标准名称，输出 JSON 格式。

规则：
1. standard_numbers: 标准编号列表，如 "GB/T 20145-2006"、"GB 50016"、"CJJ 101-2016"。
   不含纯数字编号（如"12345"不算）。从 description、cited_excerpt、suggestion 字段中提取。
2. standard_names: 标准中文名称列表，不含书名号《》，如 "灯和灯系统的光生物安全性"。
3. standard_name 字段如果已有值直接复用，无需重复提取。
4. 如果问题没有涉及任何可识别的标准，返回空数组。

输入格式: {"issues": [{"id": 1, "standard_name": "...", "description": "...", "cited_excerpt": "...", "suggestion": "..."}]}

输出格式: {"results": [{"id": 1, "standard_numbers": ["GB/T 20145-2006"], "standard_names": ["灯和灯系统的光生物安全性"]}]}"""

    user_prompt = json.dumps({"issues": input_items}, ensure_ascii=False)

    try:
        import httpx
        from openai import OpenAI

        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not api_key:
            _logger.warning("extract_standards_deepseek: DEEPSEEK_API_KEY not set, skipping")
            return {}

        base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
        # 使用轻量模型做提取（不需要深度推理）
        model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

        http_client = httpx.Client(trust_env=False, timeout=httpx.Timeout(60))
        client = OpenAI(api_key=api_key, base_url=base_url, http_client=http_client)

        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=4096,
        )

        content = response.choices[0].message.content
        if not content:
            _logger.warning("extract_standards_deepseek: empty response from LLM")
            return {}

        data = json.loads(content)
        results_list = data.get("results", [])

        output: dict[int, ExtractedStandard] = {}
        for item in results_list:
            iss_id = item.get("id")
            if iss_id is None:
                continue
            nums = item.get("standard_numbers", []) or []
            names = item.get("standard_names", []) or []
            if nums or names:
                output[iss_id] = ExtractedStandard(numbers=nums, names=names)

        _logger.info(
            "extract_standards_deepseek: extracted standards for %d/%d issues",
            len(output), len(issues),
        )
        return output

    except Exception as e:
        _logger.warning("extract_standards_deepseek failed: %s", e)
        return {}


def _search_and_link_standards(
    issues: list[AuditIssue],
    kb_ids: list[str],
    extracted: dict[int, ExtractedStandard],
) -> None:
    """搜索知识库并回填 standard_doc_id 等字段（原地修改 issues）。

    搜索策略（按优先级）：
    1. 精确文本搜索（rga）— 标准编号
    2. 向量语义搜索 — 标准编号 + 标准名称
    3. 结果精确验证 — 命中内容的 content 必须包含标准编号

    结果缓存：同一标准编号只搜一次。

    Args:
        issues: 待处理的 issue 列表（原地修改）
        kb_ids: 审核任务关联的知识库 ID 列表
        extracted: extract_standards_deepseek() 的返回值
    """
    if not issues or not kb_ids:
        return

    # 搜索结果缓存：standard_number -> {doc_id, page_number, chunk_text} | None
    _search_cache: dict[str, dict | None] = {}

    # 按 issue id 索引
    issue_by_id = {iss.id: iss for iss in issues}

    for iss_id, info in extracted.items():
        issue = issue_by_id.get(iss_id)
        if not issue or not issue.standard_reference:
            continue

        standard_numbers = info.numbers
        standard_names = info.names

        best_hit = None

        # ── 策略1: 精确文本搜索 ──
        for std_num in standard_numbers:
            if std_num in _search_cache:
                best_hit = _search_cache[std_num]
                break

            text_hits = search_doc_by_text(std_num, kb_ids)
            if text_hits:
                # 文本搜索命中了文档，但缺少 page_number
                # 用向量搜索补充 page_number 和 chunk_text
                query = f"{std_num} {standard_names[0]}" if standard_names else std_num
                vec_hits = vec_search(kb_ids, query, top_k=3)

                # 精确验证：vec hits 的 content 必须包含标准编号
                matched_doc_ids = {h["doc_id"] for h in text_hits}
                for vh in vec_hits:
                    if vh["doc_id"] in matched_doc_ids:
                        content = vh.get("content", "")
                        if any(sn in content for sn in standard_numbers):
                            best_hit = {
                                "doc_id": vh["doc_id"],
                                "page_number": vh.get("page_number"),
                                "chunk_text": content[:500],
                            }
                            break

                _search_cache[std_num] = best_hit
                break

        # ── 策略2: 向量语义搜索（文本搜索无结果时） ──
        if not best_hit and (standard_numbers or standard_names):
            query_parts = standard_numbers + standard_names
            query = " ".join(query_parts[:3])  # 最多3个词
            vec_hits = vec_search(kb_ids, query, top_k=5)

            for vh in vec_hits:
                content = vh.get("content", "")
                # 精确验证
                verified = False
                if standard_numbers:
                    verified = any(sn in content for sn in standard_numbers)
                else:
                    verified = any(nm in content for nm in standard_names)

                if verified:
                    best_hit = {
                        "doc_id": vh["doc_id"],
                        "page_number": vh.get("page_number"),
                        "chunk_text": content[:500],
                    }
                    if standard_numbers:
                        for sn in standard_numbers:
                            _search_cache[sn] = best_hit
                    break

        # ── 回填 ──
        sr = issue.standard_reference
        if best_hit:
            sr.doc_id = best_hit["doc_id"]
            raw_page = best_hit.get("page_number")
            sr.page_number = raw_page + 1 if raw_page is not None else None
            sr.chunk_text = best_hit.get("chunk_text")
            _logger.info(
                "_search_and_link_standards: linked issue #%d to doc %s",
                issue.id, best_hit["doc_id"],
            )
        # 无论是否搜到 KB 文档，只要 LLM 提取出了标准编号且 standard_name 为空，就补上
        if not sr.standard_name and standard_numbers:
            sr.standard_name = standard_numbers[0]
            sr.standard_id = standard_numbers[0]


def link_standards(
    issues: list[AuditIssue],
    kb_ids: list[str],
    *,
    extractor: ExtractFn | None = None,
) -> None:
    """审核后处理：将 issue 中引用的标准关联到 KB 文档（原地修改 issues）。

    筛选 standard_doc_id 为空的 issue → extractor 提取标准信息 →
    搜索 KB → 回填 doc_id/page_number/chunk_text。

    任何步骤失败都不影响审核结果（best-effort）。

    Args:
        issues: 审核产出的 issue 列表（原地修改 standard_reference 字段）
        kb_ids: 审核任务关联的知识库 ID 列表
        extractor: 标准提取器，默认 extract_standards_deepseek。
            测试可注入返回 canned dict 的假实现，以脱离 LLM 测试关联策略。
    """
    if not issues or not kb_ids:
        return

    if extractor is None:
        extractor = extract_standards_deepseek

    # 收集 KB 中所有有效的 doc_id（用于验证 LLM 填入的 doc_id 是否真实存在）
    valid_doc_ids: set[str] = set()
    for kb_id in kb_ids:
        try:
            for doc in _doc_repo.list_docs(kb_id):
                valid_doc_ids.add(doc.id)
        except Exception:
            pass

    # 筛选：standard_doc_id 为空，或指向不存在的文档（LLM 幻觉）
    pending = []
    for iss in issues:
        sr = iss.standard_reference
        if not sr:
            continue
        doc_id = sr.doc_id
        if not doc_id:
            pending.append(iss)
        elif doc_id not in valid_doc_ids:
            # LLM 填入了无效的 doc_id，清除后重新搜索
            sr.doc_id = None
            sr.page_number = None
            sr.chunk_text = None
            pending.append(iss)

    if not pending:
        return

    _logger.info("link_standards: %d issues need standard linking", len(pending))

    try:
        extracted = extractor(pending)
    except Exception as e:
        _logger.warning("link_standards: extraction failed: %s", e)
        return

    if not extracted:
        return

    try:
        _search_and_link_standards(pending, kb_ids, extracted)
    except Exception as e:
        _logger.warning("link_standards: search failed: %s", e)
