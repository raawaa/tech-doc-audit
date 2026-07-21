"""services.agent_tools 的格式化测试。

monkeypatch 底层检索（vec_search / _get_kb_search_paths / _run_rga），
覆盖结果格式化、来源单一性警告、空结果、失败建议文案——不加载任何模型。

V9 PRD #67：parse_search_kb_tool_output 是 search_kb 文本格式的事实定义，
由 api/routers/qa.py 与 services/agentic_qa.py 复用，本文件末尾补其单测。
"""
import services.vector_search as vector_search
from services.agent_tools import parse_search_kb_tool_output, search_kb, search_kb_text


def _result(**kw):
    base = {
        "relevance": 0.8,
        "doc_source": "GB-标准A",
        "doc_id": "doc_a",
        "clause_number": "",
        "section_path": "",
        "page_number": None,
        "content": "条款内容",
    }
    base.update(kw)
    return base


def _vec_search_stub(return_value):
    """构造一个接受 vec_search 全部位置+关键字参数的 mock lambda。

    agent_tools.search_kb 现在透传 sync_rebuild_for_audit 给 vec_search，
    测试用 lambda 必须同样接受，否则会被报"unexpected keyword argument"。
    """
    def _stub(kb_ids, query, top_k=5, **kwargs):
        return return_value
    return _stub


# ── search_kb ────────────────────────────────────────────────────────────────

def test_search_kb_empty_query_or_kb():
    assert "未提供" in search_kb([], "q")
    assert "未提供" in search_kb(["kb1"], "")


def test_search_kb_formats_results(monkeypatch):
    monkeypatch.setattr(
        vector_search, "vec_search",
        _vec_search_stub([_result(relevance=0.82, content="条款内容X")]),
    )
    out = search_kb(["kb1"], "质保期")
    assert "知识库搜索结果" in out
    assert "质保期" in out
    assert "相关度: 0.82" in out
    assert "条款内容X" in out


def test_search_kb_source_diversity_warning(monkeypatch):
    # 两条结果来自同一 doc_id → 触发来源单一性警告（QA 采用 audit 版本后的行为变更）
    monkeypatch.setattr(
        vector_search, "vec_search",
        _vec_search_stub([_result(doc_id="doc_a"), _result(doc_id="doc_a")]),
    )
    out = search_kb(["kb1"], "q")
    assert "来源单一性警告" in out


def test_search_kb_multi_doc_no_warning(monkeypatch):
    monkeypatch.setattr(
        vector_search, "vec_search",
        _vec_search_stub([_result(doc_id="doc_a"), _result(doc_id="doc_b")]),
    )
    out = search_kb(["kb1"], "q")
    assert "来源单一性警告" not in out


def test_search_kb_no_results(monkeypatch):
    monkeypatch.setattr(vector_search, "vec_search", _vec_search_stub([]))
    out = search_kb(["kb1"], "q")
    assert "未找到" in out


