import json
import re
from typing import Optional

from models.audit_document import AuditDocument, DocumentStructure, Chapter, Clause
import storage.audit_doc_repo as repo
from services.llm_client import generate, generate_with_tools
from services.structure_parser import parse_markdown_structure


def _is_markdown_content(content: str) -> bool:
    """判断 parsed_content 是否为 MinerU 生成的 Markdown。"""
    return bool(re.search(r'^#\s+\d+\.', content, re.MULTILINE)) or "<table>" in content


def identify_structure(doc: AuditDocument) -> DocumentStructure:
    """识别文档结构。

    优先级：
    1. 如果是 MinerU Markdown → 纯 Python 解析（零 LLM，覆盖全文）
    2. 如果是纯文本 → LLM Function Calling
    3. 失败时 → 正则降级
    """
    if not doc.parsed_content:
        raise ValueError("文档未解析，请先调用 parse_document")

    # MinerU Markdown → 纯 Python 解析
    if _is_markdown_content(doc.parsed_content):
        try:
            structure = parse_markdown_structure(doc.parsed_content)
            if structure.chapters:
                return structure
        except Exception:
            pass  # 降级到 LLM

    # 纯文本（pdfplumber/python-docx）→ LLM Function Calling
    try:
        return _identify_structure_with_llm(doc)
    except Exception as e:
        return _fallback_parse(doc.parsed_content)


def _identify_structure_with_llm(doc: AuditDocument) -> DocumentStructure:
    """使用 LLM Function Calling 识别文档结构（降级方案，纯文本输入时使用）。"""
    content_preview = doc.parsed_content[:8000]

    lines = content_preview.split("\n")
    compact_lines = []
    for i, line in enumerate(lines):
        s = line.strip()
        if not s:
            continue
        if len(s) < 35:
            compact_lines.append(s)
        elif s[0].isdigit() or (len(s) > 1 and s[0] in "（(" and s[1].isdigit()):
            compact_lines.append(s[:100])
        elif any(k in s for k in ["要求", "功能", "性能", "指标", "规格", "标准",
                                   "系统", "平台", "模块", "接口", "概述", "范围"]):
            compact_lines.append(s[:100])
        elif i % 5 == 0:
            compact_lines.append(s[:80])

    content_compact = "\n".join(compact_lines)
    if len(content_compact) > 5000:
        content_compact = content_compact[:5000]
    if len(content_compact) < 2000 and len(content_preview) > 2000:
        content_compact = content_preview[:2000]

    system_prompt = """你是一个专业的技术文档分析专家。请分析用户提供的文档内容，提取完整的章节结构和条款列表。

注意：
1. 识别章节标题（如"第一章 总则"、"第二章 技术要求"）
2. 识别条款编号和内容（如"1.1"、"2.1.1"、"3.2.3"等）
3. 统计总条款数
4. 如果文档没有明确的章节划分，可以用"全文"作为单一章节"""

    user_prompt = f"请分析以下文档内容，提取章节结构和条款：\n\n{content_compact}"

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    tool = {
        "type": "function",
        "function": {
            "name": "extract_document_structure",
            "description": "从技术文档中提取章节和条款结构",
            "parameters": {
                "type": "object",
                "properties": {
                    "chapters": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "number": {"type": "string"},
                                "title": {"type": "string"},
                                "clauses": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "number": {"type": "string"},
                                            "text": {"type": "string"},
                                        },
                                        "required": ["number", "text"],
                                    },
                                },
                            },
                            "required": ["number", "title", "clauses"],
                        },
                    },
                    "total_clauses": {"type": "integer"},
                },
                "required": ["chapters", "total_clauses"],
            },
        },
    }

    result = generate_with_tools(
        messages=messages,
        tools=[tool],
        tool_choice={"type": "function", "function": {"name": "extract_document_structure"}},
        timeout=120,
    )

    if result["type"] == "tool_calls":
        args = result["tool_calls"][0]["arguments"]
        return _tool_args_to_structure(args)

    return _parse_llm_json(result.get("content", ""))


def _tool_args_to_structure(data: dict) -> DocumentStructure:
    """将 Function Calling 的参数转换为 DocumentStructure 对象。"""
    chapters = []
    for ch_data in data.get("chapters", []):
        clauses = [
            Clause(number=c["number"], text=c["text"])
            for c in ch_data.get("clauses", [])
        ]
        chapters.append(Chapter(
            number=ch_data.get("number"),
            title=ch_data.get("title", ""),
            clauses=clauses,
        ))

    return DocumentStructure(
        title=data.get("title"),
        chapters=chapters,
        total_clauses=data.get("total_clauses", len(chapters)),
    )


def _parse_llm_json(llm_output: str) -> DocumentStructure:
    """解析 LLM 输出的 JSON（降级方案，无 function calling 时使用）。"""
    json_match = re.search(r"\{[\s\S]*\}", llm_output)
    if not json_match:
        return _fallback_parse("")

    try:
        data = json.loads(json_match.group())
        return _tool_args_to_structure(data)
    except (json.JSONDecodeError, KeyError):
        return _fallback_parse("")


def _fallback_parse(content: str) -> DocumentStructure:
    """降级解析：使用规则匹配章节和条款。"""
    chapters = []

    # 匹配章节标题
    chapter_pattern = r'(第[一二三四五六七八九十百]+章\s*[^\n]+|第[0-9]+章\s*[^\n]+)'
    chapter_matches = re.findall(chapter_pattern, content)

    if chapter_matches:
        for i, match in enumerate(chapter_matches):
            chapters.append(Chapter(
                number=match.split()[0] if match.split() else None,
                title=match,
                clauses=[],
            ))
    else:
        chapters.append(Chapter(
            title="文档内容",
            clauses=[],
        ))

    # 匹配条款编号
    clause_pattern = r'(\d+(?:\.\d+)+)\s+([^\n]{10,200})'
    clause_matches = re.findall(clause_pattern, content)
    total_clauses = len(clause_matches)

    return DocumentStructure(
        chapters=chapters,
        total_clauses=total_clauses,
    )


def analyze_document_structure(doc_id: str) -> AuditDocument:
    """分析文档结构并更新文档。"""
    doc = repo.get_doc(doc_id)
    if not doc:
        raise ValueError(f"文档不存在: {doc_id}")

    if not doc.parsed_content:
        raise ValueError("文档未解析")

    doc.structure = identify_structure(doc)
    doc.status = "indexed"
    return repo.update_doc(doc)


def get_document_structure(doc_id: str) -> DocumentStructure | None:
    """获取文档结构。"""
    doc = repo.get_doc(doc_id)
    if not doc:
        return None
    return doc.structure
