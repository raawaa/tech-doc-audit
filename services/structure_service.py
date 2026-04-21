import json
import os
import re
import httpx
from typing import Optional

from models.audit_document import AuditDocument, DocumentStructure, Chapter, Clause
import storage.audit_doc_repo as repo


OLLAMA_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
MODEL = os.environ.get("OLLAMA_MODEL", "qwen3.5:0.8b")


def identify_structure(doc: AuditDocument) -> DocumentStructure:
    """使用 LLM 识别文档结构。"""
    if not doc.parsed_content:
        raise ValueError("文档未解析，请先调用 parse_document")

    # 提取前 8000 字符进行分析（控制 token 数量）
    content_preview = doc.parsed_content[:8000]

    prompt = f"""分析以下技术文档，提取文档结构和条款。

要求：
1. 识别章节标题（如"第一章 总则"、"第二章 技术要求"）
2. 识别条款编号和内容（如"1.1"、"2.1.1"、"3.2.3"等）
3. 统计总条款数

输出格式（JSON）：
{{
  "title": "文档标题（如果能找到）",
  "chapters": [
    {{
      "number": "第一章",
      "title": "总则",
      "clauses": [
        {{"number": "1.1", "text": "条款内容摘要（最多100字）"}}
      ]
    }}
  ],
  "total_clauses": 数字
}}

文档内容：
{content_preview}

请直接输出 JSON，不要包含其他内容。"""

    try:
        response = httpx.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": MODEL,
                "prompt": prompt,
                "stream": False,
            },
            timeout=120,
        )
        response.raise_for_status()
        data = response.json()
        llm_output = data.get("response", "")

        # 提取 JSON
        structure = _parse_llm_json(llm_output)
        return structure

    except Exception as e:
        # LLM 失败时使用规则解析作为降级
        return _fallback_parse(doc.parsed_content)


def _parse_llm_json(llm_output: str) -> DocumentStructure:
    """解析 LLM 输出的 JSON。"""
    # 尝试提取 JSON 块
    json_match = re.search(r"\{[\s\S]*\}", llm_output)
    if not json_match:
        return _fallback_parse("")

    try:
        data = json.loads(json_match.group())
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
        # 没有章节，使用整个内容作为一个大章节
        chapters.append(Chapter(
            title="文档内容",
            clauses=[],
        ))

    # 匹配条款编号
    clause_pattern = r'(\d+(?:\.\d+)+)\s+([^\n]{10,200})'
    clause_matches = re.findall(clause_pattern, content)

    # 统计条款
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
