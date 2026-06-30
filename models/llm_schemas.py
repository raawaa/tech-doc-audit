"""LlamaIndex 结构化输出用的 Pydantic 模型。

让 LLM 返回类型安全的数据，替代手写 JSON + regex 解析。
"""

from dataclasses import dataclass
from typing import Literal, Optional, Union
from pydantic import BaseModel, Field


# ── Agent 审核主题 ──

class AuditTopicItem(BaseModel):
    """单个审核主题。"""
    id: str = Field(description="主题英文标识，如 tax_compliance")
    name: str = Field(description="主题中文名称，如 增值税与税率合规")
    prompt: str = Field(description="审核该主题时需要重点关注的具体要求")
    keywords: list[str] = Field(description="定位相关段落用的关键词，3-5 个")
    reason: str = Field(description="为什么选择这个主题")


class AuditTopicList(BaseModel):
    """LLM Agent 输出的审核主题列表。"""
    topics: list[AuditTopicItem]


# ── Topic 审核结果 ──

class StandardRef(BaseModel):
    """标准/制度引用。"""
    standard_name: str = ""
    standard_id: str = ""
    clause: Optional[str] = None
    requirement: Optional[str] = None


class TopicIssue(BaseModel):
    """单条审核发现的问题（与 models.audit_task.AuditIssue 对应）。"""
    type: str = Field(description="compliance | completeness | consistency | insufficient_evidence | out_of_scope")
    clause_number: Optional[str] = Field(default=None, description="条款编号")
    description: str = Field(description="问题描述")
    severity: str = Field(description="high | medium | low")
    standard_reference: StandardRef = Field(default_factory=StandardRef)
    suggestion: Optional[str] = None
    cited_excerpt: str = Field(default="", description="从原文中引用的具体文本片段作为证据")
    document_position: str = Field(default="", description="引用文本在文档中的位置描述")


class TopicIssueList(BaseModel):
    """主题审核结果。"""
    issues: list[TopicIssue]


# ── Agentic 审核 ──

AuditActionType = Literal[
    "read_chapter",
    "search_kb",
    "search_kb_text",
    "flag_issue",
    "finish",
]

IssueType = Literal[
    "compliance", "completeness", "consistency", "insufficient_evidence",
]
Severity = Literal["high", "medium", "low"]


