from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel, Field
from ulid import ULID


class Clause(BaseModel):
    """条款"""
    number: str
    text: str
    page: Optional[int] = None


class Chapter(BaseModel):
    """章节"""
    number: Optional[str] = None
    title: str
    clauses: list[Clause] = Field(default_factory=list)


class DocumentStructure(BaseModel):
    """文档结构"""
    title: Optional[str] = None
    chapters: list[Chapter] = Field(default_factory=list)
    total_clauses: int = 0


class AuditDocument(BaseModel):
    """待审核文档模型"""

    id: str = Field(default_factory=lambda: str(ULID()))
    name: str
    original_name: str
    file_type: Literal["pdf", "doc", "docx"]
    file_path: str
    page_count: Optional[int] = None
    status: Literal["uploaded", "parsed", "indexed", "audit_pending", "auditing", "completed", "failed"] = "uploaded"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    parsed_content: Optional[str] = None
    structure: Optional[DocumentStructure] = None
    tree_index_path: Optional[str] = None
    error_message: Optional[str] = None

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat(),
        }

    def to_dict(self) -> dict:
        data = self.model_dump()
        # 手动处理嵌套的 DocumentStructure
        if data.get("structure"):
            data["structure"] = data["structure"]
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "AuditDocument":
        for key in ("created_at", "updated_at"):
            if isinstance(data.get(key), str):
                data[key] = datetime.fromisoformat(data[key])
        # DocumentStructure 处理
        if data.get("structure") and isinstance(data["structure"], dict):
            data["structure"] = DocumentStructure(**data["structure"])
        return cls(**data)
