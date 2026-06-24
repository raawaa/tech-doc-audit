"""结构感知文档分块。

将已解析的文档（parsed_content）按章节/条款边界切分成语义完整的块，
每个块携带章节路径元数据，支持检索时获知完整的上下文位置。

分块策略（降级链）：
1. 有 DocumentStructure + Chapter.text → 按章节边界分块，超长章节在句末子切分
2. 有 DocumentStructure 但 Chapter.text 为空 → 用 parsed_content 定位章节区间
3. 无结构 → 降级到 SentenceSplitter（沿用现有逻辑）
"""

import re
from typing import Optional

from pydantic import BaseModel, Field

from core.logger import get_logger
from models.audit_document import DocumentStructure

_logger = get_logger(__name__)


# ── 数据模型 ───────────────────────────────────────────────────────────────────


class DocChunk(BaseModel):
    """文档块——知识库或审计文档的一个原子检索单元。"""
    chunk_id: str                   # 唯一标识
    doc_id: str                     # 所属文档 ID
    text: str                       # 块文本
    section_path: str               # "第四章 > 技术规格 > 4.2 电气要求"
    chapter_number: Optional[str] = None
    chapter_title: Optional[str] = None
    clause_number: Optional[str] = None
    clause_title: Optional[str] = None
    char_span_start: int = 0        # 在 parsed_content 中的起始字符偏移
    char_span_end: int = 0          # 在 parsed_content 中的结束字符偏移
    page_range: Optional[tuple[int, int]] = None


# ── 中文感知分隔符 ──────────────────────────────────────────────────────────────

# 按优先级从高到低排列：换段 > 换行 > 句末标点 > 句中停顿
CHINESE_SENTENCE_SEPARATORS = [
    "\n\n",      # 段落边界
    "\n",        # 行边界
    "。",        # 句号
    "！",        # 感叹号
    "？",        # 问号
    "；",        # 分号
    "，",        # 逗号（低优先级）
    "、",        # 顿号
]

# 从 LlamaIndex SentenceSplitter 复制默认 chunk_size，保持一致性
_DEFAULT_CHUNK_SIZE = 512
_DEFAULT_OVERLAP = 50


# ── 主函数 ─────────────────────────────────────────────────────────────────────


def chunk_by_structure(
    parsed_content: str,
    structure: Optional[DocumentStructure] = None,
    doc_id: str = "",
    max_chunk_size: int = _DEFAULT_CHUNK_SIZE,
    overlap: int = _DEFAULT_OVERLAP,
) -> list[DocChunk]:
    """按文档结构分块。

    Args:
        parsed_content: 文档全文。
        structure: 文档结构（可选），缺失时降级到 SentenceSplitter。
        doc_id: 文档 ID，用于块标识。
        max_chunk_size: 每个块的最大字符数。
        overlap: 子切分时的重叠字符数。

    Returns:
        语义完整的块列表，每个块带章节路径元数据。
    """
    if not parsed_content:
        return []

    # ── 降级路径 1：无结构 → 降级到分句器 ──────────────────────────────────────
    if not structure or not structure.chapters:
        _logger.debug(
            "No document structure available for doc '%s', "
            "falling back to sentence-based chunking",
            doc_id,
        )
        return _chunk_by_sentences(
            parsed_content, doc_id, max_chunk_size, overlap
        )

    # ── 主路径：按章/节/条款分块 ──────────────────────────────────────────────
    chunks: list[DocChunk] = []
    _ensure_chapter_texts(parsed_content, structure)

    for chap_idx, chapter in enumerate(structure.chapters):
        chapter_path = chapter.title or f"第{chap_idx + 1}章"

        # 确定章节在 parsed_content 中的字符区间
        chap_start = _find_chapter_start(parsed_content, chapter, chap_idx, structure)
        chap_end = _find_chapter_end(parsed_content, chapter, chap_idx, structure)

        # 如果章节有条款，按条款细粒度切分
        if chapter.clauses:
            _chunk_by_clauses(
                parsed_content,
                chapter,
                chapter_path,
                doc_id,
                max_chunk_size,
                overlap,
                chap_start,
                chap_end,
                chunks,
            )
        else:
            # 无条款的章节整体为一块（或子切分）
            text = chapter.text or ""
            if text:
                _sub_split_chunk(
                    text,
                    chapter_path,
                    doc_id,
                    max_chunk_size,
                    overlap,
                    chapter,
                    None,
                    chap_start,
                    chap_end,
                    chunks,
                )

    return chunks


