"""PageIndex 推理式检索服务。

使用 PageIndex 树索引进行 agentic 检索：
1. LLM 先查看文档树结构（不含文本，省 token）
2. LLM 推理决定哪些分支可能相关
3. LLM 请求获取特定分支的文本内容
4. 返回匹配的标准条文
"""

import json
from typing import Optional

import storage.kb_repo as kb_repo
import storage.doc_repo as doc_repo
import storage.index_repo as index_repo
from services.llm_client import generate_with_tools


# ── PageIndex 检索工具定义 ──────────────────────────────────────────────────

STRUCTURE_TOOL = {
    "type": "function",
    "function": {
        "name": "get_document_structure",
        "description": "获取知识库文档的目录结构（Tree TOC）。返回章节标题和层级关系，不包含正文文本（节省token）。先调这个了解文档结构。",
        "parameters": {
            "type": "object",
            "properties": {
                "doc_id": {
                    "type": "string",
                    "description": "文档ID",
                },
            },
            "required": ["doc_id"],
        },
    },
}

CONTENT_TOOL = {
    "type": "function",
    "function": {
        "name": "get_page_content",
        "description": "获取文档指定页/章节的完整文本内容。先调 get_document_structure 确定要查看的章节，再调此函数获取具体内容。pages 格式: '1-3'（第1到3页）、'5,8,10'（第5/8/10页）、或 '12'（单页）。",
        "parameters": {
            "type": "object",
            "properties": {
                "doc_id": {
                    "type": "string",
                    "description": "文档ID",
                },
                "pages": {
                    "type": "string",
                    "description": "页码范围，如 '1-3'、'5,8,10'、'12'",
                },
            },
            "required": ["doc_id", "pages"],
        },
    },
}

SEARCH_TOOLS = [STRUCTURE_TOOL, CONTENT_TOOL]


# ── 数据加载 ────────────────────────────────────────────────────────────────

def _load_kb_docs(kb_ids: list[str]) -> list[dict]:
    """加载知识库中文档的索引数据。"""
    docs_info = []
    for kb_id in kb_ids:
        kb = kb_repo.get(kb_id)
        if not kb:
            continue
        for doc in doc_repo.list_docs(kb_id):
            if doc.index_status != "ready":
                continue
            tree = index_repo.load_index(kb_id, doc.id)
            if not tree:
                continue
            docs_info.append({
                "doc_id": doc.id,
                "doc_name": doc.name,
                "kb_id": kb_id,
                "kb_name": kb.name,
                "tree": tree,
            })
    return docs_info


# ── 工具实现 ────────────────────────────────────────────────────────────────

def _execute_structure_tool(doc_id: str, docs_info: list[dict]) -> str:
    """执行 get_document_structure: 返回树结构的精简版本。"""
    for d in docs_info:
        if d["doc_id"] == doc_id:
            tree = d["tree"]
            # 移除长文本保留骨架
            return _tree_to_structure(tree, doc_id, d["doc_name"])
    return json.dumps({"error": f"文档 {doc_id} 未找到"})


def _tree_to_structure(tree: dict, doc_id: str, doc_name: str) -> str:
    """将 PageIndex 树转为精简的目录结构 JSON（不含长文本）。"""
    result = {
        "doc_id": doc_id,
        "doc_name": doc_name,
        "structure": _strip_text(tree.get("nodes", tree.get("structure", []))),
    }
    if tree.get("title"):
        result["title"] = tree["title"]
    if tree.get("doc_description"):
        result["description"] = tree["doc_description"]
    return json.dumps(result, ensure_ascii=False)


def _strip_text(nodes: list) -> list:
    """递归移除节点中的 text 字段（保留 title/summary/层级信息）。"""
    result = []
    for node in nodes:
        clean = {k: v for k, v in node.items() if k != "text" and k != "content"}
        if "text_summary" in node:
            clean["text_summary"] = node["text_summary"][:100] if node["text_summary"] else ""
        if "nodes" in node and node["nodes"]:
            clean["nodes"] = _strip_text(node["nodes"])
        result.append(clean)
    return result


def _execute_content_tool(doc_id: str, pages: str, docs_info: list[dict]) -> str:
    """执行 get_page_content: 返回指定页/节点的文本内容。"""
    for d in docs_info:
        if d["doc_id"] == doc_id:
            return _extract_pages(d["tree"], pages, d["doc_name"])
    return json.dumps({"error": f"文档 {doc_id} 未找到"})


def _extract_pages(tree: dict, pages: str, doc_name: str) -> str:
    """从树中提取指定页/节点的文本内容。"""
    # 解析页码范围
    page_nums = set()
    for part in pages.split(","):
        part = part.strip()
        if "-" in part:
            s, e = part.split("-", 1)
            page_nums.update(range(int(s.strip()), int(e.strip()) + 1))
        else:
            page_nums.add(int(part))

    # 在树中搜索匹配的节点
    results = []
    nodes = tree.get("nodes", tree.get("structure", []))
    _search_nodes(nodes, page_nums, results)

    return json.dumps({
        "doc_name": doc_name,
        "pages": pages,
        "content": results[:5] if results else "未找到该范围内的内容",
    }, ensure_ascii=False)


