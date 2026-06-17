import json
from typing import Optional

from pydantic import BaseModel
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from services.qa_service import ask as qa_ask
from services.qa_service import chat as qa_chat
from services.qa_service import chat_stream as qa_chat_stream

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


class ChatRequest(BaseModel):
    question: str = ""
    messages: Optional[list] = None
    kb_ids: list[str] = []
    session_id: Optional[str] = None
    top_k: int = 5


class ChatResponse(BaseModel):
    session_id: str
    answer: str
    sources: list[QASource]


@router.post("/ask", response_model=QAResponse)
def ask_question(req: QARequest):
    """单轮问答（无记忆）。"""
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


@router.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    """多轮对话（按 session_id 维护记忆，支持追问）。"""
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="问题不能为空")
    if not req.kb_ids:
        raise HTTPException(status_code=400, detail="请至少选择一个知识库")

    try:
        result = qa_chat(req.session_id, req.question, req.kb_ids, req.top_k)
        return ChatResponse(
            session_id=result["session_id"],
            answer=result["answer"],
            sources=[QASource(**s) for s in result["sources"]],
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"对话处理失败: {e}")


@router.post("/chat/stream")
def chat_stream(req: ChatRequest):
    """多轮对话流式版本（兼容 AI SDK v6 useChat）。

    SSE 格式兼容 EventSourceParserStream + uiMessageChunkSchema：
      event: text-start / text-delta / text-end — token 流式输出
      event: data-progress / data-sources / data-session — 自定义数据
      event: error — 错误
      event: finish — 结束标记

    同时兼容两种请求格式：
    1. AI SDK v6 默认：{ messages: [{role, content}], kb_ids, session_id }
    2. 自定义：{ question, kb_ids, session_id }
    """
    kb_ids = req.kb_ids
    if not kb_ids:
        raise HTTPException(status_code=400, detail="请至少选择一个知识库")

    # 提取问题：优先 req.question，其次从 messages 中取最后一个用户消息
    question = req.question.strip() or ""
    if not question and req.messages:
        last_msg = next((m for m in reversed(req.messages) if isinstance(m, dict) and m.get("role") == "user"), None)
        if last_msg:
            parts = last_msg.get("parts") or []
            if parts:
                texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text"]
                question = "".join(texts)
            else:
                question = last_msg.get("content", "")
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空")

    TEXT_PART_ID = "text_0"

    def _sse(event_type: str, data: dict) -> str:
        return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    def event_generator():
        text_started = False
        for event in qa_chat_stream(req.session_id, question, kb_ids, req.top_k):
            t = event["type"]

            if t == "token":
                if not text_started:
                    yield _sse("text-start", {"type": "text-start", "id": TEXT_PART_ID})
                    text_started = True
                yield _sse("text-delta", {"type": "text-delta", "id": TEXT_PART_ID, "delta": event["text"]})

            elif t == "progress":
                yield _sse("data-progress", {"type": "data-progress", "data": {"label": event["label"]}})

            elif t == "done":
                if text_started:
                    yield _sse("text-end", {"type": "text-end", "id": TEXT_PART_ID})
                if event.get("sources"):
                    yield _sse("data-sources", {"type": "data-sources", "data": {"sources": event["sources"]}})
                if event.get("suggestions"):
                    yield _sse("data-suggestions", {"type": "data-suggestions", "data": {"suggestions": event["suggestions"]}})
                yield _sse("data-session", {"type": "data-session", "data": {"session_id": event["session_id"]}})
                yield _sse("finish", {"type": "finish", "finishReason": "stop"})

            elif t == "error":
                yield _sse("error", {"type": "error", "errorText": event["message"]})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"x-vercel-ai-data-stream": "v1"},
    )
