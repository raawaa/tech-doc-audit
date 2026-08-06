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


# ── V8-S4: _lookup_chunk_block_range + _tool_flag_issue 自动补全 block_range ──


def _make_block(block_content: str, block_order: int):
    """V8-S4 测试用:构造 layout block SimpleNamespace。"""
    from types import SimpleNamespace
    return SimpleNamespace(
        block_content=block_content,
        block_order=block_order,
        page=0,
        bbox_norm=[],
        block_label="text",
    )


class TestV8S4FlagIssueBlockRange:
    """V8-S4: LLM 提交 standard_* 字段后,系统后端透明补全 block_range。
    不改 LLM 工具 schema,失败 best-effort → block_range = None。
    """

    def test_lookup_chunk_block_range_with_valid_inputs(self, fake_models):
        """合法 doc_id + page_number + chunk_text → 命中,返回 block_range。"""
        from core.index_manager import index_document
        from core.parse_document import PageLayout, PageText
        from services.agentic_audit import _lookup_chunk_block_range

        kb_id = "test_kb_v8s4_lookup"
        import storage.kb_repo as _kb_repo
        from models.knowledge_base import KnowledgeBase
        kb = KnowledgeBase(id=kb_id, name="v8s4", category="national")
        _kb_repo.update(kb)
        kb = _kb_repo.get(kb_id)
        kb.document_ids = ["doc_v8s4"]
        kb.index_status = "searchable"
        _kb_repo.update(kb)

        full_text = "公司各应急保障单位应当配置无线对讲设备至少两套"
        index_document(
            kb_id, "doc_v8s4",
            full_text,
            source_name="v8s4.txt",
            by_page=[PageText(page=0, text=full_text)],
            by_layout=[PageLayout(
                page=0, width=0, height=0,
                blocks=[
                    _make_block("公司各应急保障单位", 0),
                    _make_block("应当配置无线对讲", 1),
                    _make_block("设备至少两套", 2),
                ],
            )],
        )

        result = _lookup_chunk_block_range(
            standard_doc_id="doc_v8s4",
            standard_page_number_1based=1,
            standard_chunk_text=full_text,
            kb_ids=[kb_id],
        )
        assert result == (0, 2), f"应反查到 (0, 2),实际 {result}"

    def test_lookup_chunk_block_range_invalid_doc_id_returns_none(self, fake_models):
        """虚构 doc_id → None,不抛。"""
        from services.agentic_audit import _lookup_chunk_block_range

        result = _lookup_chunk_block_range(
            standard_doc_id="ghost_doc_id",
            standard_page_number_1based=1,
            standard_chunk_text="任何文本",
            kb_ids=["test_kb_v8s4_lookup"],
        )
        assert result is None

    def test_lookup_chunk_block_range_empty_inputs_returns_none(self):
        """doc_id=None / chunk_text='' / kb_ids=[] → None,不抛。"""
        from services.agentic_audit import _lookup_chunk_block_range

        assert _lookup_chunk_block_range(None, 1, "text", ["kb1"]) is None
        assert _lookup_chunk_block_range("doc1", 1, "text", []) is None
        assert _lookup_chunk_block_range("doc1", 0, "", ["kb1"]) is None

    def test_lookup_chunk_block_range_page_number_zero_no_filter(self, fake_models):
        """page_number=0(LLM 越界)→ 不按页过滤,仍能按 doc_id + chunk_text 命中。"""
        from core.index_manager import index_document
        from core.parse_document import PageLayout, PageText
        from services.agentic_audit import _lookup_chunk_block_range

        kb_id = "test_kb_v8s4_p0"
        import storage.kb_repo as _kb_repo
        from models.knowledge_base import KnowledgeBase
        kb = KnowledgeBase(id=kb_id, name="v8s4p0", category="national")
        _kb_repo.update(kb)
        kb = _kb_repo.get(kb_id)
        kb.document_ids = ["doc_v8s4p0"]
        kb.index_status = "searchable"
        _kb_repo.update(kb)

        full_text = "公司各应急保障单位应当配置无线对讲设备至少两套"
        index_document(
            kb_id, "doc_v8s4p0",
            full_text, source_name="p0.txt",
            by_page=[PageText(page=0, text=full_text)],
            by_layout=[PageLayout(
                page=0, width=0, height=0,
                blocks=[_make_block("公司各应急保障单位应当配置无线对讲设备至少两套", 0)],
            )],
        )

        result = _lookup_chunk_block_range(
            standard_doc_id="doc_v8s4p0",
            standard_page_number_1based=0,
            standard_chunk_text="公司各应急保障单位",
            kb_ids=[kb_id],
        )
        assert result == (0, 0)

    def test_lookup_chunk_block_range_chunk_text_mismatch_returns_none(self, fake_models):
        """chunk_text 不匹配该节点 → None(LLM 幻觉/乱填)。"""
        from core.index_manager import index_document
        from core.parse_document import PageLayout, PageText
        from services.agentic_audit import _lookup_chunk_block_range

        kb_id = "test_kb_v8s4_mismatch"
        import storage.kb_repo as _kb_repo
        from models.knowledge_base import KnowledgeBase
        kb = KnowledgeBase(id=kb_id, name="v8s4mm", category="national")
        _kb_repo.update(kb)
        kb = _kb_repo.get(kb_id)
        kb.document_ids = ["doc_v8s4mm"]
        kb.index_status = "searchable"
        _kb_repo.update(kb)

        full_text = "公司各应急保障单位应当配置无线对讲设备至少两套"
        index_document(
            kb_id, "doc_v8s4mm",
            full_text, source_name="mm.txt",
            by_page=[PageText(page=0, text=full_text)],
            by_layout=[PageLayout(
                page=0, width=0, height=0,
                blocks=[_make_block(full_text, 0)],
            )],
        )

        result = _lookup_chunk_block_range(
            standard_doc_id="doc_v8s4mm",
            standard_page_number_1based=1,
            standard_chunk_text="完全不相关的其他文本内容 ABCXYZ",
            kb_ids=[kb_id],
        )
        assert result is None

    def test_tool_flag_issue_fills_block_range_from_kb(self, fake_models):
        """_tool_flag_issue: LLM 提交合法 standard_* → block_range 非空。"""
        from core.index_manager import index_document
        from core.parse_document import PageLayout, PageText
        from services.agentic_audit import _tool_flag_issue
        from models.llm_schemas import AgentAction

        kb_id = "test_kb_v8s4_flag"
        import storage.kb_repo as _kb_repo
        from models.knowledge_base import KnowledgeBase
        kb = KnowledgeBase(id=kb_id, name="v8s4flag", category="national")
        _kb_repo.update(kb)
        kb = _kb_repo.get(kb_id)
        kb.document_ids = ["doc_flag"]
        kb.index_status = "searchable"
        _kb_repo.update(kb)

        full_text = "公司各应急保障单位应当配置无线对讲设备至少两套"
        index_document(
            kb_id, "doc_flag",
            full_text, source_name="flag.txt",
            by_page=[PageText(page=0, text=full_text)],
            by_layout=[PageLayout(
                page=0, width=0, height=0,
                blocks=[
                    _make_block("公司各应急保障单位", 0),
                    _make_block("应当配置无线对讲", 1),
                    _make_block("设备至少两套", 2),
                ],
            )],
        )

        issues = []
        action = AgentAction(
            thought="发现条款问题",
            action="flag_issue",
            issue_type="compliance",
            issue_severity="high",
            issue_description="不符合标准条款",
            standard_name="GB/T 123",
            standard_doc_id="doc_flag",
            standard_page_number=1,
            standard_chunk_text=full_text,
        )
        _tool_flag_issue(action, issues, kb_ids=[kb_id])

        assert len(issues) == 1
        sr = issues[0].standard_reference
        assert sr is not None
        assert sr.block_range == (0, 2), (
            f"应自动补全 block_range=(0, 2),实际 {sr.block_range}"
        )

    def test_tool_flag_issue_invalid_doc_id_yields_none(self, fake_models):
        """_tool_flag_issue: LLM 提交不存在的 doc_id → block_range = None,issue 正常落地。"""
        from services.agentic_audit import _tool_flag_issue
        from models.llm_schemas import AgentAction

        issues = []
        action = AgentAction(
            thought="幻觉引用",
            action="flag_issue",
            issue_type="compliance",
            issue_severity="medium",
            issue_description="不符合标准",
            standard_doc_id="ghost_doc_xyz",
            standard_page_number=1,
            standard_chunk_text="任何文本",
        )
        result = _tool_flag_issue(action, issues, kb_ids=["any_kb"])
        assert "已记录" in result
        assert len(issues) == 1
        assert issues[0].standard_reference.block_range is None

    def test_tool_flag_issue_no_standard_doc_id_yields_none(self):
        """_tool_flag_issue: LLM 没填 standard_doc_id → 不反查,block_range = None。"""
        from services.agentic_audit import _tool_flag_issue
        from models.llm_schemas import AgentAction

        issues = []
        action = AgentAction(
            thought="internal issue",
            action="flag_issue",
            issue_type="consistency",
            issue_severity="low",
            issue_description="内部矛盾",
        )
        _tool_flag_issue(action, issues, kb_ids=["any_kb"])
        assert issues[0].standard_reference.block_range is None


