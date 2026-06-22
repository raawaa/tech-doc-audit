"""agent_audit 测试 — determine_audit_topics 的结构化路径与降级链。

通过 monkeypatch 替换 ``services.agent_audit.get_llm`` 返回 MagicMock，
覆盖 LLM 动态选主题的主路径、chat 降级、全失败返回空、以及 8000 字符截断。
不触发真实模型加载。
"""

from unittest.mock import MagicMock

import services.agent_audit as agent_audit
from models.llm_schemas import AuditTopicList, AuditTopicItem


def _make_llm_with_structured(raw):
    llm = MagicMock()
    structured = MagicMock()
    structured.chat.return_value.raw = raw
    llm.as_structured_llm.return_value = structured
    return llm


def test_determine_audit_topics_structured_path(monkeypatch):
    """as_structured_llm 主路径：返回 AuditTopicList → 映射为兼容 AUDIT_TOPICS 的 dict 列表。"""
    fake = AuditTopicList(
        topics=[
            AuditTopicItem(
                id="tax", name="税率合规", prompt="审税率", keywords=["增值税"], reason="涉及"
            ),
            AuditTopicItem(
                id="brand", name="品牌限制", prompt="审品牌", keywords=["品牌"], reason="涉及"
            ),
        ]
    )
    monkeypatch.setattr("services.agent_audit.get_llm", lambda: _make_llm_with_structured(fake))

    result = agent_audit.determine_audit_topics("招标文件内容")

    assert len(result) == 2
    assert result[0]["id"] == "tax"
    assert result[0]["keywords"] == ["增值税"]
    assert "reason" in result[0]
    assert result[1]["name"] == "品牌限制"


def test_determine_audit_topics_fallback_on_structured_failure(monkeypatch):
    """as_structured_llm 抛错 → 降级到 .chat() + _parse_json_fallback。"""
    llm = MagicMock()
    llm.as_structured_llm.side_effect = RuntimeError("structured unavailable")
    chat_resp = MagicMock()
    chat_resp.message.content = (
        '{"topics": [{"id": "pay", "name": "支付条款", "prompt": "审支付", '
        '"keywords": ["保证金"], "reason": "涉及"}]}'
    )
    llm.chat.return_value = chat_resp
    monkeypatch.setattr("services.agent_audit.get_llm", lambda: llm)

    result = agent_audit.determine_audit_topics("内容")

    assert len(result) == 1
    assert result[0]["id"] == "pay"
    assert result[0]["prompt"] == "审支付"


def test_determine_audit_topics_returns_empty_on_total_failure(monkeypatch):
    """两条路径都抛错 → 返回空列表（由调用方降级到 8 个固定主题）。"""
    llm = MagicMock()
    llm.as_structured_llm.side_effect = RuntimeError("fail")
    llm.chat.side_effect = RuntimeError("fail")
    monkeypatch.setattr("services.agent_audit.get_llm", lambda: llm)

    assert agent_audit.determine_audit_topics("内容") == []


def test_determine_audit_topics_truncates_to_8000_chars(monkeypatch):
    """长文档 → 发送给 LLM 的预览截断为 max_content_chars（默认 8000）。"""
    long_content = "文档" * 5000  # 10000 字符
    captured = {}

    # ChatPromptTemplate 是 Pydantic 模型，不能 setattr 其方法；
    # 改为整体替换模块级 _prompt 引用，用假对象捕获传入的 preview。
    class _FakePrompt:
        def format_messages(self, document_preview=""):
            captured["preview"] = document_preview
            return []

    monkeypatch.setattr(agent_audit, "_prompt", _FakePrompt())
    monkeypatch.setattr(
        "services.agent_audit.get_llm",
        lambda: _make_llm_with_structured(AuditTopicList(topics=[])),
    )

    agent_audit.determine_audit_topics(long_content)

    assert len(captured["preview"]) == 8000


def test_determine_audit_topics_empty_topics_returns_empty(monkeypatch):
    """LLM 返回空 topics → 返回空列表。"""
    monkeypatch.setattr(
        "services.agent_audit.get_llm",
        lambda: _make_llm_with_structured(AuditTopicList(topics=[])),
    )
    assert agent_audit.determine_audit_topics("内容") == []
