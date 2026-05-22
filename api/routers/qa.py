from typing import Optional

from pydantic import BaseModel
from fastapi import APIRouter, HTTPException

from services.qa_service import ask as qa_ask

router = APIRouter(prefix="/api/v1/qa", tags=["qa"])


class QARequest(BaseModel):
    question: str
    kb_ids: list[str]
    top_k: int = 5


class QASource(BaseModel):
    kb_id: str
    doc_id: str
    doc_source: str
    content_snippet: str
    relevance: float


class QAResponse(BaseModel):
    answer: str
    sources: list[QASource]


@router.post("/ask", response_model=QAResponse)
def ask_question(req: QARequest):
    """向知识库提问，返回答案及参考来源。"""
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="问题不能为空")
    if not req.kb_ids:
        raise HTTPException(status_code=400, detail="请至少选择一个知识库")

    try:
        result = qa_ask(req.kb_ids, req.question, req.top_k)
        return QAResponse(
            answer=result["answer"],
            sources=[QASource(**s) for s in result["sources"]],
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"问答处理失败: {e}")