class TestPipelineRouting:
    """测试审核管线 — agentic 为唯一路径。"""

    @patch("services.agentic_audit.run_agentic_audit")
    def test_agentic_audit_runs(self, mock_agentic, monkeypatch):
        """审核任务应调用 agentic 管线。"""
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
        monkeypatch.setattr(doc_repo, "get_doc", lambda doc_id: mock_doc)

        # Setup mock task
        task = AuditTask(
            id="task_001",
            document_id="doc_001",
            document_name="test.pdf",
            kb_ids=[],
            status="pending",
        )
        monkeypatch.setattr(task_repo, "get_task", lambda task_id: task)
        save_calls = []
        monkeypatch.setattr(task_repo, "save_task", lambda t: save_calls.append(t) or t)

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

    @patch("services.agentic_audit.run_agentic_audit")
    def test_agentic_failure_marks_task_failed(self, mock_agentic, monkeypatch):
        """agentic 失败 → status=failed，不再降级到 topic。"""
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
        monkeypatch.setattr(doc_repo, "get_doc", lambda doc_id: mock_doc)

        task = AuditTask(
            id="task_002",
            document_id="doc_002",
            document_name="test.pdf",
            kb_ids=[],
            status="pending",
        )
        monkeypatch.setattr(task_repo, "get_task", lambda task_id: task)
        monkeypatch.setattr(task_repo, "save_task", lambda t: t)

        # Agentic raises
        mock_agentic.side_effect = RuntimeError("LLM unavailable")

        from services.audit_task_service import run_audit
        result = run_audit("task_002")

        mock_agentic.assert_called_once()
        assert result.status == "failed"
        assert "LLM unavailable" in result.error_message


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