class AgentAction(BaseModel):
    """Agentic 审核中每轮 LLM 的结构化决策。

    根据 action 字段选择不同的操作。工具执行结果以纯文本追加到对话历史。
    """

    thought: str = Field(
        description=(
            "推理过程：当前在审文档的哪个章节、发现了什么技术要点、"
            "为什么选择此操作（而非其他操作）"
        ),
    )
    action: AuditActionType = Field(
        description=(
            "要执行的操作。选择规则："
            "read_chapter=需要阅读文档更多章节内容时；"
            "search_kb=搜索概念性/描述性要求时（如质保期、验收标准），语义匹配，能匹配同义词；"
            "search_kb_text=搜索精确术语/编号/参数时（如GB/T 12345、IP65），文本匹配，速度更快；"
            "flag_issue=已找到标准依据且确认文档存在问题后记录；"
            "finish=审核完成，所有问题已记录"
        ),
    )

    # — read_chapter 参数 —
    chapter_index: Optional[int] = Field(
        default=None,
        description=(
            "read_chapter: 章节序号，从1开始，对应文档结构中各章节的编号。"
            "示例：读第3章时传3"
        ),
    )

    # — search_kb / search_kb_text 共用参数 —
    search_query: Optional[str] = Field(
        default=None,
        description=(
            "search_kb 或 search_kb_text: 搜索关键词，从文档当前章节中提取技术术语。"
            "示例：'质保期'、'验收标准'、'GB/T 12345'。"
            "不要输入完整句子，用2-5个词的关键词短语。"
        ),
    )
    search_top_k: Optional[int] = Field(
        default=5,
        description=(
            "search_kb: 返回结果条数，默认5。"
            "若前次搜索结果相关度过低（<0.3），可增至8-10以扩大搜索范围。"
        ),
    )

    # — flag_issue 参数 —
    issue_type: Optional[IssueType] = Field(
        default=None,
        description=(
            "flag_issue: 问题类型。"
            "compliance=违反标准规定（如数值不达标、方法错误）；"
            "completeness=缺少标准要求的必要内容（如缺失质保期条款）；"
            "consistency=文档内部数据矛盾或与标准条文不一致；"
            "insufficient_evidence=证据不足以确定判断"
        ),
    )
    issue_severity: Optional[Severity] = Field(
        default=None,
        description=(
            "flag_issue: 严重程度。"
            "high=可能导致项目失败或重大法律风险；"
            "medium=影响质量或增加成本风险；"
            "low=格式或表述瑕疵，不影响实质合规"
        ),
    )
    issue_description: Optional[str] = Field(
        default=None,
        description=(
            "flag_issue: 问题描述，清晰说明文档何处存在何问题，违反哪条标准哪项要求。"
            "示例：'第三章技术规格中IP防护等级仅标注IP54，"
            "而GB 4208-2008第5.1条要求室外设备不低于IP65。'"
        ),
    )
    standard_name: Optional[str] = Field(
        default=None,
        description=(
            "flag_issue: 标准文档名称，必须来自 search_kb 或 search_kb_text 返回结果。"
            "示例：'CJJ101-2016'、'GB/T 31462-2015'。不可自行编造标准编号。"
        ),
    )
    standard_clause: Optional[str] = Field(
        default=None,
        description=(
            "flag_issue: 标准条款编号，必须来自搜索结果中的'第X条'字段。"
            "示例：'3.2.1'、'5.4.2'。"
        ),
    )
    standard_requirement: Optional[str] = Field(
        default=None,
        description="flag_issue: 标准条款的原文要求（从搜索结果中摘录）",
    )
    cited_excerpt: Optional[str] = Field(
        default=None,
        description=(
            "flag_issue: 从待审核文档原文逐字引用的证据（必须原样复制，不可概括或改写）。"
            "示例：'设备防护等级不低于IP54'。"
            "这是证明问题存在的核心证据，请务必提供。"
        ),
    )
    document_position: Optional[str] = Field(
        default=None,
        description=(
            "flag_issue: 引用原文在文档中的章节位置。使用文档实际的章节标题，不要用编号代替。"
            "示例：'第三章 技术规格与参数要求'。"
        ),
    )
    issue_suggestion: Optional[str] = Field(
        default=None,
        description=(
            "flag_issue: 具体的修改建议。"
            "示例：'将防护等级从IP54修改为不低于IP65，以满足GB 4208-2008室外设备要求。'"
        ),
    )

    # — flag_issue 溯源参数 —
    standard_doc_id: Optional[str] = Field(
        default=None,
        description=(
            "flag_issue: 标准文档的 ID，必须来自 search_kb 返回结果中的 doc_id 字段。"
            "可选，但强烈建议提供——使审核结果可跳转到标准 PDF 原文。"
        ),
    )
    standard_page_number: Optional[int] = Field(
        default=None,
        description=(
            "flag_issue: 标准条款所在页码，来自 search_kb 返回结果中的页码字段。"
            "从1开始计数。可选。"
        ),
    )
    standard_chunk_text: Optional[str] = Field(
        default=None,
        description=(
            "flag_issue: 标准条款的原文片段，来自 search_kb 返回的内容。"
            "可选，用于在 PDF 中高亮定位。"
        ),
    )

    # — finish 参数 —
    final_summary: Optional[str] = Field(
        default=None,
        description=(
            "finish: 审核总结。应包含：发现的问题总数、各类型问题数量、"
            "各严重程度分布、关键发现概述"
        ),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 统一 agent loop 的 LLMStep adapter 类型（ADR-0001）
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class Final:
    """LLMStep 返回：模型自行结束，给出最终回答。"""
    answer: str


@dataclass
class ToolCalls:
    """LLMStep 返回：模型请求执行工具调用。"""
    calls: list[dict]


StepResult = Union[Final, ToolCalls]
