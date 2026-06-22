"""topic_audit 测试 — 纯逻辑（关键词定位 / JSON 降级 / 结果映射）+ mock 化 audit_topic。

纯逻辑部分不触发任何模型加载；audit_topic 的 mock 部分通过 monkeypatch
替换 ``get_llm`` 和 ``_search_kb_by_keywords``（后者避免触发 vector_search
→ embedding 模型），覆盖结构化输出主路径与两条降级路径。
"""

from unittest.mock import MagicMock

from services.topic_audit import (
    locate_paragraphs,
    _parse_json_fallback,
    _issues_from_schema,
    audit_topic,
)
from models.llm_schemas import TopicIssueList, TopicIssue


# ── locate_paragraphs（纯逻辑）──────────────────────────────────────────────────


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


# ── _parse_json_fallback（纯逻辑）───────────────────────────────────────────────


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


# ── _issues_from_schema（纯逻辑）────────────────────────────────────────────────


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


# ── audit_topic（mock 化）───────────────────────────────────────────────────────
#
# 关键：patch services.topic_audit._search_kb_by_keywords 避免触发
# vector_search → embedding 模型加载；patch get_llm 返回 MagicMock。


def _make_llm_with_structured(raw):
    """构造一个 get_llm() 返回值：as_structured_llm 路径返回 raw。"""
    llm = MagicMock()
    structured = MagicMock()
    structured.chat.return_value.raw = raw
    llm.as_structured_llm.return_value = structured
    return llm


def test_audit_topic_structured_path(monkeypatch):
    """as_structured_llm 主路径：返回 TopicIssueList → 映射为 AuditIssue。"""
    monkeypatch.setattr(
        "services.topic_audit._search_kb_by_keywords", lambda *a, **k: "KB参考"
    )
    fake_result = TopicIssueList(
        issues=[TopicIssue(type="compliance", description="测试问题", severity="medium")]
    )
    monkeypatch.setattr(
        "services.topic_audit.get_llm", lambda: _make_llm_with_structured(fake_result)
    )

    topic = {"id": "t1", "name": "测试主题", "prompt": "...", "keywords": ["测试"]}
    issues = audit_topic(topic, None, ["kb1"], topic_index=1, parsed_content="测试内容")

    assert len(issues) == 1
    assert issues[0].description == "测试问题"
    assert issues[0].id == 1001  # topic_index=1, i=0 → 1*1000+0+1


def test_audit_topic_fallback_to_chat(monkeypatch):
    """as_structured_llm 抛错 → 降级到 .chat() + _parse_json_fallback。"""
    monkeypatch.setattr(
        "services.topic_audit._search_kb_by_keywords", lambda *a, **k: ""
    )
    llm = MagicMock()
    llm.as_structured_llm.side_effect = RuntimeError("structured unavailable")
    chat_resp = MagicMock()
    chat_resp.message.content = (
        '{"issues": [{"type": "completeness", "description": "降级问题", "severity": "low"}]}'
    )
    llm.chat.return_value = chat_resp
    monkeypatch.setattr("services.topic_audit.get_llm", lambda: llm)

    topic = {"id": "t1", "name": "测试", "keywords": ["测试"]}
    issues = audit_topic(topic, None, ["kb1"], topic_index=0, parsed_content="测试内容")

    assert len(issues) == 1
    assert issues[0].description == "降级问题"
    assert issues[0].severity == "low"


def test_audit_topic_both_fail_returns_empty(monkeypatch):
    """structured 与 chat 两条路径都抛错 → 返回空列表（不向外抛）。"""
    monkeypatch.setattr(
        "services.topic_audit._search_kb_by_keywords", lambda *a, **k: ""
    )
    llm = MagicMock()
    llm.as_structured_llm.side_effect = RuntimeError("fail")
    llm.chat.side_effect = RuntimeError("fail")
    monkeypatch.setattr("services.topic_audit.get_llm", lambda: llm)

    topic = {"id": "t1", "name": "测试", "keywords": ["测试"]}
    assert audit_topic(topic, None, ["kb1"], topic_index=0, parsed_content="测试内容") == []


def test_audit_topic_empty_issues_returns_empty(monkeypatch):
    """LLM 返回空 issues 列表 → 返回空列表。"""
    monkeypatch.setattr(
        "services.topic_audit._search_kb_by_keywords", lambda *a, **k: ""
    )
    monkeypatch.setattr(
        "services.topic_audit.get_llm",
        lambda: _make_llm_with_structured(TopicIssueList(issues=[])),
    )

    topic = {"id": "t1", "name": "测试", "keywords": ["测试"]}
    assert audit_topic(topic, None, ["kb1"], topic_index=0, parsed_content="测试内容") == []