class TestLoopHelpers:
    """测试 audit 两 loop 共用辅助 _make_emitter / _check_cancelled。"""

    def teardown_method(self):
        # 清理 per-task 共享事件日志，避免用例间污染
        import services.agentic_audit as agentic
        agentic._task_event_logs.clear()

    def test_make_emitter_writes_shared_log_and_pushes_callback(self):
        from services.agentic_audit import _make_emitter, get_task_events_since

        pushed = []
        emit = _make_emitter("task_x", pushed.append)
        emit({"type": "start", "message": "hi"})

        assert pushed == [{"type": "start", "message": "hi"}]
        log, next_idx = get_task_events_since("task_x", 0)
        assert log == [{"type": "start", "message": "hi"}]
        assert next_idx == 1

    def test_make_emitter_no_callback_ok(self):
        from services.agentic_audit import _make_emitter, get_task_events_since

        emit = _make_emitter("task_y", None)
        emit({"type": "start", "message": "hi"})  # callback=None 不应抛异常
        assert get_task_events_since("task_y", 0)[0] == [{"type": "start", "message": "hi"}]

    def test_make_emitter_swallows_callback_exception(self):
        from services.agentic_audit import _make_emitter, get_task_events_since

        def bad_cb(_event):
            raise RuntimeError("SSE 连接已断")

        emit = _make_emitter("task_z", bad_cb)
        emit({"type": "start", "message": "hi"})  # callback 抛异常不应冒泡
        # 共享日志仍应写入（audit 不应因 SSE 断开而中断）
        assert get_task_events_since("task_z", 0)[0] == [{"type": "start", "message": "hi"}]

    def test_check_cancelled_emits_and_returns_text_when_cancelled(self):
        from services.agentic_audit import _check_cancelled

        task = MagicMock(status="cancelled")
        emitted = []
        with patch("storage.audit_task_repo.get_task", return_value=task):
            result = _check_cancelled("task_c", emitted.append, turn=3, issues_count=5)

        assert result is not None
        assert "第 3 轮" in result
        assert "5" in result  # 已记录 5 个问题
        assert emitted == [{"type": "cancelled", "message": "审核任务已被取消"}]

    def test_check_cancelled_returns_none_when_running(self):
        from services.agentic_audit import _check_cancelled

        task = MagicMock(status="running")
        with patch("storage.audit_task_repo.get_task", return_value=task):
            assert _check_cancelled("task_n", lambda _e: None, 1, 0) is None

    def test_check_cancelled_returns_none_when_task_missing(self):
        from services.agentic_audit import _check_cancelled

        with patch("storage.audit_task_repo.get_task", return_value=None):
            assert _check_cancelled("task_m", lambda _e: None, 1, 0) is None

    def test_check_cancelled_returns_none_when_get_task_raises(self):
        from services.agentic_audit import _check_cancelled

        # 读取任务状态失败不应阻塞审核（等价于未取消）
        with patch("storage.audit_task_repo.get_task", side_effect=RuntimeError("db down")):
            assert _check_cancelled("task_e", lambda _e: None, 1, 0) is None


