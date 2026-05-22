"""知识问答服务 — RAG 管线 + 多轮对话。

流程：
- ask(): 单轮问答，无记忆
- chat(): 多轮对话，按 session 维护会话历史
"""

import logging
import time
import uuid
from typing import Any

from core.settings import get_llm
from llama_index.core.llms import ChatMessage, MessageRole

_logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一个企业制度知识问答助手。你擅长根据企业规章制度回答员工提问。

## 回答规则

1. **仅基于提供的知识库内容回答**，不得使用你的预训练知识
2. **引用具体文档来源**：在回答中标注信息来源（如"根据《公司消防安全管理规定》..."）
3. **知识库内容不足时**：请明确指出"根据现有制度库，未找到相关信息"
4. **回答要求**：简洁、专业、条理清晰，使用中文
5. **多文档综合**：如果多个制度涉及同一问题，请综合回答并分别注明来源"""


# ── 单轮问答（无记忆）────────────────────────────────────────────────────────────

def ask(kb_ids: list[str], question: str, top_k: int = 5) -> dict[str, Any]:
    """对知识库提问，返回答案和相关参考来源。"""
    chunks = _search(kb_ids, question, top_k)
    if not chunks:
        return {"answer": "根据现有制度库，未找到相关信息。", "sources": []}

    context = _build_context(chunks)
    answer = _call_llm(SYSTEM_PROMPT, _build_user_prompt(question, context))
    sources = _build_sources(chunks)
    return {"answer": answer, "sources": sources}


# ── 多轮对话（按 session 记忆）────────────────────────────────────────────────────

MAX_SESSION_AGE = 7200  # 会话过期时间：2 小时
_sessions: dict[str, dict] = {}  # session_id → {"history": [...], "kb_ids": [...], "created_at": float}


def _cleanup_sessions():
    """清理过期会话。"""
    now = time.time()
    expired = [sid for sid, s in _sessions.items() if now - s["created_at"] > MAX_SESSION_AGE]
    for sid in expired:
        del _sessions[sid]


def _get_or_create_session(session_id: str | None, kb_ids: list[str]) -> tuple[str, list[dict]]:
    """获取或创建会话。返回 (session_id, history)。"""
    if not session_id:
        session_id = uuid.uuid4().hex[:12]

    _cleanup_sessions()

    if session_id in _sessions:
        session = _sessions[session_id]
        # 如果 kb_ids 变化，清空历史重新开始
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
    """将会话历史格式化为文本片段。"""
    if not history:
        return ""
    lines = []
    for msg in history:
        role = "用户" if msg["role"] == "user" else "助手"
        lines.append(f"{role}：{msg['content']}")
    return "\n".join(lines)


def chat(
    session_id: str | None,
    question: str,
    kb_ids: list[str],
    top_k: int = 5,
) -> dict[str, Any]:
    """多轮对话。自动管理会话历史，支持追问。

    Args:
        session_id: 会话 ID。传 None 或新 ID 会创建新会话。
        question: 用户问题。
        kb_ids: 知识库 ID 列表。
        top_k: 检索 chunk 数量。

    Returns:
        {"session_id": str, "answer": str, "sources": list[dict]}
    """
    session_id, history = _get_or_create_session(session_id, kb_ids)

    # 1. 向量检索
    chunks = _search(kb_ids, question, top_k)

    answer = ""
    sources = []

    if not chunks:
        answer = "根据现有制度库，未找到相关信息。"
    else:
        # 2. 构建上下文
        context = _build_context(chunks)

        # 3. 构建包含历史的消息
        history_text = _format_history(history)
        user_prompt = _build_user_prompt(question, context, history_text if history_text else None)

        # 4. 调用 LLM
        answer = _call_llm(SYSTEM_PROMPT, user_prompt)
        if not answer:
            answer = "抱歉，回答生成失败。"

        # 5. 整理来源
        sources = _build_sources(chunks)

    # 6. 保存历史
    history.append({"role": "user", "content": question})
    history.append({"role": "assistant", "content": answer})
    # 限制历史长度，避免撑爆上下文
    if len(history) > 10:
        history[:] = history[-10:]

    return {"session_id": session_id, "answer": answer, "sources": sources}


# ── 共享工具函数 ─────────────────────────────────────────────────────────────────


def _search(kb_ids: list[str], query: str, top_k: int) -> list[dict]:
    """向量检索。"""
    from services.vector_search import search as vec_search
    return vec_search(kb_ids, query, max_results=top_k)


def _build_context(chunks: list[dict]) -> str:
    """将检索结果格式化为上下文文本。"""
    context_parts = []
    for i, c in enumerate(chunks, 1):
        src = c.get("doc_source", "未知来源")
        content = c.get("content", "")[:1000]
        context_parts.append(f"[{i}] 来源：{src}\n{content}")
    return "\n\n---\n\n".join(context_parts)


def _build_user_prompt(question: str, context: str, history: str | None = None) -> str:
    """构建用户 prompt。"""
    parts = []

    if history:
        parts.append(f"## 对话历史\n\n{history}\n")

    parts.append(f"## 知识库内容\n\n{context}")
    parts.append(f"## 用户问题\n\n{question}")
    parts.append("请回答用户问题，严格遵循以上回答规则。")

    return "\n\n".join(parts)


def _call_llm(system: str, user: str) -> str:
    """调用 LLM，返回回答文本。"""
    try:
        messages = [
            ChatMessage(role=MessageRole.SYSTEM, content=system),
            ChatMessage(role=MessageRole.USER, content=user),
        ]
        response = get_llm().chat(messages)
        return response.message.content or ""
    except Exception as e:
        _logger.warning("qa llm call failed: %s", e)
        return ""


def _build_sources(chunks: list[dict]) -> list[dict]:
    """从检索结果整理来源列表。"""
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
