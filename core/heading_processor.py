"""MinerU content_list_v2 标题层级后处理。

MinerU 将所有标题标记为 text_level=1 / level=1，没有层级信息。
本模块通过正则识别中文公文的标题模式来恢复层级。

使用方式：
    processor = HeadingProcessor()
    level = processor.detect_level("第一章 总则")     # → 1
    level = processor.detect_level("第一条 目的与依据")  # → 2
"""

from __future__ import annotations

import re
from typing import NamedTuple


class HeadingRule(NamedTuple):
    """标题匹配规则。"""

    pattern: re.Pattern[str]
    level: int              # markdown heading level (1-based)
    priority: int           # lower = 优先匹配


# 规则按优先级降序排列（priority 越低越优先匹配）
_RULES: list[HeadingRule] = [
    # 章/篇 → h1
    HeadingRule(re.compile(r"第[一二三四五六七八九十百零]+[章篇]"), 1, 10),
    # 部/节 → h2
    HeadingRule(re.compile(r"第[一二三四五六七八九十百零]+[部节]"), 2, 20),
    # 分 → h2（"第X分" 如 "第十分"）
    HeadingRule(re.compile(r"第[一二三四五六七八九十百零]+[分]"), 2, 25),
    # 条 → h3
    HeadingRule(re.compile(r"^第[一二三四五六七八九十百零]+[条]"), 3, 30),
    # 1.2.3 多级编号 → h3
    HeadingRule(re.compile(r"^\d+\.\d+\."), 3, 35),
    # 一、 中文数字顿号 → h3
    HeadingRule(re.compile(r"^[一二三四五六七八九十]+[、．\.]"), 3, 40),
    # （一）括号中文数字 → h4
    HeadingRule(re.compile(r"^[\(\（][一二三四五六七八九十百零]+[\)\）]"), 4, 50),
    # 1.2 二级编号（后接汉字或空格）→ h4
    HeadingRule(re.compile(r"^\d+\.\d+[^\d]"), 4, 55),
    # 1. 简单数字编号 → h5
    HeadingRule(re.compile(r"^\d+[、．\.\s]"), 5, 58),
    # (1) 括号数字 → h6
    HeadingRule(re.compile(r"^[\(（]\d+[\)\）]"), 6, 70),
    # ①②③ 特殊符号 → h6
    HeadingRule(re.compile(r"^[①②③④⑤⑥⑦⑧⑨⑩]"), 6, 80),
]


def _is_likely_title(text: str) -> bool:
    """判断一段文本是否可能是标题（启发式）。"""
    if len(text) > 80:
        return False
    if len(text) < 2:
        return False
    # 以标点结尾的通常不是标题
    if text[-1] in "。；；！？，,.;!?":
        return False
    return True


