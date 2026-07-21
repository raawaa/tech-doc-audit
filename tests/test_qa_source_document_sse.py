"""V9 PRD #67 — 内联 source-document SSE 事件单元测试。

覆盖：
- build_source_id 形态（src_<8hex>_p<1-based-page>）
- build_source_document_payload 与 AI SDK v6 source-document schema 对齐
- parse_search_kb_tool_output：从 search_kb 结构化输出抽取 doc_id / page / block_range
- _build_source_document_events：同 doc_id 去重 + 空 doc_id 跳过
"""

import pytest

from api.routers.qa import (
    build_source_id,
    build_source_document_payload,
    _build_source_document_events,
)
from services.agent_tools import parse_search_kb_tool_output


class TestBuildSourceId:
    def test_format_matches_src_doc_short_p_page(self):
        sid = build_source_id({"doc_id": "abc", "page_number": 3})
        assert sid.startswith("src_")
        assert sid.endswith("_p4")  # 0-based page 3 → 1-based page 4

    def test_empty_doc_id_yields_empty_token(self):
        assert build_source_id({"doc_id": "", "page_number": 0}) == "src_empty_p1"

    def test_null_page_yields_p0(self):
        assert build_source_id({"doc_id": "abc", "page_number": None}).endswith("_p0")

    def test_undefined_page_yields_p0(self):
        assert build_source_id({"doc_id": "abc"}).endswith("_p0")

    def test_is_stable_across_calls(self):
        a = build_source_id({"doc_id": "X", "page_number": 2})
        b = build_source_id({"doc_id": "X", "page_number": 2})
        assert a == b


class TestBuildSourceDocumentPayload:
    def test_shape_matches_ai_sdk_v6(self):
        payload = build_source_document_payload({
            "doc_id": "doc-1",
            "doc_source": "GB/T 12345",
            "page_number": 3,
            "relevance": 0.85,
            "content_snippet": "片段",
            "block_range": [2, 5],
        })
        assert payload["type"] == "source-document"
        assert payload["mediaType"] == "application/pdf"
        assert payload["title"] == "GB/T 12345"
        assert payload["filename"] == "doc-1"
        assert payload["sourceId"].startswith("src_")
        # 原 QASource 透传到 providerMetadata.qaSource
        meta = payload["providerMetadata"]
        assert meta["qaSource"]["doc_id"] == "doc-1"
        assert meta["qaSource"]["block_range"] == [2, 5]
        assert meta["qaSource"]["page_number"] == 3

    def test_title_falls_back_to_unknown_source(self):
        payload = build_source_document_payload({
            "doc_id": "doc-1", "doc_source": "", "page_number": 0,
        })
        assert payload["title"] == "未知来源"

    def test_filename_omitted_when_doc_id_empty(self):
        # 当前实现下 build_source_document_payload 仍接受空 doc_id（_build_source_document_events
        # 才是上游 dedupe 屏障），但 filename 字段必须被省略以免误导。
        payload = build_source_document_payload({
            "doc_id": "", "doc_source": "x", "page_number": 0,
        })
        assert "filename" not in payload


class TestParseToolSources:
    def test_search_kb_structured_extracts_doc_id_page_block_range(self):
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
        assert len(sources) == 2
        assert sources[0]["doc_id"] == "doc-aaa"
        assert sources[0]["page_number"] == 4  # 1-based 第5页 → 0-based page 4
        assert sources[0]["block_range"] == [2, 5]
        assert sources[0]["relevance"] == 0.92
        assert sources[1]["doc_id"] == "doc-bbb"
        assert sources[1]["block_range"] is None

    def test_search_kb_text_returns_empty_no_doc_id(self):
        # search_kb_text 输出无结构化 doc_id → 不产 chip
        tool_out = "【知识库文本搜索结果（精确匹配: GB）】\n【doc=xxx / page=0】..."
        assert parse_search_kb_tool_output(tool_out) == []

    def test_empty_or_none_returns_empty(self):
        assert parse_search_kb_tool_output("") == []
        assert parse_search_kb_tool_output(None) == []  # type: ignore[arg-type]

    def test_dedup_within_one_tool_output(self):
        # 同一 tool 输出里若出现重复 doc_id，仅留首条
        tool_out = (
            "1. 【A】第1条\n   相关度: 0.9 | doc_id: doc-x | 页码: 第1页\n   text\n"
            "2. 【A】第2条\n   相关度: 0.8 | doc_id: doc-x | 页码: 第2页\n   text\n"
        )
        sources = parse_search_kb_tool_output(tool_out)
        assert len(sources) == 1


class TestEmitSourceDocuments:
    def test_dedups_across_calls_by_doc_id(self):
        seen: set[str] = set()
        s1 = {"doc_id": "doc-aaa", "doc_source": "A", "page_number": 0,
              "relevance": 0.9, "content_snippet": "", "block_range": None}
        s2 = {"doc_id": "doc-aaa", "doc_source": "A", "page_number": 0,
              "relevance": 0.8, "content_snippet": "", "block_range": None}
        out1 = _build_source_document_events([s1], seen)
        out2 = _build_source_document_events([s2], seen)
        assert len(out1) == 1
        assert len(out2) == 0  # 已被同 stream 首发 dedupe

    def test_skips_empty_doc_id(self):
        seen: set[str] = set()
        out = _build_source_document_events(
            [{"doc_id": "", "doc_source": "x", "page_number": 0,
              "relevance": 0.0, "content_snippet": "", "block_range": None}],
            seen,
        )
        assert out == []

    def test_multiple_unique_doc_ids_each_emit_once(self):
        seen: set[str] = set()
        sources = [
            {"doc_id": "doc-a", "doc_source": "A", "page_number": 0,
             "relevance": 0.9, "content_snippet": "", "block_range": None},
            {"doc_id": "doc-b", "doc_source": "B", "page_number": 1,
             "relevance": 0.8, "content_snippet": "", "block_range": [1, 3]},
            {"doc_id": "doc-c", "doc_source": "C", "page_number": 2,
             "relevance": 0.7, "content_snippet": "", "block_range": None},
        ]
        out = _build_source_document_events(sources, seen)
        assert len(out) == 3
        assert {e["sourceId"] for e in out} == {
            build_source_id(s) for s in sources
        }