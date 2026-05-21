"""Agent 动态审核 — LLM 分析文档并自主决定审核主题。

流程：
1. LLM 阅读文档开头部分（标题 + 目录 + 关键条款），判断文档类型
2. 参考 8 个预定义审核维度，选择相关主题
3. 可自定义预定义之外的审核维度
4. 返回与 AUDIT_TOPICS 兼容的主题列表，交由 topic_audit.audit_topic() 执行
"""

import json
import re

from llama_index.core.llms import ChatMessage, MessageRole

from core.settings import get_llm

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

对于选中的每个主题，返回：
{
  "topics": [
    {
      "id": "英文标识",
      "name": "中文主题名",
      "prompt": "审核该主题时需要重点关注的具体要求（请结合文档内容具体描述）",
      "keywords": ["关键词1", "关键词2"],
      "reason": "为什么选择这个主题"
    }
  ]
}

要求：
- 只选择文档中确实涉及、值得审核的主题
- 如果文档内容不涉及某一维度，不要强行入选
- 如果发现参考维度之外的审核方向，可以自定义
- keywords 要精准（用文档中实际出现的术语），3-5 个
- prompt 要具体（结合文档内容写审核指令）"""


def determine_audit_topics(
    parsed_content: str,
    kb_ids: list[str] | None = None,
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
    # 取文档开头部分（通常是标题 + 目录 + 前几章）
    preview = parsed_content[:max_content_chars] if parsed_content else ""

    user_prompt = f"""请分析以下文档内容，判断需要审核哪些主题。

文档内容：
{preview}

请输出 JSON（只输出 JSON，不要其他文字）。"""

    try:
        messages = [
            ChatMessage(role=MessageRole.SYSTEM, content=SYSTEM_PROMPT),
            ChatMessage(role=MessageRole.USER, content=user_prompt),
        ]
        response = get_llm().chat(messages)
        content = response.message.content or ""

        # 提取 JSON
        data = _extract_json(content)
        if not data or "topics" not in data:
            return []

        topics = data["topics"]
        # 验证每个 topic 的必要字段
        validated = []
        for t in topics:
            if all(k in t for k in ("id", "name", "keywords")):
                t.setdefault("prompt", f"审核{t['name']}相关内容")
                validated.append(t)
        return validated

    except Exception:
        return []


def _extract_json(text: str) -> dict | None:
    """从 LLM 回复中提取 JSON 对象。"""
    # 尝试直接解析
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 尝试 ```json ... ``` 代码块
    match = re.search(r"```(?:json)?\s*\n?(\{.*?\})\s*\n?```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # 尝试提取最外层 { ... }
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    return None
