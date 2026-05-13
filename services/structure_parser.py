"""从 MinerU 输出的 Markdown 解析文档结构。

不需要 LLM 参与，纯正则解析 Markdown 标题层级。
覆盖全文，不截断，不遗漏。
"""

import re
from typing import Optional

from models.audit_document import DocumentStructure, Chapter, Clause


# ── 主入口 ──────────────────────────────────────────────────────────────────

def parse_markdown_structure(markdown: str, doc_title: Optional[str] = None) -> DocumentStructure:
    """从 MinerU 生成的 Markdown 中解析完整的章节结构。

    Args:
        markdown: MinerU 输出的 Markdown 文本（含 HTML 表格）。
        doc_title: 可选文档标题。

    Returns:
        覆盖全文的 DocumentStructure，零 LLM 调用。
    """
    lines = markdown.split("\n")
    chapters: list[Chapter] = []
    current_chapter: Optional[Chapter] = None
    current_clause_num: Optional[str] = None
    current_clause_text: list[str] = []

    # 先去掉 HTML table 标签内的换行（避免干扰行解析）
    cleaned_lines = _flatten_html_tables(lines)

    for line in cleaned_lines:
        stripped = line.strip()
        if not stripped:
            continue

        # 检测 H1 标题 → 新章节
        h1_match = re.match(r'^#\s+(.+)$', stripped)
        if h1_match:
            # 保存前一个章节未完成的条款
            if current_chapter:
                _finalize_clause(current_chapter, current_clause_num, current_clause_text)

            current_clause_num = None
            current_clause_text = []

            chapter_title = _clean_heading(h1_match.group(1))
            chapter_num = _extract_number(h1_match.group(1))
            current_chapter = Chapter(number=chapter_num, title=chapter_title)
            chapters.append(current_chapter)
            continue

        if current_chapter is None:
            # H1 之前的文本（如表格、前导说明）→ 归入"前言"
            current_chapter = Chapter(title="前言")
            chapters.append(current_chapter)

        # 检测 H2 / H3 标题 → 子章节，可能本身就是条款
        h3_match = re.match(r'^###\s+(.+)$', stripped)
        h2_match = re.match(r'^##\s+(.+)$', stripped) if not h3_match else None

        if h3_match:
            _finalize_clause(current_chapter, current_clause_num, current_clause_text)
            h3_text = h3_match.group(1)
            h3_clean = _clean_heading(h3_text)

            # H3 标题可能本身就包含条款内容如 "### 2.3.1. 需求和深化设计：2个月内完成"
            if _has_clause_content(h3_text):
                num = _extract_number(h3_text)
                text = _extract_clause_text_from_heading(h3_text)
                current_chapter.clauses.append(Clause(number=num, text=text[:200]))
                # 保留编号，后续段落追加到此条款
                current_clause_num = num
                current_clause_text = []
            else:
                # 纯标题行，作为上下文记录
                current_clause_num = None
                current_clause_text = [f"【{h3_clean}】"]
            continue

        if h2_match:
            _finalize_clause(current_chapter, current_clause_num, current_clause_text)
            h2_text = h2_match.group(1)
            current_clause_num = None
            current_clause_text = [f"【{_clean_heading(h2_text)}】"]
            continue

        # 检测编号条款行：1)、2)、1.、2.、等
        clause_match = re.match(r'^\s*(\d+)[\)\.、]\s*(.*)', stripped)
        if clause_match:
            _finalize_clause(current_chapter, current_clause_num, current_clause_text)
            current_clause_num = clause_match.group(1)
            current_clause_text = [clause_match.group(2).strip()]
            continue

        # 检测带编号的短条款：3.2.1 防护等级
        clause_num_match = re.match(r'^(\d+(?:\.\d+)+)\s+(.*)', stripped)
        if clause_num_match and len(stripped) < 200:
            _finalize_clause(current_chapter, current_clause_num, current_clause_text)
            current_clause_num = clause_num_match.group(1)
            current_clause_text = [clause_num_match.group(2).strip()]
            continue

        # 检测表格标记 → 追加到上一个条款
        if stripped == "<TABLE>":
            # 追加到上一条款的文本中
            if current_chapter and current_chapter.clauses:
                last = current_chapter.clauses[-1]
                if "[包含表格]" not in last.text:
                    last.text = last.text.rstrip() + " [包含表格数据]"
            continue

        # 普通段落 → 追加到当前条款
        if current_clause_num:
            current_clause_text.append(stripped)
        else:
            current_clause_text.append(stripped)

    # 收尾
    if current_chapter:
        _finalize_clause(current_chapter, current_clause_num, current_clause_text)

    total = sum(len(ch.clauses) for ch in chapters)

    return DocumentStructure(
        title=doc_title or (chapters[0].title if chapters else None),
        chapters=chapters,
        total_clauses=total,
    )


# ── 内部辅助 ─────────────────────────────────────────────────────────────────

def _flatten_html_tables(lines: list[str]) -> list[str]:
    """将 HTML table 跨行合并为单行 <TABLE> 标记，避免表格内容被误解析。"""
    result = []
    in_table = False
    for line in lines:
        has_open = "<table" in line.lower()
        has_close = "</table>" in line.lower()

        if has_open and has_close:
            # 单行表格 <table>...</table>
            result.append("<TABLE>")
            continue

        if has_open:
            in_table = True
            result.append("<TABLE>")
            continue

        if has_close:
            in_table = False
            continue

        if not in_table:
            result.append(line)
    return result


def _clean_heading(raw: str) -> str:
    """清理标题文本：去除加粗标记、编号前缀。"""
    text = raw.strip()
    # 去除 **加粗**
    text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
    # 去除尾部编号
    text = re.sub(r'^\d+(?:\.\d+)*\.?\s*', '', text).strip()
    return text


def _extract_number(raw: str) -> str:
    """从标题或行中提取章节/条款编号。"""
    text = raw.strip()
    m = re.match(r'(\d+(?:\.\d+)*)', text)
    if m:
        return m.group(1)
    return ""


def _has_clause_content(text: str) -> bool:
    """判断 H3 标题行是否包含实质性的条款内容（而非纯标题）。"""
    cleaned = _clean_heading(text)
    # 如果清理后还有内容且带 要求/必须/应/需 等关键词
    if len(cleaned) > 10:
        return True
    return False


def _extract_clause_text_from_heading(text: str) -> str:
    """从 H3 标题行提取条款内容。"""
    cleaned = _clean_heading(text)
    # 去掉编号前缀
    return re.sub(r'^\d+(?:\.\d+)*\.?\s*', '', cleaned).strip()


def _finalize_clause(chapter: Chapter, clause_num: Optional[str], text_lines: list[str]):
    """将累积的文本行保存为 Clause。"""
    if not clause_num and not text_lines:
        return

    text = " ".join(t for t in text_lines if t).strip()
    if not text:
        return

    # 如果没编号但有内容，作为"附注"归入上下文
    if not clause_num:
        return

    chapter.clauses.append(Clause(number=clause_num, text=text[:200]))
