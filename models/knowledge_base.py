from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field
from ulid import ULID


class KnowledgeBase(BaseModel):
    """知识库模型"""

    id: str = Field(default_factory=lambda: str(ULID()))
    name: str
    description: str = ""
    category: Literal["national", "industry", "enterprise"] = "national"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    document_ids: list[str] = Field(default_factory=list)
    index_status: Literal["none", "building", "ready", "failed"] = "none"
    index_progress: float = 0.0
    index_current_doc: str = ""

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat(),
        }

    def to_dict(self) -> dict:
        return self.model_dump()

    @classmethod
    def from_dict(cls, data: dict) -> "KnowledgeBase":
        for key in ("created_at", "updated_at"):
            if isinstance(data.get(key), str):
                data[key] = datetime.fromisoformat(data[key])
        return cls(**data)
