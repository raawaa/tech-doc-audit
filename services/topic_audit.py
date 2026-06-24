"""主题审核。

流程：
1. 预定义关键词在 parsed_content 全文定位相关段落
2. KB 搜索
3. 1 次 LLM 审核调用（ChatPromptTemplate + structured output）
"""

import bisect
import re
from llama_index.core.llms import ChatMessage, MessageRole
from llama_index.core.prompts import ChatPromptTemplate

from models.audit_task import AuditIssue, IssueLocation, StandardRef
from models.llm_schemas import TopicIssueList, TopicIssue
from services.document_nav import DocumentNav
from core.logger import get_logger
from core.settings import get_llm

_logger = get_logger(__name__)


AUDIT_TOPICS = [
    {
        "id": "tax_compliance",
        "name": "增值税与税率合规",
        "prompt": "审核投标报价中的增值税税率条款是否合规、税率调整机制是否合理、税务风险是否充分披露。",
        "keywords": ["增值税", "税率", "税务", "不含税价", "发票", "税收", "税率调整"],
    },
    {
        "id": "brand_restriction",
        "name": "品牌限制与公平竞争",
        "prompt": "审核技术规格中是否存在不合理品牌指定或限制竞争条款。",
        "keywords": ["品牌", "型号", "原产地", "指定", "同等或以上"],
    },
    {
        "id": "payment_terms",
        "name": "费用与支付条款",
        "prompt": "审核投标保证金、招标服务费、付款条件等费用条款是否明确合理。",
        "keywords": ["保证金", "服务费", "付款", "费用", "报价", "金额"],
    },
    {
        "id": "quality_warranty",
        "name": "质保与验收要求",
        "prompt": "审核质保期、验收标准、售后服务要求是否完整明确。",
        "keywords": ["质保", "保修", "验收", "售后服务", "质量保证"],
    },
    {
        "id": "liability_penalty",
        "name": "责任与违约条款",
        "prompt": "审核违约责任、赔偿限额、争议解决条款是否平衡合理。",
        "keywords": ["违约", "赔偿", "责任", "争议", "罚款"],
    },
    {
        "id": "scope_clarity",
        "name": "采购范围与技术要求",
        "prompt": "审核采购范围、技术规格、参数要求是否清晰无歧义。",
        "keywords": ["范围", "规格", "参数", "要求", "标准", "技术"],
    },
    {
        "id": "data_reasonableness",
        "name": "数据与指标合理性",
        "prompt": "审核文档中涉及的具体数据、指标、参数、报价金额、时限是否合理、一致、无矛盾。",
        "keywords": ["★", "不低于", "不超过", "大于", "小于", "万元", "元/"],
    },
    {
        "id": "completeness",
        "name": "文档完整性",
        "prompt": "审核文档是否有内容缺失、占位符未填写、引用不完整等问题。",
        "keywords": ["【】", "___", "XX", "详见", "N/A"],
    },
]


# ── 段落定位 ────────────────────────────────────────────────────────────────

KEYWORD_CONTEXT_CHARS = 1500


