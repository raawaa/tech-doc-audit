import asyncio
import hashlib
import json
import os
import queue
import threading
from typing import Optional

from pydantic import BaseModel
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from services.agent_tools import parse_search_kb_tool_output
from services.qa_service import ask as qa_ask
from services.qa_service import chat as qa_chat
from services.qa_service import chat_stream as qa_chat_stream

router = APIRouter(prefix="/api/v1/qa", tags=["qa"])

_AGENTIC_QA = os.environ.get("USE_AGENTIC_QA", "true").lower() in ("true", "1", "yes")


# ═══════════════════════════════════════════════════════════════════════════════
# V9 PRD #67 — 内联 source-document chip 支撑
# ═══════════════════════════════════════════════════════════════════════════════
#
# 把 "N 个来源" 末尾折叠列表替换为流式内联 source-document parts（AI SDK v6
# 原生 type），以 sourceId 去重。同一 chat stream 中同一 doc_id 仅首次 emit。
#
# sourceId 格式：src_<doc_id_short>_p<page>，doc_id_short 取 md5(doc_id) 前 8 位
# hex（避免长 KB doc_id 污染标识符），page 为 1-based（None 时为 0）。
#
# search_kb 工具输出的结构化解析在 services.agent_tools.parse_search_kb_tool_output
# （V9 PRD #67 把它定为事实定义），本模块只负责把解析结果包成 SSE 事件。

def _short_doc_id(doc_id: str) -> str:
    if not doc_id:
        return "empty"
    return hashlib.md5(doc_id.encode("utf-8")).hexdigest()[:8]


def build_source_id(source: dict) -> str:
    """根据 QASource 构造稳定的 sourceId。"""
    doc_id = source.get("doc_id", "") or ""
    page = source.get("page_number")
    page_token = f"p{(page + 1) if isinstance(page, int) else 0}"
    return f"src_{_short_doc_id(doc_id)}_{page_token}"


def build_source_document_payload(source: dict) -> dict:
    """构造 AI SDK v6 source-document SSE 事件 payload。

    原始 QASource 落在 providerMetadata.qaSource，前端据此调用既有的
    buildQASourcePreviewUrl() 生成预览 URL（前后端共用 URL 生成器）。
    """
    doc_id = source.get("doc_id", "") or ""
    payload = {
        "type": "source-document",
        "sourceId": build_source_id(source),
        "mediaType": "application/pdf",
        "title": source.get("doc_source") or "未知来源",
        "providerMetadata": {
            "qaSource": {
                "kb_id": source.get("kb_id", "") or "",
                "doc_id": doc_id,
                "doc_source": source.get("doc_source", "") or "",
                "content_snippet": (source.get("content_snippet") or "")[:300],
                "page_number": source.get("page_number"),
                "relevance": source.get("relevance", 0.0),
                "block_range": source.get("block_range"),
            },
        },
    }
    if doc_id:
        payload["filename"] = doc_id
    return payload


def _build_source_document_events(
    sources: list[dict],
    seen_doc_ids: set[str],
) -> list[dict]:
    """生成 source-document 事件 payload；同 doc_id 跳过（后端 dedupe 屏障）。

    返回值是事件 payload 列表（调用方负责 SSE 序列化）。空 doc_id 也跳过，
    让"无 chip"成为 search_kb_text / 旧 KB 的自然结果（spec: 不留孤儿进度条）。
    """
    events = []
    for s in sources:
        doc_id = s.get("doc_id") or ""
        if not doc_id or doc_id in seen_doc_ids:
            continue
        seen_doc_ids.add(doc_id)
        events.append(build_source_document_payload(s))
    return events


class QARequest(BaseModel):
    question: str
    kb_ids: list[str]
    top_k: int = 5