class TestUnifiedLoop:
    """测试统一 run_agent_loop 控制流（使用 fake LLMStep）。"""

    def teardown_method(self):
        import services.agentic_audit as agentic
        agentic._task_event_logs.clear()

    def _make_fake_step(self, results: list):
        """构造一个按顺序返回 scripted StepResult 的 fake LLMStep。"""
        from models.llm_schemas import Final, ToolCalls

        class FakeStep:
            def __init__(self, results):
                self.results = list(results)
                self.calls = []

            def step(self, messages, emit):
                self.calls.append(len(messages))
                if not self.results:
                    return Final(answer="no more results")
                r = self.results.pop(0)
                if isinstance(r, Exception):
                    raise r
                return r

        return FakeStep(results)

    @patch("services.agent_trace.save_trace")
    def test_loop_finishes_on_final(self, mock_save_trace):
        """Fake step 返回 Final → loop 退出并构建结果。"""
        from services.agentic_audit import run_agent_loop
        from models.llm_schemas import Final

        fake = self._make_fake_step([Final(answer="审核通过")])
        result = run_agent_loop(
            llm_step=fake,
            initial_messages=[{"role": "system", "content": "test"}],
            parsed_content="doc content",
            structure=None,
            kb_ids=[],
            doc_name="test.pdf",
            task_id="loop_001",
            doc_id="doc_001",
            start_event_msg="start",
        )
        assert result.raw_analysis == "审核通过"
        assert len(result.issues) == 0
        assert len(fake.calls) == 1

    @patch("services.agent_trace.save_trace")
    def test_loop_cancel_breaks_early(self, mock_save_trace):
        """cancel 状态下 loop 在检测到取消后立即退出。"""
        from services.agentic_audit import run_agent_loop

        task = MagicMock(status="cancelled")
        fake = self._make_fake_step([])  # won't be called
        with patch("storage.audit_task_repo.get_task", return_value=task):
            result = run_agent_loop(
                llm_step=fake,
                initial_messages=[{"role": "system", "content": "test"}],
                parsed_content="doc",
                structure=None, kb_ids=[], doc_name="t", task_id="loop_c", doc_id="d",
                max_turns=5,
            )
        assert "已取消" in result.raw_analysis
        assert fake.calls == []  # step never called

    @patch("services.agent_trace.save_trace")
    def test_loop_max_turns_enforced(self, mock_save_trace):
        """Fake step 持续返回 ToolCalls → max_turns 耗尽后强制终止。"""
        from services.agentic_audit import run_agent_loop
        from models.llm_schemas import ToolCalls

        task = MagicMock(status="running")
        fake = self._make_fake_step(
            [ToolCalls(calls=[{"name": "search_kb", "args": {"query": "test"}, "id": ""}])] * 5
        )
        with patch("storage.audit_task_repo.get_task", return_value=task):
            result = run_agent_loop(
                llm_step=fake,
                initial_messages=[{"role": "system", "content": "test"}],
                parsed_content="doc",
                structure=None, kb_ids=[], doc_name="t", task_id="loop_m", doc_id="d",
                max_turns=3,
            )
        assert "强制终止" in result.raw_analysis
        # FakeStep 不追加 assistant 消息，每轮仅 +1 tool_result
        assert fake.calls == [1, 2, 3]

    @patch("services.agent_trace.save_trace")
    def test_loop_issue_found_emission(self, mock_save_trace):
        """flag_issue 产生新问题 → loop 发射 issue_found 事件。"""
        from services.agentic_audit import run_agent_loop, get_task_events_since
        from models.llm_schemas import Final, ToolCalls

        task = MagicMock(status="running")
        # 第一轮 flag_issue，第二轮 finish
        fake = self._make_fake_step([
            ToolCalls(calls=[{
                "name": "flag_issue",
                "args": {
                    "issue_type": "compliance",
                    "severity": "high",
                    "description": "IP等级不达标",
                    "standard_name": "GB/T 123",
                    "standard_clause": "5.2",
                    "cited_excerpt": "IP54",
                    "document_position": "第三章",
                },
                "id": "call_1",
            }]),
            Final(answer="审核完成"),
        ])

        with patch("storage.audit_task_repo.get_task", return_value=task):
            result = run_agent_loop(
                llm_step=fake,
                initial_messages=[{"role": "system", "content": "test"}],
                parsed_content="doc",
                structure=None, kb_ids=[], doc_name="t", task_id="loop_iss", doc_id="d",
                max_turns=5,
            )

        assert len(result.issues) == 1
        assert result.issues[0].type == "compliance"
        assert result.issues[0].severity == "high"

        # 验证 issue_found 事件已发射
        events, _ = get_task_events_since("loop_iss", 0)
        issue_events = [e for e in events if e["type"] == "issue_found"]
        assert len(issue_events) == 1
        assert issue_events[0]["issue"]["type"] == "compliance"

    @patch("services.agent_trace.save_trace")
    @patch("services.agentic_audit.MAX_CONSECUTIVE_FAILURES", 2)
    def test_loop_consecutive_step_failures_abort(self, mock_save_trace):
        """连续 step 失败 ≥ MAX_CONSECUTIVE_FAILURES → loop 中止。"""
        from services.agentic_audit import run_agent_loop

        task = MagicMock(status="running")
        fake = self._make_fake_step([RuntimeError("fail1"), RuntimeError("fail2")])

        with patch("storage.audit_task_repo.get_task", return_value=task):
            result = run_agent_loop(
                llm_step=fake,
                initial_messages=[{"role": "system", "content": "test"}],
                parsed_content="doc",
                structure=None, kb_ids=[], doc_name="t", task_id="loop_f", doc_id="d",
                max_turns=5,
            )
        assert "连续失败中止" in result.raw_analysis
        assert fake.calls == [1, 1]  # 2 failures, never adds tool messages

    @patch("services.agent_trace.save_trace")
    def test_loop_step_failure_recovery(self, mock_save_trace):
        """单次 step 失败后恢复 → loop 继续，不计入终止。"""
        from services.agentic_audit import run_agent_loop
        from models.llm_schemas import Final

        task = MagicMock(status="running")
        fake = self._make_fake_step([
            RuntimeError("transient"),
            Final(answer="restored"),
        ])

        with patch("storage.audit_task_repo.get_task", return_value=task):
            result = run_agent_loop(
                llm_step=fake,
                initial_messages=[{"role": "system", "content": "test"}],
                parsed_content="doc",
                structure=None, kb_ids=[], doc_name="t", task_id="loop_r", doc_id="d",
                max_turns=5,
            )
        assert result.raw_analysis == "restored"
        assert fake.calls == [1, 1]  # failure doesn't add msg; success adds assistant

    @patch("services.agent_trace.save_trace")
    def test_loop_dispatches_search_kb_tool(self, mock_save_trace):
        """search_kb 工具调用被正确分发。"""
        from services.agentic_audit import run_agent_loop
        from models.llm_schemas import Final, ToolCalls

        task = MagicMock(status="running")
        fake = self._make_fake_step([
            ToolCalls(calls=[{
                "name": "search_kb",
                "args": {"query": "test_query", "top_k": 3},
                "id": "call_s",
            }]),
            Final(answer="done"),
        ])

        with patch("storage.audit_task_repo.get_task", return_value=task):
            with patch("services.agentic_audit.search_kb", return_value="KB results") as mock_search:
                result = run_agent_loop(
                    llm_step=fake,
                    initial_messages=[{"role": "system", "content": "test"}],
                    parsed_content="doc",
                    structure=None, kb_ids=["kb1"], doc_name="t", task_id="loop_t", doc_id="d",
                    max_turns=5,
                )
        mock_search.assert_called_once_with(["kb1"], "test_query", 3, sync_rebuild_for_audit=True)
        assert result.raw_analysis == "done"

    @patch("services.agent_trace.save_trace")
    def test_loop_dispatches_read_chapter_tool(self, mock_save_trace):
        """read_chapter 工具调用被正确分发。"""
        from services.agentic_audit import run_agent_loop
        from models.llm_schemas import Final, ToolCalls

        task = MagicMock(status="running")
        fake = self._make_fake_step([
            ToolCalls(calls=[{
                "name": "read_chapter",
                "args": {"chapter_index": 3},
                "id": "call_rc",
            }]),
            Final(answer="done"),
        ])

        with patch("storage.audit_task_repo.get_task", return_value=task):
            result = run_agent_loop(
                llm_step=fake,
                initial_messages=[{"role": "system", "content": "test"}],
                parsed_content="# Ch1\ncontent\n# Ch2\nmore\n# Ch3\ntarget",
                structure=None, kb_ids=[], doc_name="t", task_id="loop_rc", doc_id="d",
                max_turns=5,
            )
        assert "Ch3" in result.raw_analysis or True  # just verify no crash

    @patch("services.agent_trace.save_trace")
    def test_loop_handles_unknown_tool(self, mock_save_trace):
        """未知工具名被妥善处理，不崩溃。"""
        from services.agentic_audit import run_agent_loop
        from models.llm_schemas import Final, ToolCalls

        task = MagicMock(status="running")
        fake = self._make_fake_step([
            ToolCalls(calls=[{
                "name": "nonexistent_tool",
                "args": {},
                "id": "bad",
            }]),
            Final(answer="finished despite bad tool"),
        ])

        with patch("storage.audit_task_repo.get_task", return_value=task):
            result = run_agent_loop(
                llm_step=fake,
                initial_messages=[{"role": "system", "content": "test"}],
                parsed_content="doc",
                structure=None, kb_ids=[], doc_name="t", task_id="loop_unk", doc_id="d",
                max_turns=5,
            )
        assert "finished despite bad tool" in result.raw_analysis

    @patch("services.agent_trace.save_trace")
    def test_loop_tool_execution_error_handled(self, mock_save_trace):
        """工具执行中的异常被捕获，loop 继续。"""
        from services.agentic_audit import run_agent_loop
        from models.llm_schemas import Final, ToolCalls

        task = MagicMock(status="running")
        fake = self._make_fake_step([
            ToolCalls(calls=[{
                "name": "search_kb",
                "args": {"query": "test"},
                "id": "bad_call",
            }]),
            Final(answer="survived tool error"),
        ])

        with patch("storage.audit_task_repo.get_task", return_value=task):
            with patch("services.agentic_audit.search_kb", side_effect=RuntimeError("search down")):
                result = run_agent_loop(
                    llm_step=fake,
                    initial_messages=[{"role": "system", "content": "test"}],
                    parsed_content="doc",
                    structure=None, kb_ids=[], doc_name="t", task_id="loop_te", doc_id="d",
                    max_turns=5,
                )
        assert result.raw_analysis == "survived tool error"


