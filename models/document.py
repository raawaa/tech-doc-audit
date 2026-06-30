from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel, Field
from ulid import ULID


class KBDocument(BaseModel):
    """知识库文档模型"""

    id: str = Field(default_factory=lambda: str(ULID()))
    kb_id: str
    name: str
    original_name: str
    file_type: Literal["pdf", "doc", "docx", "md"]
    file_path: str
    tree_index_path: Optional[str] = None
    page_count: Optional[int] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    embedding_status: Literal[
        "none", "pending_index", "indexing", "embedded", "failed"
    ] = "none"
    content_hash: Optional[str] = None  # SHA-256 of raw file bytes, for dedup
    metadata: dict = Field(default_factory=dict)

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat(),
        }

    def to_dict(self) -> dict:
        return self.model_dump()

    @classmethod
    def from_dict(cls, data: dict) -> "KBDocument":
        for key in ("created_at", "updated_at"):
            if isinstance(data.get(key), str):
                data[key] = datetime.fromisoformat(data[key])

        embedding_status = data.get("embedding_status")

        # 元数据迁移（ADR-0003）：旧 doc.index_status → embedding_status
        # - 只在缺失新字段时迁移；已存在则尊重（幂等）
        # - "ready" → "embedded"（终态词分裂）
        # - 其他旧值原样继承：pending_index / indexing / failed / none
        # - "building"（旧概念里 doc 不用，理论上不存在）按"非失败语义"映射为 none
        if embedding_status is None and "index_status" in data:
            legacy = data["index_status"]
            mapping = {
                "ready": "embedded",
                "pending_index": "pending_index",
                "indexing": "indexing",
                "failed": "failed",
                "none": "none",
            }
            data["embedding_status"] = mapping.get(legacy, "none")

        # 清理旧字段，不再写回新 doc
        data.pop("index_status", None)

        return cls(**data)
