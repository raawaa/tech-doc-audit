"""Agentic audit pipeline tests."""

import os
import pytest
from unittest.mock import patch, MagicMock

from models.llm_schemas import AgentAction


class TestAgentAction:
    """测试 AgentAction 模型。"""

    def test_read_chapter_action(self):
        a = AgentAction(thought="读第3章", action="read_chapter", chapter_index=3)
        assert a.action == "read_chapter"
        assert a.chapter_index == 3

    def test_search_kb_action(self):
        a = AgentAction(
            thought="搜索防护等级",
            action="search_kb",
            search_query="防护等级IP65",
            search_top_k=5,
        )
        assert a.search_query == "防护等级IP65"
        assert a.search_top_k == 5

    def test_flag_issue_action(self):
        a = AgentAction(
            thought="发现质保期不达标",
            action="flag_issue",
            issue_type="compliance",
            issue_severity="high",
            issue_description="质保期不足",
            standard_name="CJJ101-2016",
            standard_clause="3.2.1",
            cited_excerpt="质保期12个月",
            document_position="第3章",
        )
        assert a.issue_type == "compliance"
        assert a.issue_severity == "high"

    def test_finish_action(self):
        a = AgentAction(
            thought="审核完成",
            action="finish",
            final_summary="共发现3个问题",
        )
        assert a.action == "finish"
        assert a.final_summary == "共发现3个问题"


class TestChapterExtraction:
    """测试章节文本提取。"""

    def test_chapter_label(self):
        from services.agentic_audit import _chapter_label
        from models.audit_document import Chapter

        ch = Chapter(number="三", title="技术规格")
        assert "第三章" in _chapter_label(ch, 2)
        assert "技术规格" in _chapter_label(ch, 2)

    def test_find_chapter_text_markdown(self):
        from services.agentic_audit import _find_chapter_text
        from models.audit_document import DocumentStructure, Chapter, Clause

        structure = DocumentStructure(
            title="test",
            chapters=[
                Chapter(number="一", title="概述", clauses=[Clause(number="1.1", text="...")]),
                Chapter(number="二", title="要求", clauses=[Clause(number="2.1", text="...")]),
            ],
            total_clauses=2,
        )
        content = "# 第一章 概述\n\n这是概述内容。\n\n# 第二章 要求\n\n这是要求内容。"

        text1 = _find_chapter_text(content, structure, 0)
        assert "概述内容" in text1
        assert "要求" not in text1

        text2 = _find_chapter_text(content, structure, 1)
        assert "要求内容" in text2

    def test_find_chapter_text_no_structure(self):
        from services.agentic_audit import _find_chapter_text
        from models.audit_document import DocumentStructure, Chapter

        structure = DocumentStructure(
            title="test",
            chapters=[Chapter(title="全文")],
            total_clauses=0,
        )
        content = "这是一篇没有结构的文档。"
        text = _find_chapter_text(content, structure, 0)
        assert "没有结构" in text

    def test_tool_get_structure(self):
        from services.agentic_audit import _tool_get_structure
        from models.audit_document import DocumentStructure, Chapter, Clause

        structure = DocumentStructure(
            title="test",
            chapters=[
                Chapter(number="1", title="概述", clauses=[
                    Clause(number="1.1", text="..."),
                    Clause(number="1.2", text="..."),
                ]),
                Chapter(number="2", title="要求", clauses=[Clause(number="2.1", text="...")]),
            ],
            total_clauses=3,
        )
        result = _tool_get_structure(structure, "test.pdf")
        assert "2 章" in result
        assert "3 个条款" in result
        assert "概述" in result
        assert "1.1" in result

    def test_tool_get_structure_none(self):
        from services.agentic_audit import _tool_get_structure
        result = _tool_get_structure(None, "test.pdf")
        assert "无结构信息" in result

    def test_tool_flag_issue(self):
        from services.agentic_audit import _tool_flag_issue
        from models.audit_task import AuditIssue
        from models.llm_schemas import AgentAction

        issues = []
        action = AgentAction(
            thought="test",
            action="flag_issue",
            issue_type="compliance",
            issue_severity="high",
            issue_description="不符合标准",
            standard_name="GB/T 123",
            standard_clause="5.2",
        )
        result = _tool_flag_issue(action, issues)
        assert "问题 #1 已记录" in result
        assert len(issues) == 1
        assert issues[0].type == "compliance"
        assert issues[0].severity == "high"