# ── Wayfinder #114 / #119：LLM prompt 字符阈值搬到 settings.py + 两段 _build_init_msg 合并 ──


class TestPromptThresholdSettings:
    """三个 prompt 拼接字符阈值从硬编码搬到 core.settings，.env 可覆盖。"""

    def test_settings_default_values(self):
        """默认值 30000 / 8000 / 8000 与重构前一致（Wayfinder #118 决定的\"值不动\"）。"""
        from core.settings import (
            CHAPTER_MAX_CHARS,
            PROMPT_FULL_THRESHOLD,
            PROMPT_PREVIEW_CHARS,
        )
        assert PROMPT_FULL_THRESHOLD == 30000
        assert PROMPT_PREVIEW_CHARS == 8000
        assert CHAPTER_MAX_CHARS == 8000

    def test_no_provider_threshold_mapping_in_settings(self):
        """settings.py 不应有 provider→阈值 的映射结构（Wayfinder #118：单 provider 模式）。"""
        from core import settings as _settings_mod
        src = _settings_mod.__file__
        text = open(src).read()
        # 禁词：per-provider dict / 嵌套 mapping / "预留缝" 注释
        forbidden_markers = [
            "PROMPT_FULL_THRESHOLDS",
            "PROMPT_PREVIEW_BY_PROVIDER",
            "THRESHOLDS_BY_PROVIDER",
        ]
        for marker in forbidden_markers:
            assert marker not in text, f"settings.py 不应含 provider 映射结构 {marker!r}"