def test_search_kb_failure_advice(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(vector_search, "vec_search", boom)
    out = search_kb(["kb1"], "q")
    assert "语义搜索失败" in out
    assert "建议" in out
    assert "search_kb_text" in out  # 失败建议指向兄弟工具


# ── V8-S3: search_kb 透传 block_range 到 LLM 可见输出 ────────────────────────────


def test_search_kb_includes_block_range_in_meta_line(monkeypatch):
    """hit 带 block_range 时,格式化输出追加 ``block_range: (start, end)`` 行。"""
    monkeypatch.setattr(
        vector_search, "vec_search",
        _vec_search_stub([_result(doc_id="doc_a", page_number=2,
                                  block_range=(3, 7), content="条款内容X")]),
    )
    out = search_kb(["kb1"], "质保期")
    assert "block_range:" in out
    assert "(3, 7)" in out
    # 同时页码仍正常显示
    assert "第3页" in out  # 0-based + 1 → 1-based display


def test_search_kb_omits_block_range_when_none(monkeypatch):
    """hit 的 block_range = None（旧 KB chunk）时,格式化输出不加该字段。

    防止对 LLM 输出加噪音——LLM 不需要按字段思考。
    """
    monkeypatch.setattr(
        vector_search, "vec_search",
        _vec_search_stub([_result(doc_id="doc_a", page_number=2,
                                  block_range=None, content="条款内容X")]),
    )
    out = search_kb(["kb1"], "质保期")
    assert "block_range:" not in out


def test_search_kb_omits_block_range_when_absent(monkeypatch):
    """hit 完全不带 block_range key（旧 hit dict 兼容场景）→ 输出也不加。"""
    monkeypatch.setattr(
        vector_search, "vec_search",
        _vec_search_stub([{"relevance": 0.9, "doc_id": "d1",
                           "doc_source": "GB-X", "page_number": 0,
                           "content": "旧 hit 无 block_range"}]),
    )
    out = search_kb(["kb1"], "q")
    assert "block_range:" not in out


# ── search_kb_text（V5：实际走 pages grep，由 search_doc_by_text 实现）──────────────


def test_search_kb_text_formats(monkeypatch):
    monkeypatch.setattr(
        vector_search, "search_doc_by_text",
        lambda query, kb_ids: [
            {"doc_id": "d1", "kb_id": "kb1", "page_number": 3, "content": "命中行A"},
            {"doc_id": "d2", "kb_id": "kb1", "page_number": 7, "content": "命中行B"},
        ],
    )
    out = search_kb_text(["kb1"], "GB/T 12345")
    assert "知识库文本搜索结果" in out
    assert "命中行A" in out
    assert "命中行B" in out
    assert "page=3" in out


def test_search_kb_text_truncates(monkeypatch):
    long_content = "X" * 6000
    monkeypatch.setattr(
        vector_search, "search_doc_by_text",
        lambda query, kb_ids: [
            {"doc_id": "d1", "kb_id": "kb1", "page_number": 0, "content": long_content},
        ],
    )
    out = search_kb_text(["kb1"], "q")
    assert "截断" in out


def test_search_kb_text_no_results(monkeypatch):
    monkeypatch.setattr(vector_search, "search_doc_by_text", lambda *a, **k: [])
    out = search_kb_text(["kb1"], "q")
    assert "未找到" in out


def test_search_kb_text_failure_advice(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(vector_search, "search_doc_by_text", boom)
    out = search_kb_text(["kb1"], "q")
    assert "文本搜索失败" in out
    assert "建议" in out


# ── V9 PRD #67 — parse_search_kb_tool_output（共享解析）─────────────────────

class TestParseSearchKbToolOutput:
    """search_kb 文本格式的事实定义。修改 search_kb() 输出格式时，
    必须同步更新本测试与所有消费方（qa.py、agentic_qa.py）。"""

    def test_empty_or_none_returns_empty(self):
        assert parse_search_kb_tool_output("") == []
        assert parse_search_kb_tool_output(None) == []  # type: ignore[arg-type]

    def test_search_kb_text_is_rejected(self):
        # search_kb_text 输出无结构化 doc_id → 返回空
        out = "【知识库文本搜索结果（精确匹配: GB）】\n【doc=xxx / page=0】..."
        assert parse_search_kb_tool_output(out) == []

    def test_extracts_doc_id_page_block_range(self):
        tool_out = (
            "【知识库搜索结果（搜索词: 质保期，共 2 条）】\n"
            "\n"
            "1. 【GB/T 12345】第3.2条\n"
            "   相关度: 0.92 | doc_id: doc-aaa | 页码: 第5页 | block_range: (2, 5)\n"
            "   质保期 24 个月。\n"
            "\n"
            "2. 【JB/T 9999】第1条\n"
            "   相关度: 0.81 | doc_id: doc-bbb | 页码: 第2页\n"
            "   备品备件应满足最低要求。\n"
        )
        sources = parse_search_kb_tool_output(tool_out)
        assert [s["doc_id"] for s in sources] == ["doc-aaa", "doc-bbb"]
        assert sources[0]["page_number"] == 4  # 1-based 第5页 → 0-based page 4
        assert sources[0]["block_range"] == [2, 5]
        assert sources[0]["relevance"] == 0.92
        assert sources[1]["block_range"] is None

    def test_skips_source_diversity_warning_lines(self):
        # "⚠️ 来源单一性警告" 行不应被吞进 content_snippet
        tool_out = (
            "1. 【A】第1条\n"
            "   相关度: 0.9 | doc_id: doc-a | 页码: 第1页\n"
            "   真实内容\n"
            "\n⚠️ 来源单一性警告：所有结果均来自同一份标准文档（A）。\n"
        )
        sources = parse_search_kb_tool_output(tool_out)
        assert len(sources) == 1
        assert "真实内容" in sources[0]["content_snippet"]
        assert "⚠️" not in sources[0]["content_snippet"]
        assert "来源单一性警告" not in sources[0]["content_snippet"]

    def test_dedup_within_one_tool_output(self):
        # 同一 tool 输出里若出现重复 doc_id，仅留首条
        tool_out = (
            "1. 【A】第1条\n   相关度: 0.9 | doc_id: doc-x | 页码: 第1页\n   t1\n"
            "2. 【A】第2条\n   相关度: 0.8 | doc_id: doc-x | 页码: 第2页\n   t2\n"
        )
        assert len(parse_search_kb_tool_output(tool_out)) == 1

    def test_block_without_doc_id_is_dropped(self):
        # 缺 doc_id 的块（理论上 search_kb 不会产出，旧 KB 兼容）不被保留
        tool_out = (
            "1. 【无名】第1条\n   相关度: 0.9 | 页码: 第1页\n   内容\n"
        )
        assert parse_search_kb_tool_output(tool_out) == []
