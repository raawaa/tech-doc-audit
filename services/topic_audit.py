"""主题审核。

流程：
1. 预定义关键词在 parsed_content 全文定位相关段落
2. KB 搜索
3. 1 次 LLM 审核调用（ChatPromptTemplate + structured output）
"""

import re
from llama_index.core.llms import ChatMessage, MessageRole
from llama_index.core.prompts import ChatPromptTemplate

from models.audit_task import AuditIssue, IssueLocation, StandardRef
from models.llm_schemas import TopicIssueList
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


def locate_paragraphs(content: str, keywords: list[str]) -> str:
    """在 parsed_content 全文搜关键词，取上下文的完整段落。"""
    if not content or not keywords:
        return ""
    found = []
    seen = set()
    for kw in keywords:
        for m in re.finditer(re.escape(kw), content, re.IGNORECASE):
            start = max(0, m.start() - KEYWORD_CONTEXT_CHARS)
            end = min(len(content), m.end() + KEYWORD_CONTEXT_CHARS)
            key = (start // 1000, end // 1000)
            if key in seen:
                continue
            seen.add(key)
            found.append(content[start:end].strip())
            if len(found) >= 5:
                break
        if len(found) >= 5:
            break
    return "\n\n---\n\n".join(found) if found else ""


# ── 审核 ────────────────────────────────────────────────────────────────────

AUDIT_SYSTEM_PROMPT = """你是一个严格的技术文档审核专家。请对给定主题审核文档中的相关段落。

对于每个发现的问题，按要求的格式输出。没有问题的主题返回空列表。"""

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

请参照知识库中的标准/制度内容，审核文档中的相关段落是否合规。对比检查是否存在不一致、违规或遗漏之处。""",
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
    chapter_body = locate_paragraphs(parsed_content, keywords)
    if not chapter_body:
        chapter_body = "（文档中未找到匹配内容）"

    kb_reference = _search_kb_by_keywords(kb_ids, keywords, topic["name"])

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
        try:
            response = get_llm().chat(messages)
            result = _parse_json_fallback(response.message.content or "")
        except Exception as e:
            _logger.warning("topic audit failed (%s): %s", topic.get("id", "?"), e)
            return []

    if not result or not result.issues:
        return []

    return _issues_from_schema(result, topic_index)


def _issues_from_schema(result: TopicIssueList, topic_index: int) -> list[AuditIssue]:
    """将结构化 LLM 输出映射为 AuditIssue 列表。"""
    issues = []
    for i, item in enumerate(result.issues):
        std_ref = item.standard_reference
        issue = AuditIssue(
            id=topic_index * 1000 + i + 1,
            type=item.type if item.type in ("compliance", "completeness", "consistency") else "completeness",
            location=IssueLocation(
                clause_number=item.clause_number,
                original_text=item.description[:200],
            ),
            description=item.description,
            severity=item.severity if item.severity in ("high", "medium", "low") else "medium",
            suggestion=item.suggestion,
        )
        if std_ref:
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
    return _search_kb(kb_ids, topic_name)


def _parse_json_fallback(content: str) -> TopicIssueList | None:
    """当 structured_llm 不可用时的降级解析。"""
    import json
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return TopicIssueList.model_validate(data)
    except Exception:
        return None