# ── 章节区间定位 ───────────────────────────────────────────────────────────────


def _ensure_chapter_texts(parsed_content: str, structure: DocumentStructure):
    """确保每个 Chapter.text 有内容。

    如果 structure_parser 已经填充了 chapter.text，直接使用。
    如果没有，尝试从 parsed_content 按标题定位。
    """
    for chapter in structure.chapters:
        if chapter.text:
            continue
        # 尝试按标题在 parsed_content 中定位
        title = chapter.title or ""
        if not title:
            continue
        # 匹配 "## 标题" 或 "# 标题" 或 "第X章 标题"
        patterns = [
            rf"^#+\s*{re.escape(title)}\s*$",
            rf"^第[一二三四五六七八九十]+章\s*{re.escape(title)}\s*$",
        ]
        for pat in patterns:
            match = re.search(pat, parsed_content, re.MULTILINE)
            if match:
                # 找到标题行，从标题行后开始取内容（简化处理，会由后续的区间定位修正）
                start = match.end()
                chapter.text = parsed_content[start:start + 200].strip()
                break


def _find_chapter_start(
    parsed_content: str,
    chapter,
    chap_idx: int,
    structure: DocumentStructure,
) -> int:
    """找到章节在 parsed_content 中的起始偏移。"""
    # 优先用 chapter.text 定位
    if chapter.text and chapter.text in parsed_content:
        idx = parsed_content.find(chapter.text)
        if idx >= 0:
            return idx

    # 按标题行定位
    title = chapter.title or ""
    if title:
        patterns = [
            rf"^#+\s*{re.escape(title)}\s*$",
            rf"^第[一二三四五六七八九十]+章\s*{re.escape(title)}\s*$",
        ]
        for pat in patterns:
            match = re.search(pat, parsed_content, re.MULTILINE)
            if match:
                return match.start()

    # 兜底：按章节顺序估算
    prev_end = 0
    for i in range(chap_idx):
        prev = structure.chapters[i]
        if prev.text and prev.text in parsed_content:
            idx = parsed_content.find(prev.text)
            if idx >= 0:
                prev_end = idx + len(prev.text)
    return prev_end


def _find_chapter_end(
    parsed_content: str,
    chapter,
    chap_idx: int,
    structure: DocumentStructure,
) -> int:
    """找到章节在 parsed_content 中的结束偏移。"""
    # 下一个章节的起始即为当前章节的结束
    if chap_idx + 1 < len(structure.chapters):
        next_start = _find_chapter_start(
            parsed_content, structure.chapters[chap_idx + 1],
            chap_idx + 1, structure,
        )
        if next_start > 0:
            return next_start

    # 最后一章：到文档末尾
    return len(parsed_content)


# ── 按条款细粒度切分 ───────────────────────────────────────────────────────────


def _chunk_by_clauses(
    parsed_content: str,
    chapter,
    chapter_path: str,
    doc_id: str,
    max_chunk_size: int,
    overlap: int,
    chap_start: int,
    chap_end: int,
    chunks: list[DocChunk],
):
    """将章节内的条款聚合成块，确保同一块内条款不跨块分割。"""
    clause_texts = []
    current_start = chap_start

    for clause in chapter.clauses:
        clause_path = f"{chapter_path} > {clause.number} {clause.text[:30]}"
        clause_text = clause.text or ""

        if not clause_text:
            continue

        clause_texts.append(clause_text)

    # 如果没有有效的条款文本，把整个章节作为一块
    if not clause_texts:
        text = parsed_content[chap_start:chap_end].strip()
        if text:
            _sub_split_chunk(
                text, chapter_path, doc_id,
                max_chunk_size, overlap,
                chapter, None,
                chap_start, chap_end, chunks,
            )
        return

    # 简单策略：每个条款单独成块（条款通常较短，不会超过 max_chunk_size）
    for clause in chapter.clauses:
        clause_text = clause.text or ""
        if not clause_text:
            continue

        clause_path = f"{chapter_path} > {clause.number}"
        clause_start = parsed_content.find(clause_text, chap_start)

        _sub_split_chunk(
            clause_text, clause_path, doc_id,
            max_chunk_size, overlap,
            chapter, clause,
            max(clause_start, chap_start) if clause_start >= 0 else chap_start,
            chap_end,
            chunks,
        )


