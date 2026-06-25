import json
import os
from typing import Optional

from pydantic import BaseModel
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from services.qa_service import ask as qa_ask
from services.qa_service import chat as qa_chat
from services.qa_service import chat_stream as qa_chat_stream

router = APIRouter(prefix="/api/v1/qa", tags=["qa"])

_AGENTIC_QA = os.environ.get("USE_AGENTIC_QA", "").lower() in ("true", "1", "yes")


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
        if _AGENTIC_QA:
            from services.agentic_qa import run_agentic_qa
            import uuid
            qa_id = uuid.uuid4().hex[:12]
            result = run_agentic_qa(req.question, req.kb_ids, qa_id=qa_id)
            return QAResponse(
                answer=result["answer"],
                sources=[QASource(
                    kb_id="", doc_id="", doc_source=s.get("doc_source", "未知来源"),
                    content_snippet="", relevance=1.0,
                ) for s in result["sources"]] if result["sources"] else [],
            )
        else:
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
    """多轮对话流式版本。

    非 Agentic 模式：兼容 AI SDK v6 useChat (text-start/text-delta/text-end)
    Agentic 模式：发送 reasoning / tool_call / tool_result / answer 事件
    """
    kb_ids = req.kb_ids
    if not kb_ids:
        raise HTTPException(status_code=400, detail="请至少选择一个知识库")

    # 提取问题
    question = req.question.strip() or ""
    chat_history = []
    if not question and req.messages:
        # 从 messages 中提取对话历史和最后一个用户问题
        for m in req.messages:
            if isinstance(m, dict):
                role = m.get("role", "")
                parts = m.get("parts") or []
                if parts:
                    texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text"]
                    content = "".join(texts)
                else:
                    content = m.get("content", "")
                if role == "user":
                    if m is req.messages[-1] or (isinstance(req.messages[-1], dict) and m == req.messages[-1]):
                        question = content  # 最后一个 user 消息是当前问题
                    else:
                        chat_history.append({"role": "user", "content": content})
                elif role == "assistant":
                    chat_history.append({"role": "assistant", "content": content})
    if not question:
        # fallback: 取最后一个 user 消息
        for m in reversed(req.messages or []):
            if isinstance(m, dict) and m.get("role") == "user":
                parts = m.get("parts") or []
                if parts:
                    question = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text")
                else:
                    question = m.get("content", "")
                if question:
                    break
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空")

    TEXT_PART_ID = "text_0"

    def _sse(event_type: str, data: dict) -> str:
        return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    def event_generator():
        if _AGENTIC_QA:
            from services.agentic_qa import run_agentic_qa
            import uuid
            qa_id = uuid.uuid4().hex[:12]

            # Agentic 流式事件队列
            events: list[dict] = []

            def collect(event: dict):
                events.append(event)

            result = run_agentic_qa(
                question, kb_ids,
                chat_history=chat_history if chat_history else None,
                event_callback=collect,
                qa_id=qa_id,
            )

            # 将事件转为 SSE
            text_started = False
            answer_text = result.get("answer", "")
            for event in events:
                t = event["type"]

                if t == "reasoning":
                    yield _sse("data-progress", {
                        "type": "data-progress",
                        "data": {"label": f"💭 {event['content'][:200]}"},
                    })

                elif t == "tool_call":
                    yield _sse("data-progress", {
                        "type": "data-progress",
                        "data": {"label": f"🔍 {event['tool']}: {json.dumps(event.get('args', {}), ensure_ascii=False)}"},
                    })

                elif t == "tool_result":
                    # 不发送完整结果，只发进度
                    yield _sse("data-progress", {
                        "type": "data-progress",
                        "data": {"label": f"📋 {event['tool']} 完成"},
                    })

                elif t == "start":
                    yield _sse("data-progress", {
                        "type": "data-progress",
                        "data": {"label": event.get("message", "Agentic 问答开始")},
                    })

                elif t == "error":
                    yield _sse("error", {
                        "type": "error",
                        "errorText": event.get("message", "未知错误"),
                    })

                elif t == "answer":
                    # 流式输出答案文本
                    if not text_started:
                        yield _sse("text-start", {"type": "text-start", "id": TEXT_PART_ID})
                        text_started = True
                    yield _sse("text-delta", {
                        "type": "text-delta",
                        "id": TEXT_PART_ID,
                        "delta": event.get("content", ""),
                    })

            if answer_text:
                if not text_started:
                    yield _sse("text-start", {"type": "text-start", "id": TEXT_PART_ID})
                yield _sse("text-end", {"type": "text-end", "id": TEXT_PART_ID})

            if result.get("sources"):
                yield _sse("data-sources", {
                    "type": "data-sources",
                    "data": {"sources": [
                        {"kb_id": "", "doc_id": "", "doc_source": s.get("doc_source", "未知来源"),
                         "content_snippet": "", "relevance": 1.0}
                        for s in result["sources"]
                    ]},
                })

            yield _sse("finish", {"type": "finish", "finishReason": "stop"})

        else:
            # 原始 RAG 管道流式
            text_started_rag = False
            for event in qa_chat_stream(req.session_id, question, kb_ids, req.top_k):
                t = event["type"]

                if t == "token":
                    if not text_started_rag:
                        yield _sse("text-start", {"type": "text-start", "id": TEXT_PART_ID})
                        text_started_rag = True
                    yield _sse("text-delta", {"type": "text-delta", "id": TEXT_PART_ID, "delta": event["text"]})

                elif t == "progress":
                    yield _sse("data-progress", {"type": "data-progress", "data": {"label": event["label"]}})

                elif t == "done":
                    if text_started_rag:
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
