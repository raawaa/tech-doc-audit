"""LlamaIndex 结构化输出用的 Pydantic 模型。

让 LLM 返回类型安全的数据，替代手写 JSON + regex 解析。
"""

from typing import Literal, Optional
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
        description="推理过程：当前在审哪个章节、看到了什么、为什么做这个操作",
    )
    action: AuditActionType = Field(
        description="要执行的操作",
    )

    # — read_chapter 参数 —
    chapter_index: Optional[int] = Field(
        default=None, description="read_chapter: 章节序号（从 1 开始）",
    )

    # — search_kb 参数 —
    search_query: Optional[str] = Field(
        default=None, description="search_kb: 搜索关键词",
    )
    search_top_k: Optional[int] = Field(
        default=5, description="search_kb: 返回条数",
    )

    # — flag_issue 参数 —
    issue_type: Optional[IssueType] = Field(
        default=None, description="flag_issue: 问题类型",
    )
    issue_severity: Optional[Severity] = Field(
        default=None, description="flag_issue: 严重程度",
    )
    issue_description: Optional[str] = Field(
        default=None, description="flag_issue: 问题描述",
    )
    standard_name: Optional[str] = Field(
        default=None, description="flag_issue: 标准名称（来自 search_kb 结果）",
    )
    standard_clause: Optional[str] = Field(
        default=None, description="flag_issue: 标准条款编号",
    )
    standard_requirement: Optional[str] = Field(
        default=None, description="flag_issue: 标准原文要求",
    )
    cited_excerpt: Optional[str] = Field(
        default=None, description="flag_issue: 从文档原文引用的证据",
    )
    document_position: Optional[str] = Field(
        default=None, description="flag_issue: 引用在文档中的位置",
    )
    issue_suggestion: Optional[str] = Field(
        default=None, description="flag_issue: 修改建议",
    )

    # — finish 参数 —
    final_summary: Optional[str] = Field(
        default=None, description="finish: 审核总结",
    )
