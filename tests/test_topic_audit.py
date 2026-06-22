"""topic_audit 纯逻辑测试 — 关键词定位、JSON 降级解析、结构化结果映射。

这些测试不触发 LLM / embedding 模型加载，只覆盖 ``services/topic_audit.py``
里无副作用的纯函数，正好对应架构复盘 §6.5 标记为「未校准 / 未验证」的
关键词定位逻辑（KEYWORD_CONTEXT_CHARS=1500）。
"""

from services.topic_audit import (
    locate_paragraphs,
    _parse_json_fallback,
    _issues_from_schema,
)
from models.llm_schemas import TopicIssueList, TopicIssue


# ── locate_paragraphs ──────────────────────────────────────────────────────────


def test_locate_paragraphs_finds_keyword():
    """含关键词的内容 → 返回非空，且包含关键词及其上下文。"""
    content = "文档前言。" + "增值税税率" + "后续条款说明。"
    result = locate_paragraphs(content, ["增值税"])
    assert result != ""
    assert "增值税" in result


def test_locate_paragraphs_dedup():
    """两个关键词命中同一区域（同一 1000 字符桶）→ 去重为单段。"""
    content = "增值税税率相关条款说明。"
    result = locate_paragraphs(content, ["增值税", "税率"])
    # 短文本：两次命中的 (start//1000, end//1000) 桶相同 → 只保留 1 段
    assert result != ""
    assert result.count("---") == 0  # 单段无分隔符


def test_locate_paragraphs_caps_at_5():
    """10 个分散关键词（各自落入不同桶）→ 截断为上限 5 段。"""
    parts = []
    for i in range(10):
        # 每段填充足够长，使各关键词的 ±1500 窗口落在不同 1000 桶
        parts.append("填充" * 1500 + f"关键词{i}")
    content = "".join(parts)
    keywords = [f"关键词{i}" for i in range(10)]
    result = locate_paragraphs(content, keywords)
    segments = result.split("\n\n---\n\n")
    assert len(segments) == 5  # 硬上限


def test_locate_paragraphs_empty_inputs():
    """空内容或空关键词列表 → 返回空串。"""
    assert locate_paragraphs("", ["增值税"]) == ""
    assert locate_paragraphs("内容", []) == ""
    assert locate_paragraphs("", []) == ""


def test_locate_paragraphs_no_match():
    """内容不含任何关键词 → 返回空串。"""
    assert locate_paragraphs("完全无关的招标内容", ["增值税", "保证金"]) == ""


# ── _parse_json_fallback ───────────────────────────────────────────────────────


def test_parse_json_fallback_valid():
    """文本中嵌入合法 JSON → 解析为 TopicIssueList。"""
    content = '前缀说明 {"issues": [{"type": "compliance", "description": "问题", "severity": "high"}]} 后缀'
    result = _parse_json_fallback(content)
    assert result is not None
    assert len(result.issues) == 1
    assert result.issues[0].type == "compliance"
    assert result.issues[0].severity == "high"


def test_parse_json_fallback_no_braces():
    """纯文本无花括号 → 返回 None。"""
    assert _parse_json_fallback("纯文本无 JSON 结构") is None


def test_parse_json_fallback_malformed():
    """花括号内非合法 JSON → 返回 None（不抛异常）。"""
    assert _parse_json_fallback("{这不是合法 json}") is None


# ── _issues_from_schema ────────────────────────────────────────────────────────


def test_issues_from_schema_maps_fields():
    """结构化输出 → AuditIssue：id 公式正确，非法 type/severity 降级默认值。"""
    result = TopicIssueList(
        issues=[
            TopicIssue(
                type="compliance",
                description="问题一",
                severity="high",
                clause_number="3.2",
            ),
            TopicIssue(
                type="invalid_type",   # 非法 → completeness
                description="问题二",
                severity="critical",   # 非法 → medium
            ),
        ]
    )
    issues = _issues_from_schema(result, topic_index=2)

    assert len(issues) == 2
    # id = topic_index * 1000 + i + 1
    assert issues[0].id == 2001
    assert issues[0].type == "compliance"
    assert issues[0].severity == "high"
    assert issues[1].id == 2002
    # 非法值降级
    assert issues[1].type == "completeness"
    assert issues[1].severity == "medium"


def test_issues_from_schema_empty():
    """空 issues 列表 → 返回空列表。"""
    result = TopicIssueList(issues=[])
    assert _issues_from_schema(result, topic_index=0) == []
