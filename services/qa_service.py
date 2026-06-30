"""知识问答服务 — RAG 管线 + 多轮对话。

- ask(): 单轮问答，使用 LlamaIndex RetrieverQueryEngine
- chat(): 多轮对话，使用 LlamaIndex ContextChatEngine + ChatMemoryBuffer
- chat_stream(): 流式多轮对话
"""

import logging
import time
import uuid
from typing import Any, Generator

from llama_index.core import PromptTemplate
from llama_index.core.chat_engine import ContextChatEngine
from llama_index.core.memory import ChatMemoryBuffer
from llama_index.core.query_engine import RetrieverQueryEngine
from llama_index.core.response_synthesizers import get_response_synthesizer

from core.retriever import CrossKBRetriever
from core.settings import get_llm, get_embed_model

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

# ── 多轮对话专用 System Prompt（含追问建议指令）─────────────────────────────────

CHAT_SYSTEM_PROMPT = """你是一个企业制度知识问答助手。你擅长根据企业规章制度回答员工提问。

## 回答规则

1. **仅基于提供的知识库内容回答**，不得使用你的预训练知识
2. **引用具体文档来源**：在回答中标注信息来源（如"根据《公司消防安全管理规定》..."）
3. **知识库内容不足时**：请明确指出"根据现有制度库，未找到相关信息"
4. **回答要求**：简洁、专业、条理清晰，使用中文
5. **多文档综合**：如果多个制度涉及同一问题，请综合回答并分别注明来源
6. **追问建议**：在回答的最后，提供 2-3 个相关的追问问题，每个问题单独一行，以【追问】开头。例如：
   【追问】该制度的适用范围是什么？
   【追问】违规行为的具体处罚标准是什么？
   如果知识库内容不足，则不需要提供追问。"""

# ── QueryEngine 缓存（按 (kb_ids_tuple, top_k) 复用）───────────────────────────

_query_engines: dict[tuple, RetrieverQueryEngine] = {}
_embed_initialized = False


def _get_query_engine(kb_ids: list[str], top_k: int = 5) -> RetrieverQueryEngine:
    """获取或创建 QueryEngine（覆盖 Embed + LLM 初始化）。"""
    global _embed_initialized
    if not _embed_initialized:
        try:
            get_embed_model()
            get_llm()
            _embed_initialized = True
        except Exception as e:
            _logger.warning("embed/llm init failed: %s", e)
            raise RuntimeError(f"模型初始化失败: {e}")

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
            "page_number": n.metadata.get("page_number"),
            "relevance": round(n.score or 0, 4),
        }
        for n in response.source_nodes
    ]

    if not sources and not answer:
        answer = "根据现有制度库，未找到相关信息。"

    return {"answer": answer, "sources": sources}


# ── 多轮对话（ContextChatEngine + ChatMemoryBuffer）─────────────────────────────

MAX_SESSION_AGE = 7200
_sessions: dict[str, dict] = {}


def _cleanup_sessions():
    now = time.time()
    expired = [sid for sid, s in _sessions.items() if now - s["created_at"] > MAX_SESSION_AGE]
    for sid in expired:
        del _sessions[sid]


def _build_chat_engine(kb_ids: list[str], top_k: int) -> ContextChatEngine:
    """创建 ContextChatEngine 实例。

    内置 ChatMemoryBuffer 管理对话历史，自动进行 token 感知的截断。
    """
    retriever = CrossKBRetriever(kb_ids=kb_ids, top_k=top_k)
    memory = ChatMemoryBuffer.from_defaults(token_limit=4000)
    node_postprocessors = []
    # QA 场景不使用 reranker（按需加载延迟较高，QA 需要快速响应）
    # 向量搜索结果已足够用于问答
    return ContextChatEngine.from_defaults(
        retriever=retriever,
        memory=memory,
        system_prompt=CHAT_SYSTEM_PROMPT,
        node_postprocessors=node_postprocessors,
    )