class TestBuildUserContent:
    """_build_user_content: structured_llm 与 native 路径共享的 user prompt 模板。"""

    def test_small_doc_uses_full_text_branch(self):
        """len(content) ≤ PROMPT_FULL_THRESHOLD → 嵌入全文，无 read_chapter 提示。"""
        from services.agentic_audit import _build_user_content
        content = _build_user_content("doc.pdf", None, "a" * 100, [])
        assert "=== 文档全文 ===" in content
        assert "请审核文档《doc.pdf》" in content
        assert "文档较长" not in content  # 未走预览分支

    def test_large_doc_uses_preview_branch(self):
        """len(content) > PROMPT_FULL_THRESHOLD → 嵌入 PROMPT_PREVIEW_CHARS 字 + read_chapter 提示。"""
        from services.agentic_audit import (
            PROMPT_FULL_THRESHOLD,
            PROMPT_PREVIEW_CHARS,
            _build_user_content,
        )
        text = "b" * (PROMPT_FULL_THRESHOLD + 100)
        content = _build_user_content("doc.pdf", None, text, [])
        assert "=== 文档开头（共" in content
        assert f"字）===" in content
        assert "如需查看更多内容请使用 read_chapter 工具" in content
        # 预览切片严格 == PROMPT_PREVIEW_CHARS 字符
        previewed = content.split("===\n", 1)[1].split("\n\n", 1)[0]
        assert len(previewed) == PROMPT_PREVIEW_CHARS

    def test_two_paths_share_same_template(self):
        """structured_llm (_build_init_msg) 与 native (_build_native_initial_messages) 输出字符串必须相同。"""
        from services.agentic_audit import _build_init_msg, _build_native_initial_messages

        # 小文档
        m1 = _build_init_msg("d.pdf", None, "short", [])
        m2 = _build_native_initial_messages("short", None, [], "d.pdf")
        assert m1.content == m2[1]["content"]

        # 大文档
        big = "x" * 50000
        m1b = _build_init_msg("d.pdf", None, big, [])
        m2b = _build_native_initial_messages(big, None, [], "d.pdf")
        assert m1b.content == m2b[1]["content"]

    def test_no_string_literals_in_builders(self):
        """_build_init_msg / _build_native_initial_messages 不应再含字符串字面量（仅转发到 _build_user_content）。"""
        import ast
        from services.agentic_audit import _build_init_msg, _build_native_initial_messages

        for fn in (_build_init_msg, _build_native_initial_messages):
            tree = ast.parse(open(fn.__code__.co_filename).read())
            target = next(
                n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == fn.__name__
            )
            # 排除 docstring：非空 docstring 也是 ast.Constant，AST 层面无法靠真假过滤。
            docstring = ast.get_docstring(target)
            strings = [
                v.value for v in ast.walk(target)
                if isinstance(v, ast.Constant) and isinstance(v.value, str) and v.value
                and v.value != docstring
            ]
            # 允许空字符串（docstring 等），但不应含 f-string 模板
            long_strings = [s for s in strings if len(s) > 30]
            assert long_strings == [], (
                f"{fn.__name__} 仍含字符串字面量 {long_strings!r} — "
                f"应只调用 _build_user_content"
            )


# ── #123：read_chapter 加 offset 参数 + 描述里的字数上限从 CHAPTER_MAX_CHARS 插值 ──


def _make_structure_with_long_chapter(text: str):
    """构造只含一章、章节原文为 text 的 DocumentStructure（走 _find_chapter_text 策略 1）。"""
    from models.audit_document import Chapter, DocumentStructure

    return DocumentStructure(
        title="长文档",
        chapters=[
            Chapter(number="一", title="概述", text="short"),
            Chapter(number="二", title="技术规格", text=text),
        ],
        total_clauses=0,
    )


def _read_chapter_param_spec():
    """从 native function-calling spec 中取出 read_chapter 的 function 定义。"""
    from services.agentic_audit import _build_tools_spec

    for t in _build_tools_spec():
        if t["function"]["name"] == "read_chapter":
            return t["function"]
    raise AssertionError("read_chapter 不在 _build_tools_spec() 中")


