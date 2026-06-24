"""原子需求模型——从 KB 标准文档中提取的可检查需求。"""

from typing import Literal, Optional
from pydantic import BaseModel, Field


class AtomicRequirement(BaseModel):
    """一条原子化的可检查需求。

    从 KB 标准文档的某个条款中提取出的最小可检查单元。
    例如：GB/T XXXX 第 5.2 条"质保期自验收合格之日起计算，不得少于12个月"。
    """
    requirement_id: str = Field(description="唯一标识，如 req_gbt_xxx_5.2_0")
    source_kb_id: str = Field(description="来源知识库 ID")
    source_doc_id: str = Field(description="来源文档 ID")
    source_doc_name: str = Field(description="来源文档名称")
    source_clause: str = Field(description="来源条款编号，如 '5.2'")
    standard_name: str = Field(description="标准名称，如 'GB/T XXXX-2024'")
    standard_id: str = Field(description="标准编号")
    requirement_text: str = Field(description="需求原文，一个具体可验证的要求")
    supplementary_context: str = Field(default="", description="需求的补充语境（如适用条件、例外情况）")
    check_type: Literal["threshold", "exists", "equals", "range", "semantic"] = Field(
        description="检查类型：threshold=阈值, exists=是否存在, equals=精确匹配, range=范围, semantic=语义判断"
    )
    expected_value: Optional[str] = Field(default=None, description="期望值（threshold/equals/range 类型时使用）")
    category: str = Field(description="需求类别，如 'quality_warranty', 'payment_terms'")
    keywords: list[str] = Field(default_factory=list, description="检索用关键词")
    status: Literal["pending_review", "approved", "rejected"] = Field(
        default="pending_review",
        description="需求状态：pending_review=待人工审核, approved=已确认, rejected=已驳回"
    )


class AtomicRequirementList(BaseModel):
    """LLM 输出的需求提取结果。"""
    requirements: list[AtomicRequirement]
    notes: str = Field(default="", description="提取说明或备注")
