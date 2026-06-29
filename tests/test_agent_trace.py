"""services.agent_trace.save_trace 的测试（写 tmp_path，不加载模型）。"""
import json

from services.agent_trace import save_trace


def test_metadata_roundtrip_and_timestamp(tmp_path):
    p = tmp_path / "t.json"
    res = save_trace(
        p,
        [{"role": "user", "content": "hi"}],
        metadata={"qa_id": "q1", "finished": True},
    )
    assert res == p
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["qa_id"] == "q1"
    assert data["finished"] is True
    assert data.get("timestamp")  # 自动注入 UTC ISO timestamp
    assert data["messages"] == [{"role": "user", "content": "hi"}]


def test_content_truncated(tmp_path):
    p = tmp_path / "t.json"
    big = "X" * 11000
    save_trace(p, [{"role": "assistant", "content": big}])
    data = json.loads(p.read_text(encoding="utf-8"))
    content = data["messages"][0]["content"]
    assert "truncated" in content
    assert len(content) < len(big)


def test_tool_call_args_truncated(tmp_path):
    p = tmp_path / "t.json"
    big_args = "Y" * 6000
    msgs = [{
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "1",
            "type": "function",
            "function": {"name": "search_kb", "arguments": big_args},
        }],
    }]
    save_trace(p, msgs)
    data = json.loads(p.read_text(encoding="utf-8"))
    args = data["messages"][0]["tool_calls"][0]["function"]["arguments"]
    assert "truncated" in args
    assert len(args) < len(big_args)


def test_does_not_mutate_caller_messages(tmp_path):
    p = tmp_path / "t.json"
    big = "X" * 11000
    msgs = [{"role": "assistant", "content": big}]
    save_trace(p, msgs)
    assert msgs[0]["content"] == big  # 调用方消息不被修改


def test_unwritable_returns_none(tmp_path):
    # blocker 是文件，作为父目录会令 mkdir 失败 → 返回 None，不抛异常
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    p = blocker / "t.json"
    res = save_trace(p, [{"role": "user", "content": "hi"}])
    assert res is None


def test_creates_parent_dirs(tmp_path):
    p = tmp_path / "a" / "b" / "c" / "t.json"
    res = save_trace(p, [{"role": "user", "content": "hi"}], metadata={"k": "v"})
    assert res == p
    assert p.exists()


def test_no_metadata(tmp_path):
    p = tmp_path / "t.json"
    save_trace(p, [{"role": "user", "content": "hi"}])
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["messages"] == [{"role": "user", "content": "hi"}]
    assert data.get("timestamp")
