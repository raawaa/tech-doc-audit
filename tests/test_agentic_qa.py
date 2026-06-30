"""agentic_qa._extract_sources 测试 — 从 search_kb 工具结果解析完整来源字段。

_extract_sources 必须从 search_kb 返回的结构化文本中解析出：
doc_source / doc_id / page_number(0-based) / content_snippet / relevance，
而非旧实现那样仅保留文档名。
"""

from services.agentic_qa import _extract_sources


def _search_kb_result_text() -> str:
    """构造一段模拟 search_kb 工具返回的结构化文本（格式对齐 agent_tools.search_kb）。"""
    return (
        "【知识库搜索结果（搜索词: 质保期，共 2 条）】\n"
        "\n"
        "1. 【GB/T 1234-2018】 第 3.2.1 条\n"
        "   相关度: 0.85 | doc_id: doc-abc | 页码: 第5页\n"
        "   本文档规定了质保期不得少于两年。\n"
        "\n"
        "2. 【GB/T 5678-2020】\n"
        "   相关度: 0.42 | doc_id: doc-xyz | 页码: 第1页\n"
        "   一般要求。\n"
    )


def test_extract_sources_parses_doc_id_and_page_number():
    messages = [{"role": "tool", "content": _search_kb_result_text()}]
    sources = _extract_sources(messages)

    assert len(sources) == 2

    first = sources[0]
    assert first["doc_source"] == "GB/T 1234-2018"
    assert first["doc_id"] == "doc-abc"
    assert first["page_number"] == 4  # 第5页(1-based) → 0-based 4
    assert first["relevance"] == 0.85
    assert "质保期" in first["content_snippet"]

    second = sources[1]
    assert second["doc_source"] == "GB/T 5678-2020"
    assert second["doc_id"] == "doc-xyz"
    assert second["page_number"] == 0  # 第1页 → 0
    assert second["relevance"] == 0.42


def test_extract_sources_page_number_none_when_missing():
    """非 PDF / 无页码的来源：page_number 为 None，doc_id 仍可解析。"""
    content = (
        "【知识库搜索结果（搜索词: x，共 1 条）】\n"
        "\n"
        "1. 【某非PDF文档】\n"
        "   相关度: 0.30 | doc_id: doc-nopage\n"
        "   内容片段。\n"
    )
    sources = _extract_sources([{"role": "tool", "content": content}])
    assert len(sources) == 1
    assert sources[0]["doc_id"] == "doc-nopage"
    assert sources[0]["page_number"] is None


def test_extract_sources_dedup_by_doc_source_across_messages():
    """同名文档跨多次搜索去重，保留首次命中。"""
    msg1 = {
        "role": "tool",
        "content": (
            "【知识库搜索结果（搜索词: a，共 1 条）】\n"
            "\n"
            "1. 【重复文档】\n"
            "   相关度: 0.80 | doc_id: doc-1 | 页码: 第2页\n"
            "   第一次命中。\n"
        ),
    }
    msg2 = {
        "role": "tool",
        "content": (
            "【知识库搜索结果（搜索词: b，共 1 条）】\n"
            "\n"
            "1. 【重复文档】\n"
            "   相关度: 0.50 | doc_id: doc-1 | 页码: 第9页\n"
            "   第二次命中。\n"
        ),
    }
    sources = _extract_sources([msg1, msg2])
    assert len(sources) == 1  # 同名文档去重
    assert sources[0]["page_number"] == 1  # 第2页 → 1（首次命中保留）


def test_extract_sources_ignores_non_tool_messages_and_text_search_headers():
    """非 tool 消息不参与；search_kb_text 的 header 标记不计为来源。"""
    messages = [
        {"role": "user", "content": "问题"},
        {"role": "assistant", "content": "答案"},
        {"role": "tool", "content": "【知识库文本搜索结果（精确匹配: GB）】\n无命中"},
    ]
    sources = _extract_sources(messages)
    assert sources == []


def test_extract_sources_skips_single_source_warning():
    """来源单一性警告行不计入来源或内容。"""
    content = (
        "【知识库搜索结果（搜索词: a，共 1 条）】\n"
        "\n"
        "1. 【唯一文档】\n"
        "   相关度: 0.80 | doc_id: doc-1 | 页码: 第2页\n"
        "   内容。\n"
        "\n"
        "⚠️ 来源单一性警告：所有 1 条结果均来自同一份标准文档（唯一文档）。请换关键词。\n"
    )
    sources = _extract_sources([{"role": "tool", "content": content}])
    assert len(sources) == 1
    assert "来源单一性警告" not in sources[0]["content_snippet"]