def _get_or_create_engine(
    session_id: str | None, kb_ids: list[str], top_k: int
) -> tuple[str, ContextChatEngine]:
    """获取或按 session 创建 ChatEngine（每个 session 独立记忆）。"""
    if not session_id:
        session_id = uuid.uuid4().hex[:12]
    _cleanup_sessions()

    if session_id in _sessions:
        session = _sessions[session_id]
        if session["kb_ids"] != kb_ids:
            # KB 列表变更 → 重建引擎（历史重置）
            session["engine"] = _build_chat_engine(kb_ids, top_k)
            session["kb_ids"] = kb_ids
            session["created_at"] = time.time()
        return session_id, session["engine"]

    engine = _build_chat_engine(kb_ids, top_k)
    _sessions[session_id] = {
        "engine": engine,
        "kb_ids": kb_ids,
        "created_at": time.time(),
    }
    return session_id, engine


def _extract_suggestions(text: str) -> list[str]:
    """从 LLM 回答中提取【追问】行。"""
    suggestions = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("【追问】"):
            suggestions.append(stripped.replace("【追问】", "").strip())
    return suggestions


def _strip_suggestions(text: str) -> str:
    """移除 LLM 回答中的【追问】行。"""
    lines = [line for line in text.split("\n") if not line.strip().startswith("【追问】")]
    return "\n".join(lines).strip()


def _sources_from_nodes(source_nodes) -> list[dict]:
    """从 NodeWithScore 列表构建 sources 响应（与 ask() 格式一致）。"""
    return [
        {
            "kb_id": n.metadata.get("kb_id", ""),
            "doc_id": n.metadata.get("doc_id", ""),
            "doc_source": n.metadata.get("source", ""),
            "content_snippet": n.node.text[:300],
            "page_number": n.metadata.get("page_number"),
            "relevance": round(n.score or 0, 4),
        }
        for n in source_nodes
    ]


def chat(
    session_id: str | None,
    question: str,
    kb_ids: list[str],
    top_k: int = 5,
) -> dict[str, Any]:
    """多轮对话。使用 ContextChatEngine（自动管理历史 + 来源追踪）。"""
    global _embed_initialized
    if not _embed_initialized:
        try:
            get_embed_model()
            get_llm()
            _embed_initialized = True
        except Exception as e:
            _logger.warning("embed/llm init failed in chat: %s", e)
            raise RuntimeError(f"模型初始化失败: {e}")

    session_id, engine = _get_or_create_engine(session_id, kb_ids, top_k)

    try:
        response = engine.chat(question)
        raw_answer = str(response.response or "")
        suggestions = _extract_suggestions(raw_answer)
        clean_answer = _strip_suggestions(raw_answer)
        sources = _sources_from_nodes(response.source_nodes)
    except Exception as e:
        _logger.warning("qa llm call failed: %s", e)
        clean_answer = "抱歉，回答生成失败。"
        sources = []

    if not sources and not clean_answer:
        clean_answer = "根据现有制度库，未找到相关信息。"

    return {"session_id": session_id, "answer": clean_answer, "sources": sources}


def chat_stream(
    session_id: str | None,
    question: str,
    kb_ids: list[str],
    top_k: int = 5,
) -> Generator[dict[str, Any], None, None]:
    """多轮对话流式版本。使用 ContextChatEngine。

    将处理阶段和 LLM 回答按事件 yield，供 SSE 端点消费。
    """
    global _embed_initialized
    if not _embed_initialized:
        yield {"type": "progress", "stage": "load_model", "label": "正在加载 AI 模型..."}
        try:
            get_embed_model()
            get_llm()
            _embed_initialized = True
        except Exception as e:
            _logger.warning("embed/llm init failed in chat_stream: %s", e)
            yield {"type": "error", "message": f"模型初始化失败: {e}"}
            return

    session_id, engine = _get_or_create_engine(session_id, kb_ids, top_k)

    yield {"type": "progress", "stage": "search", "label": "正在检索知识库..."}
    yield {"type": "progress", "stage": "llm", "label": "正在生成回答..."}

    answer = ""
    try:
        response = engine.stream_chat(question)
        for chunk in response:
            delta = chunk.delta or ""
            if delta:
                answer += delta
                yield {"type": "token", "text": delta}
    except Exception as e:
        _logger.warning("qa llm stream failed: %s", e)
        if not answer:
            yield {"type": "error", "message": "回答生成失败。"}
            return

    # 回答完成 → 构建 sources 和 suggestions
    sources = _sources_from_nodes(response.source_nodes)
    suggestions = _extract_suggestions(answer)

    yield {"type": "done", "session_id": session_id, "sources": sources, "suggestions": suggestions}
