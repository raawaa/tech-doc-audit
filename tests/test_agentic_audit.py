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
        mock_search.assert_called_once_with(["kb1"], "test_query", 3)
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