def locate_paragraphs(content: str, keywords: list[str]) -> tuple[str, list[dict]]:
    """在 parsed_content 全文搜关键词，取上下文的完整段落。

    Returns:
        (章节正文, 位置元数据列表)
        位置元数据: {"line_start": int, "line_end": int, "keyword": str}
    """
    if not content or not keywords:
        return "", []

    lines = content.split("\n")
    # 预计算每行的字符偏移
    line_offsets = [0]
    for line in lines:
        line_offsets.append(line_offsets[-1] + len(line) + 1)  # +1 for \n

    found = []
    positions = []
    seen = set()
    for kw in keywords:
        for m in re.finditer(re.escape(kw), content, re.IGNORECASE):
            start = max(0, m.start() - KEYWORD_CONTEXT_CHARS)
            end = min(len(content), m.end() + KEYWORD_CONTEXT_CHARS)
            key = (start // 1000, end // 1000)
            if key in seen:
                continue
            seen.add(key)

            # 将字符偏移映射到行号
            import bisect
            line_start = bisect.bisect_right(line_offsets, start) - 1
            line_end = bisect.bisect_right(line_offsets, end) - 1
            line_start = max(0, line_start)
            line_end = max(0, line_end)

            found.append(content[start:end].strip())
            positions.append({"line_start": line_start + 1, "line_end": line_end + 1, "keyword": kw})
            if len(found) >= 5:
                break
        if len(found) >= 5:
            break

    return "\n\n---\n\n".join(found) if found else "", positions


# ── 审核 ────────────────────────────────────────────────────────────────────

AUDIT_SYSTEM_PROMPT = """你是一个严格的技术文档审核专家。请对给定主题审核文档中的相关段落。

对于每个发现的问题，按要求的格式输出。没有问题的主题返回空列表。

问题类型说明：
- compliance: 文档内容违反标准/制度规定
- completeness: 文档缺少必要内容或信息不完整
- consistency: 文档内部或与外部标准存在不一致
- insufficient_evidence: 证据不足，无法做出判断
- out_of_scope: 文档内容超出审核范围或不适用于当前标准

当证据不足或无法确定时，请使用 insufficient_evidence 类型。不要强行判断。

重要：每个问题必须包含标准/制度依据（standard_reference），包括标准名称（standard_name）和标准编号（standard_id）。同时必须从原文中引用具体的文本片段（cited_excerpt）并注明该引用在文档中的位置（document_position）。当知识库无直接标准依据时，可引用通用法律法规（如《招标投标法》、《民法典》、《政府采购法》等）作为参考。"""

# 结构化输出模板
_audit_prompt = ChatPromptTemplate(
    message_templates=[
        ChatMessage(role=MessageRole.SYSTEM, content=AUDIT_SYSTEM_PROMPT),
        ChatMessage(
            role=MessageRole.USER,
            content="""审核主题：{topic_name}
审核要求：{topic_prompt}

【文档相关段落】
{chapter_body}

【知识库参考依据】
{kb_reference}

请参照知识库中的标准/制度内容，审核文档中的相关段落是否合规。对比检查是否存在不一致、违规或遗漏之处。
	请在 document_position 字段中注明问题所在的具体行号范围（如"第 42-58 行"）。""",
        ),
    ]
)


def audit_topic(
    topic: dict,
    doc_nav: DocumentNav,
    kb_ids: list[str],
    topic_index: int,
    parsed_content: str,
) -> list[AuditIssue]:
    """一个主题：关键词定位段落 → 搜 KB → 1 次 LLM。"""
    keywords = topic.get("keywords", [topic["name"]])
    chapter_body, positions = locate_paragraphs(parsed_content, keywords)
    # 在段落文本前附加行号范围
    if positions:
        line_ranges = ", ".join(f"第{p['line_start']}-{p['line_end']}行"
                                for p in positions[:3])
        if chapter_body:
            chapter_body = f"（以下段落来自文档 {line_ranges}）\n\n{chapter_body}"
    if not chapter_body:
        chapter_body = "（文档中未找到匹配内容）"

    kb_reference = _search_kb_by_keywords(kb_ids, keywords, topic["name"])
    # 当知识库无相关依据时，提示 LLM 基于文档内容做自洽性审核
    if not kb_reference or kb_reference == "【知识库参考依据（向量检索）】":
        kb_reference = """（知识库中未找到直接相关的标准依据。

请基于以下维度对文档内容本身进行自洽性审核：
1. 文档内部数据是否前后一致（如清单编号连续性、规格参数矛盾）
2. 条款表述是否清晰无歧义
3. 引用、承诺、免责声明是否有遗漏
4. 用词是否规范，有无过度宽泛的兜底条款
如有发现问题请按 JSON 格式输出，无问题则返回空列表。）"""

    messages = _audit_prompt.format_messages(
        topic_name=topic["name"],
        topic_prompt=topic.get("prompt", ""),
        chapter_body=chapter_body,
        kb_reference=kb_reference,
    )

    try:
        structured_llm = get_llm().as_structured_llm(output_cls=TopicIssueList)
        response = structured_llm.chat(messages)
        result: TopicIssueList = response.raw
    except Exception:
        # 降级：手调 .chat() + JSON 解析
        from core.degradation import record as _deg_record
        _deg_record("topic_audit", "structured_llm_failed",
                     f"Topic '{topic.get('id', '?')}': structured_llm failed, falling back to chat+JSON parse")
        try:
            response = get_llm().chat(messages)
            result = _parse_json_fallback(response.message.content or "")
        except Exception as e:
            _deg_record("topic_audit", "chat_fallback_failed",
                         f"Topic '{topic.get('id', '?')}': {e}")
            _logger.warning("topic audit failed (%s): %s", topic.get("id", "?"), e)
            return []

    if not result or not result.issues:
        return []

    return _issues_from_schema(result, topic_index)


def _issues_from_schema(result: TopicIssueList, topic_index: int) -> list[AuditIssue]:
    """将结构化 LLM 输出映射为 AuditIssue 列表。过滤掉无描述/无引用的噪声。"""
    issues = []
    for i, item in enumerate(result.issues):
        # 过滤噪声：无描述且无引用的 issue 视为解析 artifact
        desc = (item.description or "").strip()
        excerpt = (item.cited_excerpt or "").strip()
        if not desc and not excerpt:
            continue
        std_ref = item.standard_reference
        # 如果 description 为空但有引用，生成兜底描述
        if not desc and excerpt:
            desc = f"（审核发现潜在问题，相关原文引用如上）"
        if not desc:
            desc = "(未提供具体描述)"
        issue = AuditIssue(
            id=topic_index * 1000 + i + 1,
            type=item.type if item.type in ("compliance", "completeness", "consistency", "insufficient_evidence", "out_of_scope") else "completeness",
            location=IssueLocation(
                clause_number=item.clause_number,
                original_text=item.description[:200],
            ),
            description=item.description,
            severity=item.severity if item.severity in ("high", "medium", "low") else "medium",
            suggestion=item.suggestion,
            cited_excerpt=item.cited_excerpt,
            document_position=item.document_position,
        )
        if std_ref and (std_ref.standard_name or std_ref.standard_id):
            issue.standard_reference = StandardRef(
                standard_name=std_ref.standard_name or "",
                standard_id=std_ref.standard_id or "",
                clause=std_ref.clause,
                requirement=std_ref.requirement,
            )
        issues.append(issue)
    return issues


def _search_kb(kb_ids: list[str], query: str) -> str:
    from services.vector_search import get_kb_content_for_audit
    try:
        return get_kb_content_for_audit(kb_ids, query)
    except Exception:
        return ""


def _search_kb_by_keywords(kb_ids: list[str], keywords: list[str], topic_name: str = "") -> str:
    """用预定义关键词搜索知识库，跳过 LLM 提取步骤。"""
    try:
        from services.vector_search import search_by_keywords
        result = search_by_keywords(kb_ids, keywords, topic_name)
        if result:
            return result
    except Exception:
        pass
    from core.degradation import record as _deg_record
    _deg_record("kb_search", "keyword_search_failed",
                 f"Keyword search failed for topic '{topic_name}', falling back to semantic search")
    return _search_kb(kb_ids, topic_name)


def _parse_json_fallback(content: str) -> TopicIssueList | None:
    """当 structured_llm 不可用时的降级解析。

    支持三种 LLM 输出格式：
    1. {"issues": [...]} — 标准 JSON 对象
    2. [...] — 直接 JSON 数组
    3. ```json [...] ``` — Markdown 代码块中的 JSON
    """
    import json
    # 去除 Markdown 代码块包装
    content = re.sub(r'```(?:json)?\s*', '', content, flags=re.IGNORECASE).strip()
    content = content.rstrip('`').strip()

    # 尝试 JSON 对象格式: {"issues": ...}
    match = re.search(r'\{[^{]*"issues"\s*:', content, re.DOTALL)
    if match:
        try:
            start = match.start()
            # 提取完整 JSON 对象（平衡花括号）
            depth = 0
            end = start
            for j in range(start, len(content)):
                ch = content[j]
                if ch == '{': depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        end = j + 1
                        break
            data = json.loads(content[start:end])
            return TopicIssueList.model_validate(data)
        except Exception:
            pass

    # 尝试直接 JSON 数组格式: [{...}, ...] 或 []
    match = re.search(r'\[\s*\{', content, re.DOTALL)
    if not match:
        match = re.search(r'\[\s*\]', content, re.DOTALL)
    if match:
        try:
            start = match.start()
            # 提取完整 JSON 数组（平衡方括号）
            depth = 0
            end = start
            for j in range(start, len(content)):
                ch = content[j]
                if ch == '[': depth += 1
                elif ch == ']':
                    depth -= 1
                    if depth == 0:
                        end = j + 1
                        break
            issues_data = json.loads(content[start:end])
            # 将数组包装为 TopicIssueList
            issues = []
            for item in issues_data:
                if not isinstance(item, dict):
                    continue
                # 标准化字段名
                desc = item.get('issue') or item.get('description') or item.get('problem') or ''
                # 构造 StandardRef
                sr_data = item.get('standard_reference')
                sr = None
                if isinstance(sr_data, dict):
                    from models.llm_schemas import StandardRef as StdRef
                    sr = StdRef(
                        standard_name=sr_data.get('standard_name', ''),
                        standard_id=sr_data.get('standard_id', ''),
                        clause=sr_data.get('clause'),
                        requirement=sr_data.get('requirement'),
                    )
                issues.append(TopicIssue(
                    type=item.get('type', 'compliance'),
                    severity=item.get('severity', 'medium'),
                    description=desc,
                    clause_number=item.get('clause_number'),
                    suggestion=item.get('suggestion') or item.get('modification', ''),
                    cited_excerpt=item.get('cited_excerpt', ''),
                    document_position=item.get('document_position', ''),
                    standard_reference=sr,
                ))
            if issues:
                return TopicIssueList(issues=issues)
        except Exception:
            pass

    return None