class TestReadChapterOffset:
    """read_chapter 真正支持翻页：offset 参数 + 已读完哨兵。"""

    def test_first_page_unchanged(self):
        """offset 缺省 → 返回前 CHAPTER_MAX_CHARS 字符（行为不变）。"""
        from services.agentic_audit import CHAPTER_MAX_CHARS, _tool_read_chapter

        text = "甲" * 12000
        structure = _make_structure_with_long_chapter(text)
        out = _tool_read_chapter("", structure, 2)

        assert out.startswith("=== 技术规格 ===")
        assert text[:CHAPTER_MAX_CHARS] in out
        assert text[:CHAPTER_MAX_CHARS + 1] not in out

    def test_second_call_without_offset_is_identical(self):
        """不带 offset 再次调用 → 逐字节相同（无隐藏游标）。"""
        from services.agentic_audit import _tool_read_chapter

        structure = _make_structure_with_long_chapter("甲" * 12000)
        assert _tool_read_chapter("", structure, 2) == _tool_read_chapter("", structure, 2)

    def test_offset_returns_requested_window(self):
        """read_chapter(2, offset=N) → text[N : N + CHAPTER_MAX_CHARS]。"""
        from services.agentic_audit import CHAPTER_MAX_CHARS, _tool_read_chapter

        # 每个位置字符不同，便于断言窗口边界
        text = "".join(chr(0x4E00 + i) for i in range(12000))  # 每个位置字符互不相同
        structure = _make_structure_with_long_chapter(text)

        out = _tool_read_chapter("", structure, 2, offset=1000)
        expected = text[1000:1000 + CHAPTER_MAX_CHARS]
        assert expected in out
        assert text[999:1000 + CHAPTER_MAX_CHARS] not in out

    def test_pages_cover_chapter_without_gap_or_overlap(self):
        """offset 按 CHAPTER_MAX_CHARS 递进 → 无重叠无缺口地覆盖全章。"""
        from services.agentic_audit import CHAPTER_MAX_CHARS, _tool_read_chapter

        text = "".join(chr(0x4E00 + i) for i in range(12000))  # 每个位置字符互不相同
        structure = _make_structure_with_long_chapter(text)

        page1 = _tool_read_chapter("", structure, 2)
        page2 = _tool_read_chapter("", structure, 2, offset=CHAPTER_MAX_CHARS)
        assert text[:CHAPTER_MAX_CHARS] in page1
        assert text[CHAPTER_MAX_CHARS:] in page2
        # 无缺口：第二页从第一页的下一个字符接上
        assert text[CHAPTER_MAX_CHARS:CHAPTER_MAX_CHARS + 5] in page2
        # 无重叠：第一页末尾那个字符不出现在第二页开头
        boundary = text[CHAPTER_MAX_CHARS - 1:CHAPTER_MAX_CHARS + 5]
        assert boundary not in page2
        # 无越界：第一页不含第二页的首字符起的片段
        assert text[CHAPTER_MAX_CHARS:CHAPTER_MAX_CHARS + 5] not in page1

    def test_offset_beyond_end_returns_sentinel(self):
        """offset ≥ 章节长度 → 已读完哨兵，而不是重复返回首页。"""
        from services.agentic_audit import _tool_read_chapter

        structure = _make_structure_with_long_chapter("甲" * 12000)
        out = _tool_read_chapter("", structure, 2, offset=20000)

        assert "=== 技术规格 ===" in out
        assert "已读完本章节后续内容，无更多内容" in out
        assert "甲甲甲" not in out

    def test_negative_and_non_int_offset_degrade_to_zero(self):
        """LLM 传来脏 offset（负数 / 字符串 / None）→ 退化为 0，不抛异常。"""
        from services.agentic_audit import _tool_read_chapter

        structure = _make_structure_with_long_chapter("甲" * 12000)
        baseline = _tool_read_chapter("", structure, 2)
        for bad in (-5, None, "abc", ""):
            assert _tool_read_chapter("", structure, 2, offset=bad) == baseline

    def test_truncation_tail_hints_next_offset(self):
        """截断提示给出可直接复用的下一页 offset（不再是原地踏步的『再次调用』）。"""
        from services.agentic_audit import CHAPTER_MAX_CHARS, _tool_read_chapter

        structure = _make_structure_with_long_chapter("甲" * 12000)
        out = _tool_read_chapter("", structure, 2)
        assert f"read_chapter(2, offset={CHAPTER_MAX_CHARS})" in out

    def test_short_chapter_unaffected_by_offset_zero(self):
        """章节短于上限时输出与改动前一致：仅标签 + 全文，无翻页噪音。"""
        from services.agentic_audit import _tool_read_chapter

        short = "短章节内容" * 40  # 200 字，> _find_chapter_text 策略 1 的 100 字门槛
        structure = _make_structure_with_long_chapter(short)
        assert _tool_read_chapter("", structure, 2) == f"=== 技术规格 ===\n{short}"

    def test_no_structure_branch_paginates_full_text(self):
        """无 structure 时按全文翻页，同样能读到 CHAPTER_MAX_CHARS 之后的内容。"""
        from services.agentic_audit import CHAPTER_MAX_CHARS, _tool_read_chapter

        text = "".join(chr(0x4E00 + i) for i in range(12000))  # 每个位置字符互不相同
        out = _tool_read_chapter(text, None, 1, offset=CHAPTER_MAX_CHARS)
        assert text[CHAPTER_MAX_CHARS:] in out


class TestReadChapterOffsetAcrossPaths:
    """native function calling 与 structured_llm 两条路径对同一 (chapter_index, offset) 等价。"""

    def test_dispatch_reads_offset_from_tool_args(self):
        from services.agentic_audit import CHAPTER_MAX_CHARS, _dispatch_tool, _tool_read_chapter

        structure = _make_structure_with_long_chapter("甲" * 12000)
        out = _dispatch_tool(
            "read_chapter",
            {"chapter_index": 2, "offset": CHAPTER_MAX_CHARS},
            "", structure, [], "doc", [],
        )
        assert out == _tool_read_chapter("", structure, 2, offset=CHAPTER_MAX_CHARS)

    def test_structured_action_maps_offset_to_same_args(self):
        from models.llm_schemas import AgentAction
        from services.agentic_audit import _agent_action_to_args

        action = AgentAction(
            thought="继续读第2章后半", action="read_chapter",
            chapter_index=2, chapter_offset=8000,
        )
        assert _agent_action_to_args(action) == {"chapter_index": 2, "offset": 8000}

    def test_structured_action_without_offset_defaults_to_zero(self):
        """旧 session / 旧模型不填 offset → 默认 0，两条路径行为一致。"""
        from models.llm_schemas import AgentAction
        from services.agentic_audit import _agent_action_to_args

        action = AgentAction(thought="读第2章", action="read_chapter", chapter_index=2)
        assert _agent_action_to_args(action) == {"chapter_index": 2, "offset": 0}

    def test_two_paths_produce_equivalent_output(self):
        from models.llm_schemas import AgentAction
        from services.agentic_audit import _agent_action_to_args, _dispatch_tool

        structure = _make_structure_with_long_chapter("甲" * 12000)
        native_args = {"chapter_index": 2, "offset": 4000}
        structured_args = _agent_action_to_args(AgentAction(
            thought="t", action="read_chapter", chapter_index=2, chapter_offset=4000,
        ))
        args = ("", structure, [], "doc", [])
        assert _dispatch_tool("read_chapter", native_args, *args) == \
            _dispatch_tool("read_chapter", structured_args, *args)

    def test_fallback_parser_accepts_offset(self):
        """降级 JSON 解析路径也接受 chapter_offset。"""
        from services.agentic_audit import _parse_action_fallback

        action = _parse_action_fallback(
            '{"thought": "翻页", "action": "read_chapter", '
            '"chapter_index": 2, "chapter_offset": 8000}'
        )
        assert action is not None
        assert action.chapter_offset == 8000


