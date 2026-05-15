"""文档导航工具。

从 parsed_content 中提取标题树（仅 H1/H2，零成本），
供 Agent 按需阅读相关段落，不再依赖精确的条款级结构解析。
"""

import re
from typing import Optional


class DocumentNav:
    """文档导航器。接收 parsed_content，提供按标题取内容的能力。"""

    def __init__(self, doc_id: str, parsed_content: str):
        self.doc_id = doc_id
        self.content = parsed_content
        self.lines = parsed_content.split("\n") if parsed_content else []
        self._sections: list[dict] = []  # 缓存
        self._build_index()

    # ── 构建标题索引 ──────────────────────────────────────────────────────

    def _build_index(self):
        """遍历全文，提取 H1/H2 标题及其行号范围。"""
        sections = []
        current = None

        for i, line in enumerate(self.lines):
            stripped = line.strip()
            m = re.match(r'^(#{1,2})\s+(.+)$', stripped)
            if not m:
                continue

            level = len(m.group(1))
            title = m.group(2).strip()
            # 去掉加粗标记用于匹配
            title_clean = re.sub(r'\*\*(.*?)\*\*', r'\1', title).strip()

            if current:
                current["end_line"] = i
                sections.append(current)

            current = {
                "level": level,
                "title": title_clean,
                "title_raw": title,
                "start_line": i,
                "end_line": len(self.lines),
            }

        if current:
            sections.append(current)

        self._sections = sections

    # ── Agent 工具函数 ─────────────────────────────────────────────────────

    def get_structure(self) -> str:
        """返回文档的标题树（不含正文），供 Agent 了解文档结构。"""
        if not self._sections:
            return "（文档无标题结构）"

        lines = []
        for sec in self._sections:
            prefix = "  " if sec["level"] == 2 else ""
            lines.append(f'{prefix}{sec["title"]}')

        return "\n".join(lines)

    def get_h1_structure(self) -> str:
        """仅返回 H1 章节标题（干净的章节名，不含超长行内容）。"""
        h1s = [sec["title"] for sec in self._sections if sec["level"] == 1]
        return "\n".join(h1s) if h1s else self.get_structure()

    def get_section_content(self, title_or_number: str, max_chars: int = 5000) -> str:
        """按标题名称或编号查找章节并返回正文内容。

        Args:
            title_or_number: 章节标题（如"投标报价"）或编号（如"1.28"）。
            max_chars: 返回的最大字符数。

        Returns:
            章节原文，找不到时返回空字符串。
        """
        # 先尝试编号匹配
        target = title_or_number.strip()

        # 精确匹配
        for sec in self._sections:
            title = sec["title"]
            if title == target:
                return self._extract_text(sec, max_chars)
            # 编号前缀匹配（"1.28" 匹配 "1.28. 投标报价"）
            if target and re.match(re.escape(target) + r'[\.\s]', title):
                return self._extract_text(sec, max_chars)

        # 模糊匹配（包含关键词）
        for sec in self._sections:
            if target.lower() in sec["title"].lower():
                return self._extract_text(sec, max_chars)

        return ""

    def _extract_text(self, section: dict, max_chars: int) -> str:
        """从行范围提取正文。"""
        start = section["start_line"]
        end = min(section["end_line"], len(self.lines))

        text = "\n".join(self.lines[start:end]).strip()
        if len(text) > max_chars:
            text = text[:max_chars] + "\n... [截断]"
        return text

    def find_relevant_sections(self, keywords: list[str]) -> str:
        """按关键词查找相关章节的标题（供主题审核预先定位用）。"""
        hits = []
        for sec in self._sections:
            for kw in keywords:
                if kw.lower() in sec["title"].lower():
                    hits.append(f'{sec["title"]}')
                    break
        return "\n".join(hits) if hits else "（未找到匹配章节）"

    def search_sections_by_title(self, query: str) -> str:
        """用语义查询找相关章节（LLM 遍历标题）。"""
        # 简单实现：从 get_structure 返回的标题树里做关键词过滤
        structure = self.get_structure()
        lines = structure.split("\n")
        matched = [l for l in lines if any(w in l.lower() for w in query.lower().split())]
        return "\n".join(matched) if matched else structure
