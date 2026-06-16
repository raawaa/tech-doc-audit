"""Agent 动态审核 — LLM 分析文档并自主决定审核主题。

流程：
1. LLM 阅读文档开头部分（标题 + 目录 + 关键条款），判断文档类型
2. 参考 8 个预定义审核维度，选择相关主题
3. 可自定义预定义之外的审核维度
4. 返回与 AUDIT_TOPICS 兼容的主题列表，交由 topic_audit.audit_topic() 执行

使用 LlamaIndex ChatPromptTemplate + structured output，
替代手写 .chat() + regex JSON 解析。
"""

from typing import Optional
from llama_index.core.llms import ChatMessage, MessageRole
from llama_index.core.prompts import ChatPromptTemplate

from core.settings import get_llm
from models.llm_schemas import AuditTopicList, AuditTopicItem

SYSTEM_PROMPT = """你是一个技术文档审核专家。你的任务是分析文档内容，确定需要审核的主题。

参考审核维度（根据文档类型选择性使用，不需要全部套用）：
1. 增值税与税率合规 — 检查税率条款、调整机制、税务风险披露
2. 品牌限制与公平竞争 — 检查技术规格中是否指定品牌、限制竞争
3. 费用与支付条款 — 检查保证金、服务费、付款条件是否明确合理
4. 质保与验收要求 — 检查质保期、验收标准、售后服务
5. 责任与违约条款 — 检查违约责任、赔偿限额、争议解决
6. 采购范围与技术要求 — 检查范围、规格、参数是否清晰
7. 数据与指标合理性 — 检查数据、指标、时限是否合理一致
8. 文档完整性 — 检查是否有缺失、占位符未填写等问题

要求：
- 只选择文档中确实涉及、值得审核的主题
- 如果文档内容不涉及某一维度，不要强行入选
- 如果发现参考维度之外的审核方向，可以自定义
- keywords 要精准（用文档中实际出现的术语），3-5 个
- prompt 要具体（结合文档内容写审核指令）"""


# 结构化 LLM 输出模板
_prompt = ChatPromptTemplate(
    message_templates=[
        ChatMessage(role=MessageRole.SYSTEM, content=SYSTEM_PROMPT),
        ChatMessage(
            role=MessageRole.USER,
            content="请分析以下文档内容，判断需要审核哪些主题。\n\n文档内容：\n{document_preview}",
        ),
    ]
)


def determine_audit_topics(
    parsed_content: str,
    kb_ids: Optional[list[str]] = None,
    max_content_chars: int = 8000,
) -> list[dict]:
    """LLM 分析文档，返回相关审核主题列表。

    返回值与 topic_audit.AUDIT_TOPICS 格式兼容：
    [{"id": "...", "name": "...", "prompt": "...", "keywords": [...]}, ...]

    Args:
        parsed_content: 文档全文。
        kb_ids: 知识库 ID 列表（暂未使用，预留）。
        max_content_chars: 发送给 LLM 的文档最大字符数。

    Returns:
        审核主题列表。如果 LLM 解析失败返回空列表，由调用方降级到固定主题。
    """
    preview = (parsed_content or "")[:max_content_chars]
    messages = _prompt.format_messages(document_preview=preview)

    try:
        structured_llm = get_llm().as_structured_llm(output_cls=AuditTopicList)
        response = structured_llm.chat(messages)
        result: AuditTopicList = response.raw
    except Exception:
        # as_structured_llm 降级：手调 .chat() + 手动解析
        try:
            response = get_llm().chat(messages)
            result = _parse_json_fallback(response.message.content or "", AuditTopicList)
        except Exception:
            return []

    if not result or not result.topics:
        return []

    validated = []
    for t in result.topics:
        validated.append({
            "id": t.id,
            "name": t.name,
            "prompt": t.prompt or f"审核{t.name}相关内容",
            "keywords": t.keywords,
            "reason": t.reason,
        })
    return validated


def _parse_json_fallback(content: str, model_cls: type) -> Optional[AuditTopicList]:
    """当 structured_llm 不可用时的降级解析。"""
    import json, re
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return model_cls.model_validate(data)
    except Exception:
        return None
