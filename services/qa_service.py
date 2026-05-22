"""知识问答服务 — RAG 管线。

流程：提问 → 向量检索 top_k chunk → LLM 综合生成答案 → 返回 {answer, sources}
"""

import logging
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


def ask(kb_ids: list[str], question: str, top_k: int = 5) -> dict[str, Any]:
    """对知识库提问，返回答案和相关参考来源。

    Args:
        kb_ids: 知识库 ID 列表。
        question: 用户问题。
        top_k: 检索 chunk 数量。

    Returns:
        {"answer": str, "sources": list[dict]}
    """
    # 1. 向量检索
    from services.vector_search import search as vec_search

    chunks = vec_search(kb_ids, question, max_results=top_k)
    if not chunks:
        return {
            "answer": "根据现有制度库，未找到相关信息。",
            "sources": [],
        }

    # 2. 构建上下文
    context_parts = []
    for i, c in enumerate(chunks, 1):
        src = c.get("doc_source", "未知来源")
        content = c.get("content", "")[:1000]
        context_parts.append(f"[{i}] 来源：{src}\n{content}")
    context = "\n\n---\n\n".join(context_parts)

    # 3. 调用 LLM
    user_prompt = f"""## 知识库内容

{context}

## 用户问题

{question}

请回答用户问题，严格遵循以上回答规则。"""

    try:
        messages = [
            ChatMessage(role=MessageRole.SYSTEM, content=SYSTEM_PROMPT),
            ChatMessage(role=MessageRole.USER, content=user_prompt),
        ]
        response = get_llm().chat(messages)
        answer = response.message.content or ""
    except Exception as e:
        _logger.warning("qa llm call failed: %s", e)
        answer = f"抱歉，回答生成失败：{e}"

    # 4. 整理来源
    sources = [
        {
            "kb_id": c.get("kb_id", ""),
            "doc_id": c.get("doc_id", ""),
            "doc_source": c.get("doc_source", ""),
            "content_snippet": (c.get("content", "") or "")[:300],
            "relevance": c.get("relevance", 0),
        }
        for c in chunks
    ]

    return {"answer": answer, "sources": sources}