class QASource(BaseModel):
    kb_id: str
    doc_id: str
    doc_source: str
    content_snippet: str
    page_number: Optional[int] = None
    relevance: float
    # V8: block_range 坐标（[start, end] 0-based block_order 闭区间）;
    # None = 非 PDF / 旧 KB / 匹配失败 → 前端 fallback 到 highlight 字符串匹配。
    block_range: Optional[list[int]] = None


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
                    kb_id=s.get("kb_id", ""),
                    doc_id=s.get("doc_id", ""),
                    doc_source=s.get("doc_source", "未知来源"),
                    content_snippet=s.get("content_snippet", ""),
                    page_number=s.get("page_number"),
                    relevance=s.get("relevance", 1.0),
                    block_range=s.get("block_range"),
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
        # PRD #67: 跨 agentic / RAG 两条分支共用同一 dedupe 屏障。
        # 本端发 source-document 时按 doc_id 首次出现时唯一 emit，AI SDK 再用
        # sourceId 跨 message 去重；两边 dedupe 是独立的。
        seen_doc_ids: set[str] = set()

        if _AGENTIC_QA:
            from services.agentic_qa import run_agentic_qa
            import uuid

            event_queue: queue.Queue = queue.Queue()
            qa_id = uuid.uuid4().hex[:12]
            result_container: dict = {}

            def _run_agentic():
                result = run_agentic_qa(
                    question, kb_ids,
                    chat_history=chat_history if chat_history else None,
                    event_callback=event_queue.put,
                    qa_id=qa_id,
                )
                result_container["result"] = result
                event_queue.put(None)  # sentinel

            thread = threading.Thread(target=_run_agentic, daemon=True)
            thread.start()

            text_started = False
            # 记录每个 tool_call 的 toolCallId，为后续 tool_result 关联
            pending_tool_calls: dict[str, str] = {}  # tool_name → toolCallId
            step_counter = 0
            current_rid = ""  # 当前推理块的 ID，跨 reasoning_start/delta/end 复用

            # 发送流开始（messageMetadata 确保 AI SDK 立即触发 write()，让前端即时显示加载指示器）
            yield _sse("start", {"type": "start", "messageMetadata": {}})

            while True:
                try:
                    event = event_queue.get(timeout=120)
                except queue.Empty:
                    break

                if event is None:
                    break

                t = event.get("type", "")

                if t == "start":
                    yield _sse("start-step", {"type": "start-step"})

                elif t == "reasoning_start":
                    step_counter += 1
                    current_rid = f"reasoning-{step_counter}"
                    yield _sse("reasoning-start", {"type": "reasoning-start", "id": current_rid})

                elif t == "reasoning_delta":
                    if current_rid:
                        yield _sse("reasoning-delta", {
                            "type": "reasoning-delta",
                            "id": current_rid,
                            "delta": event.get("content", ""),
                        })

                elif t == "reasoning_end":
                    if current_rid:
                        yield _sse("reasoning-end", {"type": "reasoning-end", "id": current_rid})
                    current_rid = ""

                elif t == "text_start":
                    text_started = True
                    yield _sse("text-start", {"type": "text-start", "id": TEXT_PART_ID})

                elif t == "text_delta":
                    yield _sse("text-delta", {
                        "type": "text-delta",
                        "id": TEXT_PART_ID,
                        "delta": event.get("content", ""),
                    })

                elif t == "text_end":
                    yield _sse("text-end", {"type": "text-end", "id": TEXT_PART_ID})

                elif t == "tool_call":
                    func_name = event.get("tool", "unknown")
                    step_counter += 1
                    call_id = f"call-{step_counter}"
                    pending_tool_calls[func_name] = call_id
                    yield _sse("tool-input-start", {
                        "type": "tool-input-start",
                        "toolCallId": call_id,
                        "toolName": func_name,
                    })
                    yield _sse("tool-input-available", {
                        "type": "tool-input-available",
                        "toolCallId": call_id,
                        "toolName": func_name,
                        "input": event.get("args", {}),
                    })

                elif t == "tool_result":
                    func_name = event.get("tool", "unknown")
                    call_id = pending_tool_calls.pop(func_name, f"call-{step_counter}")
                    tool_content = event.get("content", "")
                    yield _sse("tool-output-available", {
                        "type": "tool-output-available",
                        "toolCallId": call_id,
                        "output": tool_content,
                    })
                    # PRD #67: tool-output-available 紧随其后下发 source-document
                    # （仅该 doc_id 首次出现时），保持流顺序与文本交错。
                    for src_event in _build_source_document_events(
                        parse_search_kb_tool_output(tool_content), seen_doc_ids
                    ):
                        yield _sse("source-document", src_event)

                elif t == "error":
                    yield _sse("error", {
                        "type": "error",
                        "errorText": event.get("message", "未知错误"),
                    })

            # 发送最终结果
            result = result_container.get("result", {})
            answer_text = result.get("answer", "")
            if answer_text and not text_started:
                # 错误/降级情况：没有流式文本输出，发一次完整的 text 事件
                yield _sse("text-start", {"type": "text-start", "id": TEXT_PART_ID})
                yield _sse("text-delta", {"type": "text-delta", "id": TEXT_PART_ID, "delta": answer_text})
                yield _sse("text-end", {"type": "text-end", "id": TEXT_PART_ID})
            yield _sse("finish-step", {"type": "finish-step"})
            yield _sse("finish", {"type": "finish", "finishReason": "stop"})

        else:
            # 原始 RAG 管道流式
            yield _sse("start", {"type": "start", "messageMetadata": {}})
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
                    # PRD #67: 用 source-document parts 替换 data-sources。
                    # RAG 路径在 done 时一次性拿到 sources；按流顺序 emit。
                    for src_event in _build_source_document_events(
                        event.get("sources") or [], seen_doc_ids
                    ):
                        yield _sse("source-document", src_event)
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