def _search_nodes(nodes: list, page_nums: set, results: list, depth: int = 0):
    """递归搜索树节点，找到匹配页码的节点内容。"""
    for node in nodes:
        physical_index = node.get("physical_index") or node.get("page")
        if physical_index and int(physical_index) in page_nums:
            text = node.get("text") or node.get("content") or ""
            if text:
                results.append({
                    "title": node.get("title", ""),
                    "page": int(physical_index),
                    "text": text[:2000],
                })

        # 检查 summary 是否匹配
        page_range = node.get("page_range") or node.get("pages")
        if page_range:
            try:
                rng = str(page_range)
                for pn in page_nums:
                    if f" {pn} " in f" {rng.replace('-', ' ')} ":
                        text = node.get("text") or node.get("content") or ""
                        if text:
                            results.append({
                                "title": node.get("title", ""),
                                "page_range": rng,
                                "text": text[:2000],
                            })
                            break
            except (ValueError, AttributeError):
                pass

        if node.get("nodes"):
            _search_nodes(node["nodes"], page_nums, results, depth + 1)


# ── 工具调度 ────────────────────────────────────────────────────────────────

TOOL_DISPATCH = {
    "get_document_structure": _execute_structure_tool,
    "get_page_content": _execute_content_tool,
}


def _run_tool(tool_name: str, args: dict, docs_info: list[dict]) -> str:
    """执行指定的工具并返回结果。"""
    if tool_name == "get_document_structure":
        return _execute_structure_tool(args["doc_id"], docs_info)
    elif tool_name == "get_page_content":
        return _execute_content_tool(args["doc_id"], args["pages"], docs_info)
    return json.dumps({"error": f"未知工具: {tool_name}"})


# ── 入口：Agentic 检索 ──────────────────────────────────────────────────────

def pageindex_search(kb_ids: list[str], query: str, max_results: int = 5) -> list[dict]:
    """在知识库中使用 PageIndex 推理式检索。

    流程：
    1. 加载知识库文档的 PageIndex 树索引
    2. LLM agent 使用工具查看树结构 → 推理 → 查看内容 → 返回匹配结果
    """
    docs_info = _load_kb_docs(kb_ids)
    if not docs_info:
        return []

    # 注册工具实现到 agent 可调用的函数
    doc_list = "\n".join(f"- {d['doc_name']} (ID: {d['doc_id']})" for d in docs_info)

    system_prompt = f"""你是一个专业的技术标准检索专家。你的任务是根据用户的查询，从知识库文档中找到最相关的标准条文。

可用文档：
{doc_list}

检索策略：
1. 先调 get_document_structure 查看文档目录结构
2. 根据目录判断哪些章节可能与查询相关
3. 调 get_page_content 获取具体内容
4. 如果某个文档没有相关内容，说明原因并继续查其他文档

请逐步检索，找到最相关的内容后告诉我结果。"""

    user_prompt = f"""查询内容：{query}

请从以上文档中找到与此查询最相关的标准条文。先查看文档结构，再获取具体内容。"""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    collected = []
    max_tool_calls = 8  # 防止死循环

    for turn in range(max_tool_calls):
        try:
            result = generate_with_tools(
                messages=messages,
                tools=SEARCH_TOOLS,
                tool_choice="auto",
                timeout=60,
            )

            if result["type"] == "text":
                # Agent 完成检索，返回最终结果
                collected.append({
                    "source": "pageindex_summary",
                    "content": result["content"],
                    "relevance": 0.9,
                })
                break

            # 处理 tool calls
            for tc in result["tool_calls"]:
                tool_result = _run_tool(tc["name"], tc["arguments"], docs_info)
                # 将工具结果加入对话
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": f"call_{turn}_{tc['name']}",
                        "type": "function",
                        "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"], ensure_ascii=False)},
                    }],
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": f"call_{turn}_{tc['name']}",
                    "content": tool_result,
                })

        except Exception as e:
            collected.append({
                "source": "error",
                "content": f"检索出错: {e}",
            })
            break

    # 整理结果
    formatted = []
    for item in collected:
        if item["source"] != "pageindex_summary":
            formatted.append(item)

    if not formatted and collected:
        formatted = collected

    return formatted[:max_results]


def pageindex_get_kb_content(kb_ids: list[str], query: str) -> str:
    """获取检索结果的文本表示（供审核分析使用）。"""
    results = pageindex_search(kb_ids, query, max_results=5)
    if not results:
        return "未找到相关标准依据。"

    content_parts = ["【参考标准依据（PageIndex 推理检索）】"]
    for i, r in enumerate(results, 1):
        content = r.get("content", "")
        if content:
            content_parts.append(f"\n{i}. {r.get('doc_name', '标准')}")
            content_parts.append(f"   内容: {content[:500]}")

    return "\n".join(content_parts)