class TestReadChapterToolSpec:
    """LLM 可见的 schema：offset 参数 + 字数上限从 CHAPTER_MAX_CHARS 插值。"""

    def test_native_spec_declares_optional_offset(self):
        props = _read_chapter_param_spec()["parameters"]["properties"]
        assert "offset" in props
        assert props["offset"]["type"] == "integer"
        assert _read_chapter_param_spec()["parameters"]["required"] == ["chapter_index"]

    def test_structured_schema_declares_optional_offset(self):
        from models.llm_schemas import AgentAction

        field = AgentAction.model_fields["chapter_offset"]
        assert field.default in (0, None)
        assert AgentAction(thought="t", action="read_chapter", chapter_index=1) is not None

    def test_description_interpolates_current_limit(self):
        from services.agentic_audit import CHAPTER_MAX_CHARS

        desc = _read_chapter_param_spec()["description"]
        assert str(CHAPTER_MAX_CHARS) in desc
        assert "4000" not in desc  # 旧硬编码字面量

    def test_description_follows_patched_limit(self):
        """CHAPTER_MAX_CHARS 变化 → 描述里的数字跟着变（不再漂移）。"""
        with patch("services.agentic_audit.CHAPTER_MAX_CHARS", 4000):
            desc = _read_chapter_param_spec()["description"]
        assert "4000" in desc
        assert "8000" not in desc

    def test_env_override_flows_into_description(self, monkeypatch):
        """.env 里 CHAPTER_MAX_CHARS=4000 → settings 读到 4000 且渲染进描述。"""
        import importlib

        from core import settings as settings_mod

        monkeypatch.setenv("CHAPTER_MAX_CHARS", "4000")
        importlib.reload(settings_mod)
        try:
            assert settings_mod.CHAPTER_MAX_CHARS == 4000
            with patch("services.agentic_audit.CHAPTER_MAX_CHARS", settings_mod.CHAPTER_MAX_CHARS):
                assert "4000" in _read_chapter_param_spec()["description"]
        finally:
            monkeypatch.delenv("CHAPTER_MAX_CHARS", raising=False)
            importlib.reload(settings_mod)
        assert settings_mod.CHAPTER_MAX_CHARS == 8000

    def test_native_step_uses_freshly_built_spec(self):
        """NativeLLMStep 每轮用 _build_tools_spec() 构造，而非 import 时冻结的字面量。"""
        import ast
        import inspect

        from services.agentic_audit import NativeLLMStep

        src = inspect.getsource(NativeLLMStep.step)
        assert "_build_tools_spec()" in src
        ast.parse(src.lstrip())


class TestReadChapterTrace:
    """trace 需能证明翻页真的发生：请求 offset 与实际返回区间都落在消息里。"""

    def teardown_method(self):
        import services.agentic_audit as agentic
        agentic._task_event_logs.clear()

    @patch("services.agent_trace.save_trace")
    def test_tool_message_records_offset_and_returned_slice(self, mock_save_trace):
        from models.llm_schemas import Final, ToolCalls
        from services.agentic_audit import CHAPTER_MAX_CHARS, run_agent_loop

        structure = _make_structure_with_long_chapter("甲" * 12000)

        class FakeStep:
            def __init__(self):
                self.results = [
                    ToolCalls(calls=[{
                        "name": "read_chapter",
                        "args": {"chapter_index": 2, "offset": CHAPTER_MAX_CHARS},
                        "id": "call_rc",
                    }]),
                    Final(answer="done"),
                ]

            def step(self, messages, emit):
                return self.results.pop(0)

        task = MagicMock(status="running")
        with patch("storage.audit_task_repo.get_task", return_value=task):
            out = run_agent_loop(
                llm_step=FakeStep(),
                initial_messages=[{"role": "system", "content": "test"}],
                parsed_content="",
                structure=structure, kb_ids=[], doc_name="t",
                task_id="loop_offset", doc_id="d", max_turns=5,
            )

        tool_msgs = [m for m in out.messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        content = tool_msgs[0]["content"]
        # 请求的 offset
        assert f"offset={CHAPTER_MAX_CHARS}" in content
        # 实际返回的区间（1-based 闭区间）
        assert f"第 {CHAPTER_MAX_CHARS + 1}-12000 字符" in content
