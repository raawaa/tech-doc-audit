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
    index_status: Literal["none", "building", "ready", "failed", "pending_index"] = "none"
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
        return cls(**data)