class HeadingProcessor:
    """标题层级推断器。

    用法:
        processor = HeadingProcessor()
        for item in content_list:
            level = processor.detect_level(item["text"])
            if level:
                node = {"type": "heading", "level": level, "text": ...}
            else:
                node = {"type": "paragraph", "text": ...}
    """

    def detect_level(self, text: str) -> int | None:
        """推断标题层级，返回 1-6 或 None（非标题）。"""
        text = text.strip()
        if not _is_likely_title(text):
            return None
        for rule in _RULES:
            if rule.pattern.search(text):
                return rule.level
        return None

    def rebuild_markdown(
        self, content_list: list[dict]
    ) -> str:
        """从 MinerU content_list / content_list_v2 重建带正确标题层级的 Markdown。

        自动检测 V1（type: text, text_level）和 V2（type: title, content）格式。
        """
        if not content_list:
            return ""
        # 检测格式
        sample = content_list[0]
        is_v2 = "content" in sample and isinstance(sample.get("content"), dict)
        if is_v2:
            return self._rebuild_v2(content_list)
        return self._rebuild_v1(content_list)

    def _rebuild_v1(self, items: list[dict]) -> str:
        """V1 格式：{type: text, text, text_level, ...}。"""
        lines: list[str] = []
        for item in items:
            item_type = item.get("type", "")
            if item_type in ("page_header", "page_number", "image"):
                continue
            text = (item.get("text") or "").strip()
            if not text:
                continue
            if item_type == "table":
                lines.append(f"\n[表格]\n{text}\n")
                continue
            # text_level=1 表示标题
            if item.get("text_level") == 1:
                level = self.detect_level(text)
                if level:
                    lines.append(f"\n{'#' * level} {text}\n")
                else:
                    lines.append(text)
            else:
                if text == (lines[-1:] or [None])[0]:
                    continue
                lines.append(text)
        return _merge_lines(lines)

    def _rebuild_v2(self, items: list[dict]) -> str:
        """V2 格式：{type: title, content: {title_content, level, ...}}。"""
        lines: list[str] = []

        for item in items:
            item_type = item.get("type", "")
            content = item.get("content", {})

            if item_type == "page_header" or item_type == "page_number":
                continue

            if item_type == "title":
                text = _extract_text_content(content)
                if not text:
                    continue
                level = self.detect_level(text)
                if level:
                    lines.append(f"\n{'#' * level} {text}\n")
                else:
                    lines.append(text)

            elif item_type == "paragraph":
                text = _extract_paragraph_content(content)
                if text:
                    if text == lines[-1:] or not text:
                        continue
                    lines.append(text)

            elif item_type == "table":
                table_text = _extract_table_content(content)
                if table_text:
                    lines.append(f"\n[表格]\n{table_text}\n")

            elif item_type == "index":
                text = _extract_list_content(content)
                if text:
                    lines.append(text)

        return _merge_lines(lines)

    def process_mineru_output(
        self, mineru_output_dir: str
    ) -> str | None:
        """直接处理 MinerU 输出目录，返回修复后的 Markdown。"""
        from pathlib import Path

        out_dir = Path(mineru_output_dir)

        # 找 content_list_v2.json（优先级高）或 content_list.json
        json_path = out_dir / "auto" / (
            list(out_dir.rglob("*content_list_v2.json")) or
            list(out_dir.rglob("*content_list.json"))
        )[0].name

        if not json_path.exists():
            return None

        import json
        data = json.loads(json_path.read_text(encoding="utf-8"))

        # content_list_v2.json 是 list[list[dict]]（按页分组）
        if isinstance(data, list) and data and isinstance(data[0], list):
            items = [it for page in data for it in page]
        elif isinstance(data, list):
            items = data
        else:
            return None

        return self.rebuild_markdown(items)

    def rebuild_from_md(self, md_text: str) -> str:
        """降级方案：从 MinerU 的扁平淡 MD 文本修复标题层级。

        MinerU 将所有标题输出为 `# `，本方法逐行重新检测层级并替换。
        """
        lines = md_text.split("\n")
        result: list[str] = []
        for line in lines:
            stripped = line.strip()
            # 匹配以 # 开头的标题行（MinerU 输出全是一级）
            if stripped.startswith("# ") and len(stripped) > 2:
                text = stripped[2:].strip()
                level = self.detect_level(text)
                if level:
                    result.append(f"\n{'#' * level} {text}\n")
                else:
                    result.append(text)
            else:
                result.append(line)
        return _merge_lines(result)


# ── 内容提取辅助 ─────────────────────────────────────────────────────────────────


def _extract_text_content(content: dict) -> str:
    """从 title content 结构中提取文本。"""
    parts = content.get("title_content", [])
    return "".join(p.get("content", "") for p in parts if p.get("type") == "text")


def _extract_paragraph_content(content: dict) -> str:
    """从 paragraph content 结构中提取文本。"""
    parts = content.get("paragraph_content", [])
    return "".join(p.get("content", "") for p in parts if p.get("type") == "text")


def _extract_table_content(content: dict) -> str:
    """从 table content 中提取文本（简单拼接）。"""
    cells = content.get("cells", []) or content.get("table_content", [])
    if not cells:
        return ""
    rows = []
    for row in cells:
        if isinstance(row, list):
            rows.append(" | ".join(str(c.get("content", "") if isinstance(c, dict) else c) for c in row))
    return "\n".join(rows)


def _extract_list_content(content: dict) -> str:
    """从 list/index content 中提取文本。"""
    items = content.get("list_items", [])
    lines = []
    for it in items:
        parts = it.get("item_content", [])
        text = "".join(p.get("content", "") for p in parts)
        if text:
            lines.append(text)
    return "\n".join(lines)


def _merge_lines(lines: list[str]) -> str:
    """合并行，去除连续空行。"""
    result = []
    prev_empty = False
    for line in lines:
        is_empty = not line.strip()
        if is_empty and prev_empty:
            continue
        result.append(line)
        prev_empty = is_empty
    return "\n".join(result).strip()
