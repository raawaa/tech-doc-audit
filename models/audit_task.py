from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel, Field
from ulid import ULID


AuditType = Literal["compliance", "completeness", "consistency", "insufficient_evidence", "out_of_scope"]


class IssueLocation(BaseModel):
    """问题位置"""
    chapter: Optional[str] = None
    clause_number: Optional[str] = None
    page: Optional[int] = None
    original_text: str


class StandardRef(BaseModel):
    """标准依据"""
    standard_name: str
    standard_id: str
    clause: Optional[str] = None
    requirement: Optional[str] = None
    # PDF 跳转溯源
    doc_id: Optional[str] = None          # KB 文档 ID，定位文件
    page_number: Optional[int] = None     # 条款所在页码 (1-based)
    chunk_text: Optional[str] = None      # chunk 原文片段，用于 PDF 高亮搜索


class ExtractedStandard(BaseModel):
    """LLM 从 issue 文本中提取出的标准信息（标准关联的中间产物）。

    由标准 extractor 产出、供关联策略消费；不持久化。
    """
    numbers: list[str] = Field(default_factory=list)   # 标准编号，如 "GB/T 20145-2006"
    names: list[str] = Field(default_factory=list)     # 标准中文名，不含书名号《》


class AuditIssue(BaseModel):
    """审核问题"""
    id: int
    type: AuditType
    location: IssueLocation
    description: str
    severity: Literal["high", "medium", "low"]
    standard_reference: Optional[StandardRef] = None
    suggestion: Optional[str] = None
    cited_excerpt: str = ""
    document_position: str = ""


class ResultSummary(BaseModel):
    """结果摘要"""
    total_clauses: int = 0
    issues_count: int = 0
    compliance_issues: int = 0
    completeness_issues: int = 0
    consistency_issues: int = 0
    high_severity: int = 0
    medium_severity: int = 0
    low_severity: int = 0


class AuditResult(BaseModel):
    """审核结果"""
    task_id: str
    document_id: str
    document_name: str
    summary: ResultSummary
    issues: list[AuditIssue] = Field(default_factory=list)
    raw_analysis: Optional[str] = None
    generated_at: datetime = Field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        # 使用 mode="json" 自动处理 datetime 序列化
        return self.model_dump(mode="json")


class AuditTask(BaseModel):
    """审核任务"""
    id: str = Field(default_factory=lambda: str(ULID()))
    document_id: str
    document_name: str
    kb_ids: list[str] = Field(default_factory=list)
    audit_types: list[AuditType] = Field(default_factory=lambda: ["compliance", "completeness", "consistency"])
    status: Literal["pending", "processing", "completed", "failed", "cancelled"] = "pending"
    progress: float = 0.0
    progress_label: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    result: Optional[AuditResult] = None
    error_message: Optional[str] = None
    degradation_log: list[dict] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        # 使用 mode="json" 自动处理 datetime 序列化
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict) -> "AuditTask":
        for key in ("created_at", "updated_at", "started_at", "completed_at"):
            if isinstance(data.get(key), str):
                try:
                    data[key] = datetime.fromisoformat(data[key])
                except ValueError:
                    data[key] = None
        # 处理 AuditResult
        if data.get("result") and isinstance(data["result"], dict):
            result_data = data["result"]
            for key in ("generated_at",):
                if result_data.get(key) and isinstance(result_data[key], str):
                    try:
                        result_data[key] = datetime.fromisoformat(result_data[key])
                    except ValueError:
                        result_data[key] = None
            # 处理 issues
            if result_data.get("issues"):
                issues = []
                for issue_data in result_data["issues"]:
                    loc_data = issue_data.get("location", {})
                    if isinstance(loc_data, dict):
                        issue_data["location"] = IssueLocation(**loc_data)
                    std_data = issue_data.get("standard_reference")
                    if std_data and isinstance(std_data, dict):
                        issue_data["standard_reference"] = StandardRef(**std_data)
                    issues.append(AuditIssue(**issue_data))
                result_data["issues"] = issues
            data["result"] = AuditResult(**result_data)
        return cls(**data)
