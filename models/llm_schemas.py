"""LlamaIndex 结构化输出用的 Pydantic 模型。

让 LLM 返回类型安全的数据，替代手写 JSON + regex 解析。
"""

from typing import Optional
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
