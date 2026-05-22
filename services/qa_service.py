"""知识问答服务 — RAG 管线 + 多轮对话。

- ask(): 单轮问答，使用 LlamaIndex RetrieverQueryEngine
- chat(): 多轮对话，使用 CrossKBRetriever + 外部历史管理
"""

import logging
import time
import uuid
from typing import Any

from llama_index.core import PromptTemplate
from llama_index.core.query_engine import RetrieverQueryEngine
from llama_index.core.response_synthesizers import get_response_synthesizer

from core.retriever import CrossKBRetriever
from core.settings import get_llm, get_embed_model
from llama_index.core.llms import ChatMessage, MessageRole

_logger = logging.getLogger(__name__)

# ── 自定义 QA prompt（替代默认的 Context + Query 模板）─────────────────────────

QA_PROMPT_TMPL = """你是一个企业制度知识问答助手。你擅长根据企业规章制度回答员工提问。

## 回答规则

1. **仅基于提供的知识库内容回答**，不得使用你的预训练知识
2. **引用具体文档来源**：在回答中标注信息来源（如"根据《公司消防安全管理规定》..."）
3. **知识库内容不足时**：请明确指出"根据现有制度库，未找到相关信息"
4. **回答要求**：简洁、专业、条理清晰，使用中文
5. **多文档综合**：如果多个制度涉及同一问题，请综合回答并分别注明来源

## 知识库内容

{context_str}

## 用户问题

{query_str}

请回答用户问题，严格遵循以上回答规则。"""

QA_PROMPT = PromptTemplate(QA_PROMPT_TMPL)

# ── QueryEngine 缓存（按 (kb_ids_tuple, top_k) 复用）───────────────────────────

_query_engines: dict[tuple, RetrieverQueryEngine] = {}
_embed_initialized = False


def _get_query_engine(kb_ids: list[str], top_k: int = 5) -> RetrieverQueryEngine:
    """获取或创建 QueryEngine（覆盖 Embed + LLM 初始化）。"""
    global _embed_initialized
    if not _embed_initialized:
        get_embed_model()
        get_llm()
        _embed_initialized = True

    key = (tuple(sorted(kb_ids)), top_k)
    if key not in _query_engines:
        retriever = CrossKBRetriever(kb_ids=kb_ids, top_k=top_k)
        synth = get_response_synthesizer(
            text_qa_template=QA_PROMPT,
            response_mode="compact",
        )
        _query_engines[key] = RetrieverQueryEngine(
            retriever=retriever,
            response_synthesizer=synth,
        )
    return _query_engines[key]


# ── 单轮问答 ────────────────────────────────────────────────────────────────────


def ask(kb_ids: list[str], question: str, top_k: int = 5) -> dict[str, Any]:
    """单轮问答。使用 RetrieverQueryEngine。"""
    engine = _get_query_engine(kb_ids, top_k)
    response = engine.query(question)

    answer = str(response.response or "")
    sources = [
        {
            "kb_id": n.metadata.get("kb_id", ""),
            "doc_id": n.metadata.get("doc_id", ""),
            "doc_source": n.metadata.get("source", ""),
            "content_snippet": n.node.text[:300],
            "relevance": round(n.score or 0, 4),
        }
        for n in response.source_nodes
    ]

    if not sources and not answer:
        answer = "根据现有制度库，未找到相关信息。"

    return {"answer": answer, "sources": sources}


# ── 多轮对话（按 session 记忆）───────────────────────────────────────────────────

MAX_SESSION_AGE = 7200
_sessions: dict[str, dict] = {}


def _cleanup_sessions():
    now = time.time()
    expired = [sid for sid, s in _sessions.items() if now - s["created_at"] > MAX_SESSION_AGE]
    for sid in expired:
        del _sessions[sid]


def _get_or_create_session(session_id: str | None, kb_ids: list[str]) -> tuple[str, list[dict]]:
    if not session_id:
        session_id = uuid.uuid4().hex[:12]
    _cleanup_sessions()
    if session_id in _sessions:
        session = _sessions[session_id]
        if session["kb_ids"] != kb_ids:
            session["history"] = []
            session["kb_ids"] = kb_ids
    else:
        _sessions[session_id] = {
            "history": [],
            "kb_ids": kb_ids,
            "created_at": time.time(),
        }
    return session_id, _sessions[session_id]["history"]


def _format_history(history: list[dict]) -> str:
    if not history:
        return ""
    lines = []
    for msg in history:
        role = "用户" if msg["role"] == "user" else "助手"
        lines.append(f"{role}：{msg['content']}")
    return "\n".join(lines)


def _search(kb_ids: list[str], query: str, top_k: int) -> list[dict]:
    """向量检索（用于多轮对话的上下文构建）。"""
    from services.vector_search import search as vec_search
    return vec_search(kb_ids, query, max_results=top_k)


def _build_context(chunks: list[dict]) -> str:
    context_parts = []
    for i, c in enumerate(chunks, 1):
        src = c.get("doc_source", "未知来源")
        content = c.get("content", "")[:1000]
        context_parts.append(f"[{i}] 来源：{src}\n{content}")
    return "\n\n---\n\n".join(context_parts)


def _build_sources(chunks: list[dict]) -> list[dict]:
    return [
        {
            "kb_id": c.get("kb_id", ""),
            "doc_id": c.get("doc_id", ""),
            "doc_source": c.get("doc_source", ""),
            "content_snippet": (c.get("content", "") or "")[:300],
            "relevance": c.get("relevance", 0),
        }
        for c in chunks
    ]


def chat(
    session_id: str | None,
    question: str,
    kb_ids: list[str],
    top_k: int = 5,
) -> dict[str, Any]:
    """多轮对话。自动管理会话历史，支持追问。"""
    # 确保 LLM 和 Embed 已初始化
    if not _embed_initialized:
        get_embed_model()
        get_llm()

    session_id, history = _get_or_create_session(session_id, kb_ids)
    chunks = _search(kb_ids, question, top_k)

    answer = ""
    sources = []

    if not chunks:
        answer = "根据现有制度库，未找到相关信息。"
    else:
        context = _build_context(chunks)
        history_text = _format_history(history) if history else None

        parts = []
        if history_text:
            parts.append(f"## 对话历史\n\n{history_text}\n")
        parts.append(f"## 知识库内容\n\n{context}")
        parts.append(f"## 用户问题\n\n{question}")
        parts.append("请回答用户问题，严格遵循以上回答规则。")

        user_prompt = "\n\n".join(parts)
        try:
            messages = [
                ChatMessage(role=MessageRole.SYSTEM, content=QA_PROMPT_TMPL.split("## 知识库内容", 1)[0].strip()),
                ChatMessage(role=MessageRole.USER, content=user_prompt),
            ]
            response = get_llm().chat(messages)
            answer = response.message.content or ""
        except Exception as e:
            _logger.warning("qa llm call failed: %s", e)
            answer = ""

        if not answer:
            answer = "抱歉，回答生成失败。"

        sources = _build_sources(chunks)

    history.append({"role": "user", "content": question})
    history.append({"role": "assistant", "content": answer})
    if len(history) > 10:
        history[:] = history[-10:]

    return {"session_id": session_id, "answer": answer, "sources": sources}
