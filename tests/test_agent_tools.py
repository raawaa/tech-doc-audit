"""services.agent_tools 的格式化测试。

monkeypatch 底层检索（vec_search / _get_kb_search_paths / _run_rga），
覆盖结果格式化、来源单一性警告、空结果、失败建议文案——不加载任何模型。
"""
import services.vector_search as vector_search
from services.agent_tools import search_kb, search_kb_text


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