class TestPipelineRouting:
    """测试审核管线选择逻辑。"""

    @patch("services.agentic_audit.run_agentic_audit")
    def test_agentic_audit_activated(self, mock_agentic):
        """USE_AGENTIC_AUDIT=true 时应走 agentic 管线。"""
        os.environ["USE_AGENTIC_AUDIT"] = "true"

        from services.audit_task_service import repo as task_repo
        from services.audit_task_service import doc_repo
        from models.audit_task import AuditTask, AuditResult, ResultSummary
        from models.audit_document import DocumentStructure

        # Setup mock doc with structure set (skip structure analysis)
        mock_doc = MagicMock()
        mock_doc.id = "doc_001"
        mock_doc.name = "test.pdf"
        mock_doc.parsed_content = "test content"
        mock_doc.structure = DocumentStructure(
            chapters=[], total_clauses=0,
        )
        doc_repo.get_doc = MagicMock(return_value=mock_doc)

        # Setup mock task with pre-set topics (skip agent_audit LLM call)
        task = AuditTask(
            id="task_001",
            document_id="doc_001",
            document_name="test.pdf",
            kb_ids=[],
            status="pending",
        )
        object.__setattr__(task, 'audit_topics', [{"id": "test", "name": "测试主题", "prompt": "test", "keywords": ["test"]}])
        task_repo.get_task = MagicMock(return_value=task)
        task_repo.save_task = MagicMock()

        # Mock agentic result
        mock_result = AuditResult(
            task_id="task_001",
            document_id="doc_001",
            document_name="test.pdf",
            summary=ResultSummary(),
            issues=[],
            raw_analysis="Agentic audit done",
        )
        mock_agentic.return_value = mock_result

        from services.audit_task_service import run_audit
        result = run_audit("task_001")

        mock_agentic.assert_called_once()
        assert result.status == "completed"

        del os.environ["USE_AGENTIC_AUDIT"]

    @patch("services.agentic_audit.run_agentic_audit")
    def test_agentic_fallback_to_topic(self, mock_agentic):
        """agentic 失败时应降级到 topic_audit。"""
        os.environ["USE_AGENTIC_AUDIT"] = "true"

        from services.audit_task_service import repo as task_repo
        from services.audit_task_service import doc_repo
        from models.audit_task import AuditTask
        from models.audit_document import DocumentStructure

        mock_doc = MagicMock()
        mock_doc.id = "doc_002"
        mock_doc.name = "test.pdf"
        mock_doc.parsed_content = "test content"
        mock_doc.structure = DocumentStructure(
            chapters=[], total_clauses=0,
        )
        doc_repo.get_doc = MagicMock(return_value=mock_doc)

        task = AuditTask(
            id="task_002",
            document_id="doc_002",
            document_name="test.pdf",
            kb_ids=[],
            status="pending",
        )
        object.__setattr__(task, 'audit_topics', [{"id": "test", "name": "测试主题", "prompt": "test", "keywords": ["test"]}])
        task_repo.get_task = MagicMock(return_value=task)
        task_repo.save_task = MagicMock()

        # Agentic raises
        mock_agentic.side_effect = RuntimeError("LLM unavailable")

        # Mock topic audit
        with patch("services.audit_task_service._run_topic_audit_pipeline") as mock_topic:
            mock_topic.return_value = ([], ["主题审核: 无问题"], task)

            from services.audit_task_service import run_audit
            result = run_audit("task_002")

            mock_agentic.assert_called_once()
            mock_topic.assert_called_once()
            assert result.status == "completed"

        del os.environ["USE_AGENTIC_AUDIT"]


class TestFallbackParser:
    """测试 structured_llm 降级解析。"""

    def test_json_parse(self):
        from services.agentic_audit import _parse_action_fallback
        result = _parse_action_fallback(
            '{"thought": "读取章节", "action": "read_chapter", "chapter_index": 1}'
        )
        assert result is not None
        assert result.action == "read_chapter"

    def test_markdown_wrapped_json(self):
        from services.agentic_audit import _parse_action_fallback
        result = _parse_action_fallback(
            '```json\n{"thought": "搜索", "action": "search_kb", "search_query": "IP65"}\n```'
        )
        assert result is not None
        assert result.action == "search_kb"
        assert result.search_query == "IP65"

    def test_invalid_json(self):
        from services.agentic_audit import _parse_action_fallback
        result = _parse_action_fallback("这不是 JSON")
        assert result is None

    def test_missing_action_field(self):
        from services.agentic_audit import _parse_action_fallback
        result = _parse_action_fallback('{"thought": "test"}')
        assert result is None
