"""qa_service 测试 — 会话管理、chat / chat_stream、追问提取。

关键：整体 patch ``_build_chat_engine``，避免 ContextChatEngine 构造
CrossKBRetriever 时触发 bge-m3 embedding 模型加载（2GB，CI 会 OOM）。
fixture 设 ``_embed_initialized = True`` 跳过 chat() 开头的模型初始化检查。
"""

import time
from unittest.mock import MagicMock

import pytest

import services.qa_service as qa_service


@pytest.fixture(autouse=True)
def reset_qa_state():
    """每个测试前跳过模型初始化、清空 session 缓存；测试后再复位。"""
    qa_service._embed_initialized = True
    qa_service._sessions.clear()
    yield
    qa_service._sessions.clear()
    qa_service._embed_initialized = False


def _fake_engine(answer="回答", source_nodes=None):
    """构造一个 mock ContextChatEngine，chat() 返回固定回答。"""
    engine = MagicMock()
    resp = MagicMock()
    resp.response = answer
    resp.source_nodes = source_nodes or []
    engine.chat.return_value = resp
    return engine


class _FakeStreamResponse:
    """模拟 ContextChatEngine.stream_chat 的返回：可迭代 + 带 source_nodes。"""

    def __init__(self, deltas, source_nodes=None):
        self._deltas = deltas
        self.source_nodes = source_nodes or []

    def __iter__(self):
        for d in self._deltas:
            chunk = MagicMock()
            chunk.delta = d
            yield chunk


# ── 会话管理 ───────────────────────────────────────────────────────────────────


def test_chat_creates_new_session(monkeypatch):
    """session_id=None → 新建 12 位 hex session_id，_sessions 多一项。"""
    monkeypatch.setattr(qa_service, "_build_chat_engine", lambda kb_ids, top_k: _fake_engine())

    result = qa_service.chat(None, "问题", ["kb1"])

    assert result["session_id"]
    assert len(result["session_id"]) == 12
    assert len(qa_service._sessions) == 1
    assert result["answer"] == "回答"


def test_chat_reuses_existing_session(monkeypatch):
    """同 session_id + 同 KB 列表 → 复用引擎，_build_chat_engine 只调一次。"""
    build_count = {"n": 0}

    def fake_build(kb_ids, top_k):
        build_count["n"] += 1
        return _fake_engine()

    monkeypatch.setattr(qa_service, "_build_chat_engine", fake_build)

    r1 = qa_service.chat(None, "问题1", ["kb1"])
    sid = r1["session_id"]
    qa_service.chat(sid, "问题2", ["kb1"])

    assert build_count["n"] == 1  # 引擎只建一次


def test_chat_kb_list_change_rebuilds_engine(monkeypatch):
    """同 session_id 但 KB 列表变化 → 重建引擎。"""
    build_count = {"n": 0}

    def fake_build(kb_ids, top_k):
        build_count["n"] += 1
        return _fake_engine()

    monkeypatch.setattr(qa_service, "_build_chat_engine", fake_build)

    r1 = qa_service.chat(None, "q", ["kb1"])
    sid = r1["session_id"]
    qa_service.chat(sid, "q", ["kb2"])  # KB 列表变更

    assert build_count["n"] == 2  # 重建一次


def test_cleanup_sessions_evicts_expired():
    """超过 MAX_SESSION_AGE 的 session → 被 _cleanup_sessions 清除。"""
    qa_service._sessions["old"] = {
        "engine": MagicMock(),
        "kb_ids": ["kb1"],
        "created_at": time.time() - qa_service.MAX_SESSION_AGE - 1,
    }
    qa_service._cleanup_sessions()
    assert "old" not in qa_service._sessions


# ── 追问提取 ───────────────────────────────────────────────────────────────────


def test_extract_suggestions():
    text = "回答内容\n【追问】第一个问题？\n【追问】第二个问题？"
    assert qa_service._extract_suggestions(text) == ["第一个问题？", "第二个问题？"]


def test_strip_suggestions():
    text = "回答内容\n【追问】追问问题？\n结尾"
    stripped = qa_service._strip_suggestions(text)
    assert "【追问】" not in stripped
    assert "回答内容" in stripped
    assert "结尾" in stripped


# ── 错误降级 ───────────────────────────────────────────────────────────────────


def test_chat_llm_failure_returns_fallback(monkeypatch):
    """engine.chat 抛错 → 返回固定兜底文案 + 空 sources。"""
    engine = MagicMock()
    engine.chat.side_effect = RuntimeError("llm down")
    monkeypatch.setattr(qa_service, "_build_chat_engine", lambda kb_ids, top_k: engine)

    result = qa_service.chat(None, "问题", ["kb1"])

    assert result["answer"] == "抱歉，回答生成失败。"
    assert result["sources"] == []


def test_chat_stream_yields_progress_then_tokens(monkeypatch):
    """流式：progress 事件 → token 事件 → done 事件。"""
    engine = MagicMock()
    engine.stream_chat.return_value = _FakeStreamResponse(["你", "好", ""])
    monkeypatch.setattr(qa_service, "_build_chat_engine", lambda kb_ids, top_k: engine)

    events = list(qa_service.chat_stream(None, "问题", ["kb1"]))
    types = [e["type"] for e in events]

    assert "progress" in types
    token_events = [e for e in events if e["type"] == "token"]
    assert [e["text"] for e in token_events] == ["你", "好"]  # 空 delta 被过滤
    assert types[-1] == "done"