# ── 子切分（超长文本在句末切分） ─────────────────────────────────────────────────


def _sub_split_chunk(
    text: str,
    section_path: str,
    doc_id: str,
    max_chunk_size: int,
    overlap: int,
    chapter,
    clause,
    char_start: int,
    char_end: int,
    chunks: list[DocChunk],
):
    """如果文本超过 max_chunk_size，在中���句末标点处切分。"""
    if len(text) <= max_chunk_size:
        chunks.append(DocChunk(
            chunk_id=f"ch_{doc_id}_{len(chunks)}",
            doc_id=doc_id,
            text=text.strip(),
            section_path=section_path,
            chapter_number=chapter.number,
            chapter_title=chapter.title,
            clause_number=clause.number if clause else None,
            clause_title=clause.text[:60] if clause and clause.text else None,
            char_span_start=char_start,
            char_span_end=char_end,
        ))
        return

    # 超长文本：在句末标点处切分
    pos = 0
    sub_idx = 0
    while pos < len(text):
        end = _find_sentence_boundary(text, pos, max_chunk_size)
        chunk_text = text[pos:end].strip()
        if chunk_text:
            chunks.append(DocChunk(
                chunk_id=f"ch_{doc_id}_{len(chunks)}",
                doc_id=doc_id,
                text=chunk_text,
                section_path=f"{section_path}（续{sub_idx}）",
                chapter_number=chapter.number,
                chapter_title=chapter.title,
                clause_number=clause.number if clause else None,
                clause_title=clause.text[:60] if clause and clause.text else None,
                char_span_start=char_start + pos,
                char_span_end=char_start + end,
            ))
        sub_idx += 1
        pos = end - overlap  # 重叠区域
        if pos < 0:
            pos = 0


def _find_sentence_boundary(text: str, start: int, max_size: int) -> int:
    """找到尽量接近 max_size 且不跨句子的切分点。"""
    if start + max_size >= len(text):
        return len(text)

    end = start + max_size
    segment = text[start:end]

    # 从后往前在句末标点处切分
    for sep in CHINESE_SENTENCE_SEPARATORS:
        pos = segment.rfind(sep)
        if pos > 0:
            return start + pos + len(sep)

    # 没有合适句末标点，在最后一个空格或 max_size 处切分
    last_space = segment.rfind(" ")
    if last_space > 0:
        return start + last_space + 1
    return end


# ── 降级路径：分句器分块（无结构可用时） ──────────────────────────────────────


def _chunk_by_sentences(
    parsed_content: str,
    doc_id: str,
    max_chunk_size: int,
    overlap: int,
) -> list[DocChunk]:
    """无结构可用时的降级：纯分句器分块。"""
    chunks: list[DocChunk] = []
    pos = 0
    chunk_index = 0

    while pos < len(parsed_content):
        end = _find_sentence_boundary(parsed_content, pos, max_chunk_size)
        chunk_text = parsed_content[pos:end].strip()
        if chunk_text:
            chunks.append(DocChunk(
                chunk_id=f"ch_{doc_id}_{chunk_index}",
                doc_id=doc_id,
                text=chunk_text,
                section_path="全文",
                char_span_start=pos,
                char_span_end=end,
            ))
            chunk_index += 1
        pos = end - overlap
        if pos < 0:
            pos = 0

    return chunks


# ── 工具函数 ────────────────────────────────────────────────────────────────────


def format_chunks_for_llm(chunks: list[DocChunk], max_chars: int = 3000) -> str:
    """将分块结果格式化为 LLM 可读的文本。"""
    parts = []
    for c in chunks:
        parts.append(f"[{c.section_path}] {c.text.strip()}")
    result = "\n\n".join(parts)
    if len(result) > max_chars:
        result = result[:max_chars] + "\n\n...（截断）"
    return result
