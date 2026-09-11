"""``services.bulk_reparse_service`` 契约测试（issue #108 / spec #102 第 1 步）。

本 ticket 是**纯下沉重构**：批量重新解析 (Bulk Reparse) 的领域逻辑
（待重解析文档选取 / OCR 成本预检 / 页数上限分类 / 受控并发编排）从
``scripts/bulk_reparse.py`` 搬进 service，CLI 降级为薄 wrapper。

因此测试盯住两件事：
1. service 自己的可观察行为（三条选取规则各自独立成立、``force``
   绕过、V8 cache defense 计入 uncached、超页数上限进 skipped）。
2. CLI 与 service 在同一 KB 上给出**同一份**目标清单与成本估算
   （spec #102 story 43 —— 防两个入口语义分叉）。

不触碰真实 OCR / 向量索引：``reparse_document`` 被 patch 掉，
只验证编排层（并发、终态收集、逐篇回调）的行为。
"""
from __future__ import annotations

import importlib.util
import json
import logging
from pathlib import Path

import pytest

import storage.doc_repo as doc_repo
import storage.kb_repo as kb_repo
from core import paddleocr_cache, pages_store
from core.settings import PADDLEOCR_PAGE_LIMIT
from models.knowledge_base import KnowledgeBase


# ── fixture：隔离数据目录 + 造 KB 与四类文档 ────────────────────────────────────


@pytest.fixture
def isolated_data_dir(tmp_path):
    """数据目录隔离由 conftest 的 per-test ``AUDIT_DATA_DIR`` 保证（issue #137）。

    存储层 ``get_data_dir()`` 每次调用解析 env，无需再 monkeypatch 模块属性。
    保留此 fixture 只为给测试一个指向本用例数据目录的 Path。
    """
    return tmp_path


@pytest.fixture
def kb(isolated_data_dir):
    """一个空 KB。"""
    return kb_repo.create(KnowledgeBase(id="kb_bulk_reparse", name="批量库", category="national"))


def _add_doc(
    kb_id: str,
    name: str,
    *,
    embedding_status: str = "embedded",
    page_count: int | None = 5,
    content_hash: str | None = None,
    pages: dict | None = None,
):
    """造一篇 doc（可选带 pages 文件）。返回 KBDocument。"""
    doc = doc_repo.save_doc(kb_id, name, b"%PDF-1.4 dummy " + name.encode(), "pdf")
    doc.embedding_status = embedding_status
    doc.page_count = page_count
    doc.content_hash = content_hash
    doc_repo._save_doc_meta(doc)
    if pages is not None:
        pages_store.save_pages(kb_id, doc.id, pages)
    return doc


def _good_pages() -> dict:
    return {
        "by_page": [{"page": 0, "text": "x" * 50}],
        "full_text": "x" * 50,
        "layout": [{"page": 0, "blocks": [{"block_order": 0}]}],
    }


def _write_cache_entry(content_hash: str, *, source: str) -> Path:
    """按 (content_hash, model_version) 写一条缓存条目。"""
    path = (
        paddleocr_cache.get_cache_dir()
        / f"{content_hash}_{paddleocr_cache._MODEL_VERSION}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": paddleocr_cache._MODEL_VERSION, "source": source, "result": {}}),
        encoding="utf-8",
    )
    return path


# ── 待重解析文档选取：三条规则各自独立成立 ──────────────────────────────────────


def test_rule_not_embedded_alone_makes_a_target(kb):
    """规则 1：``embedding_status != embedded`` —— 即便 pages 文件齐全且 layout 非空。"""
    from services import bulk_reparse_service as svc

    doc = _add_doc(kb.id, "unembedded.pdf", embedding_status="failed", pages=_good_pages())

    targets = svc.list_target_docs(kb.id)

    assert [t.doc.id for t in targets] == [doc.id]
    assert targets[0].reason == svc.REASON_NOT_EMBEDDED
    assert targets[0].has_pages_file is True


def test_rule_missing_pages_file_alone_makes_a_target(kb):
    """规则 2：缺 pages 文件 —— 即便已 ``embedded``。"""
    from services import bulk_reparse_service as svc

    doc = _add_doc(kb.id, "nopages.pdf", embedding_status="embedded", pages=None)

    targets = svc.list_target_docs(kb.id)

    assert [t.doc.id for t in targets] == [doc.id]
    assert targets[0].reason == svc.REASON_MISSING_PAGES
    assert targets[0].has_pages_file is False


def test_rule_empty_layout_alone_makes_a_target(kb):
    """规则 3（#93 兜底）：pages 文件存在但 ``layout=[]`` 的"假成功"。"""
    from services import bulk_reparse_service as svc

    doc = _add_doc(
        kb.id, "fakesuccess.pdf",
        embedding_status="embedded",
        pages={"by_page": [{"page": 0, "text": "x" * 50}], "full_text": "x" * 50, "layout": []},
    )

    targets = svc.list_target_docs(kb.id)

    assert [t.doc.id for t in targets] == [doc.id]
    assert targets[0].reason == svc.REASON_EMPTY_LAYOUT


def test_healthy_doc_is_not_a_target(kb):
    """三条规则都不命中 → 不进名单。"""
    from services import bulk_reparse_service as svc

    _add_doc(kb.id, "healthy.pdf", embedding_status="embedded", pages=_good_pages())

    assert svc.list_target_docs(kb.id) == []


def test_force_selects_every_doc_including_healthy_ones(kb):
    """``force=True`` 绕过三条规则，整库都是目标（#99 换解析器后的整库重建入口）。"""
    from services import bulk_reparse_service as svc

    healthy = _add_doc(kb.id, "healthy.pdf", embedding_status="embedded", pages=_good_pages())
    broken = _add_doc(kb.id, "nopages.pdf", embedding_status="embedded", pages=None)

    targets = svc.list_target_docs(kb.id, force=True)

    assert {t.doc.id for t in targets} == {healthy.id, broken.id}
    assert {t.reason for t in targets} == {svc.REASON_FORCED}


def test_missing_kb_yields_no_targets(isolated_data_dir):
    """KB 不存在（无 meta 目录）→ 空名单，不抛。"""
    from services import bulk_reparse_service as svc

    assert svc.list_target_docs("kb_does_not_exist") == []


# ── OCR 成本预检 ───────────────────────────────────────────────────────────────


def test_estimate_counts_cache_hit_as_cached(kb, monkeypatch):
    """有缓存条目（``source=paddleocr``）→ 计 cached，页数不进 OCR 预算。"""
    from services import bulk_reparse_service as svc

    monkeypatch.setenv("PADDLEOCR_API_TOKEN", "tok")
    monkeypatch.setenv("PADDLEOCR_API_URL", "https://ocr.example")
    _add_doc(kb.id, "cached.pdf", embedding_status="failed", page_count=7, content_hash="h_cached")
    _write_cache_entry("h_cached", source="paddleocr")

    cost = svc.estimate_ocr_cost(svc.list_target_docs(kb.id))

    assert (cost.cached, cost.uncached) == (1, 0)
    assert (cost.pages_cached, cost.pages_uncached) == (7, 0)


def test_estimate_treats_legacy_fallback_pdfplumber_entry_as_cached(kb, monkeypatch):
    """#99/05 后 V8 cache defense 已删；历史 ``fallback_pdfplumber`` 条目按命中计。

    运维清理是单独工单 —— 预检乐观"有缓存就当命中"，实测由 #110 收。
    """
    from services import bulk_reparse_service as svc

    monkeypatch.setenv("PADDLEOCR_API_TOKEN", "tok")
    monkeypatch.setenv("PADDLEOCR_API_URL", "https://ocr.example")
    _add_doc(kb.id, "legacy.pdf", embedding_status="failed", page_count=9, content_hash="h_legacy")
    _write_cache_entry("h_legacy", source="fallback_pdfplumber")

    cost = svc.estimate_ocr_cost(svc.list_target_docs(kb.id))

    assert (cost.cached, cost.uncached) == (1, 0)
    assert (cost.pages_cached, cost.pages_uncached) == (9, 0)


def test_estimate_uses_default_pages_when_page_count_missing(kb):
    """无 ``page_count`` 元数据 → 按 ``DEFAULT_PAGES_ESTIMATE`` 估算。"""
    from services import bulk_reparse_service as svc

    _add_doc(kb.id, "unknownpages.pdf", embedding_status="failed", page_count=None)

    cost = svc.estimate_ocr_cost(svc.list_target_docs(kb.id))

    assert cost.uncached == 1
    assert cost.pages_uncached == svc.DEFAULT_PAGES_ESTIMATE


def test_estimate_counts_corrupt_cache_entry_as_uncached(kb):
    """缓存条目损坏 → ``get_cached`` 会重解析，估算必须同口径计 uncached。"""
    from services import bulk_reparse_service as svc

    _add_doc(kb.id, "corrupt.pdf", embedding_status="failed", page_count=6, content_hash="h_bad")
    path = paddleocr_cache.get_cache_dir() / f"h_bad_{paddleocr_cache._MODEL_VERSION}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    cost = svc.estimate_ocr_cost(svc.list_target_docs(kb.id))

    assert (cost.cached, cost.uncached) == (0, 1)
    assert cost.pages_uncached == 6


# ── 页数上限分类 ───────────────────────────────────────────────────────────────


def test_over_cost_limit_doc_lands_in_warnings(kb):
    """超拆分成本阈值 → 进 ``cost_exceeded_docs`` 清单；pages_uncached 不再被 cap。"""
    from services import bulk_reparse_service as svc
    from core.settings import BULK_REPARSE_SPLIT_COST_LIMIT_PAGES

    doc = _add_doc(
        kb.id, "huge.pdf", embedding_status="failed",
        page_count=BULK_REPARSE_SPLIT_COST_LIMIT_PAGES + 1,
    )

    cost = svc.estimate_ocr_cost(svc.list_target_docs(kb.id))

    assert [over.doc.id for over in cost.cost_exceeded_docs] == [doc.id]
    assert cost.cost_exceeded_docs[0].page_count == BULK_REPARSE_SPLIT_COST_LIMIT_PAGES + 1
    assert cost.cost_exceeded_docs[0].reason == svc.SKIP_REASON_COST_EXCEEDED


def test_pages_uncached_is_not_capped_to_split_trigger_anymore(kb):
    """#181 验收 #4：``pages_uncached`` 不再被 cap 在 ``PADDLEOCR_PAGE_LIMIT``。

    旧实现把超限 doc 按 ``PADDLEOCR_PAGE_LIMIT`` 计费（服务端会截断），导致
    实际页数被低估。新实现按真实预估页数计费，超限 doc 进 ``will_split_docs``、
    进 ``runnable``、走 splitter 拆分路径（不丢失页）。
    """
    from services import bulk_reparse_service as svc

    _add_doc(
        kb.id, "huge.pdf", embedding_status="failed",
        page_count=PADDLEOCR_PAGE_LIMIT + 50,
    )

    cost = svc.estimate_ocr_cost(svc.list_target_docs(kb.id))

    # 真实预估页数（250）全数计入 pages_uncached —— 不再被 cap 到 100。
    assert cost.pages_uncached == PADDLEOCR_PAGE_LIMIT + 50


def test_will_split_docs_uses_pdf_splitter_chunk_ranges(kb):
    """#181 验收：``will_split_docs`` 调 :func:`pdf_splitter.chunk_ranges` 唯一实现。

    不在本服务里写第二份 ``ceil`` 除法 —— splitter 是 chunks_planned 的**唯一**算法来源。
    """
    from services import bulk_reparse_service as svc
    from core.pdf_splitter import chunk_ranges

    doc = _add_doc(
        kb.id, "huge.pdf", embedding_status="failed",
        page_count=PADDLEOCR_PAGE_LIMIT + 47,  # 147 页
    )

    cost = svc.estimate_ocr_cost(svc.list_target_docs(kb.id))

    assert [plan.doc.id for plan in cost.will_split_docs] == [doc.id]
    plan = cost.will_split_docs[0]
    assert plan.page_count == PADDLEOCR_PAGE_LIMIT + 47
    assert plan.chunks_planned == len(chunk_ranges(plan.page_count))
    # 147 / 99 = 2 块
    assert plan.chunks_planned == 2
    # chunks_total / ocr_pages_total 合计
    assert cost.chunks_total == 2
    assert cost.ocr_pages_total == PADDLEOCR_PAGE_LIMIT + 47


def test_split_by_cost_limit_separates_runnable_from_skipped(kb):
    """分类器把"会跑"与"会跳过"分开，跳过项带原因。"""
    from services import bulk_reparse_service as svc
    from core.settings import BULK_REPARSE_SPLIT_COST_LIMIT_PAGES

    small = _add_doc(kb.id, "small.pdf", embedding_status="failed", page_count=10)
    huge = _add_doc(
        kb.id, "huge.pdf", embedding_status="failed",
        page_count=BULK_REPARSE_SPLIT_COST_LIMIT_PAGES + 1,
    )

    runnable, skipped = svc.split_by_cost_limit(svc.list_target_docs(kb.id))

    assert [t.doc.id for t in runnable] == [small.id]
    assert [s.doc.id for s in skipped] == [huge.id]
    assert skipped[0].reason == svc.SKIP_REASON_COST_EXCEEDED
    assert skipped[0].page_count == BULK_REPARSE_SPLIT_COST_LIMIT_PAGES + 1


def test_split_by_cost_limit_ignore_cost_limit_keeps_everyone_runnable(kb):
    """``ignore_cost_limit=True`` → 全员进 runnable（``--force`` 不能绕的成本护栏出口）。"""
    from services import bulk_reparse_service as svc
    from core.settings import BULK_REPARSE_SPLIT_COST_LIMIT_PAGES

    small = _add_doc(kb.id, "small.pdf", embedding_status="failed", page_count=10)
    huge = _add_doc(
        kb.id, "huge.pdf", embedding_status="failed",
        page_count=BULK_REPARSE_SPLIT_COST_LIMIT_PAGES + 1,
    )

    runnable, skipped = svc.split_by_cost_limit(
        svc.list_target_docs(kb.id), ignore_cost_limit=True,
    )

    assert {t.doc.id for t in runnable} == {small.id, huge.id}
    assert skipped == []


def test_list_target_docs_resolves_estimated_page_count_from_pdf_when_missing(kb, monkeypatch):
    """#181 验收 #5：``doc.page_count is None`` + 可读源 PDF → 预检用真实页数而非 ``DEFAULT_PAGES_ESTIMATE``。"""
    from services import bulk_reparse_service as svc

    doc = _add_doc(kb.id, "x.pdf", embedding_status="failed", page_count=None)
    # 让 ``pdf_page_count`` 返回一个固定数
    monkeypatch.setattr(
        "core.parse_document.pdf_page_count", lambda _path: 42,
    )

    targets = svc.list_target_docs(kb.id)
    target = next(t for t in targets if t.doc.id == doc.id)
    assert target.estimated_page_count == 42


def test_will_split_property_uses_split_trigger(kb):
    """``ReparseTarget.will_split`` = ``estimated_page_count > PADDLEOCR_PAGE_LIMIT``。"""
    from services import bulk_reparse_service as svc

    small = _add_doc(kb.id, "small.pdf", embedding_status="failed", page_count=10)
    huge = _add_doc(
        kb.id, "huge.pdf", embedding_status="failed",
        page_count=PADDLEOCR_PAGE_LIMIT + 1,
    )

    targets = {t.doc.id: t for t in svc.list_target_docs(kb.id)}

    assert targets[small.id].will_split is False
    assert targets[huge.id].will_split is True


def test_cost_exceeded_doc_does_not_appear_in_will_split_docs(kb):
    """review #181 双计数回归锁：超拆分成本阈值的 doc 不会跑，**不**应出现在
    ``will_split_docs`` 也不应贡献到 ``chunks_total`` / ``ocr_pages_total``。

    修前 bug：cost-exceeded 分支在 ``continue`` 之前先 append ``will_split_docs``
    并累加 chunks_total / ocr_pages_total。语义上 cost-exceeded doc 跑都不会跑，
    不可能走拆分路径。
    """
    from services import bulk_reparse_service as svc
    from core.settings import BULK_REPARSE_SPLIT_COST_LIMIT_PAGES

    huge = _add_doc(
        kb.id, "huge.pdf", embedding_status="failed",
        page_count=BULK_REPARSE_SPLIT_COST_LIMIT_PAGES + 1,  # 同时超 PADDLEOCR_PAGE_LIMIT + 成本阈值
    )

    cost = svc.estimate_ocr_cost(svc.list_target_docs(kb.id))

    assert [s.doc.id for s in cost.cost_exceeded_docs] == [huge.id]
    # 关键：cost-exceeded doc **不**出现在 will_split_docs 也不贡献 totals
    assert cost.will_split_docs == []
    assert cost.chunks_total == 0
    assert cost.ocr_pages_total == 0


def test_estimated_page_count_falls_back_to_pdf_then_default(kb, monkeypatch):
    """``doc.page_count`` → ``pdf_page_count`` → ``DEFAULT_PAGES_ESTIMATE`` 三段回退。"""
    from services import bulk_reparse_service as svc

    # 三种 doc：无 metadata（fallback to default）/ 有 metadata（用 metadata）/
    # 无 metadata 但源 PDF 可读（fallback to pdf_page_count 真值）
    no_meta = _add_doc(kb.id, "no_meta.pdf", embedding_status="failed", page_count=None)
    explicit = _add_doc(kb.id, "explicit.pdf", embedding_status="failed", page_count=10)
    pdf_only = _add_doc(kb.id, "pdf_only.pdf", embedding_status="failed", page_count=None)

    monkeypatch.setattr(
        "core.parse_document.pdf_page_count",
        lambda path: 42 if "pdf_only" in path else None,
    )

    targets = {t.doc.id: t for t in svc.list_target_docs(kb.id)}

    assert targets[no_meta.id].estimated_page_count == svc.DEFAULT_PAGES_ESTIMATE
    assert targets[pdf_only.id].estimated_page_count == 42
    assert targets[explicit.id].estimated_page_count == 10


# ── 批量编排 ───────────────────────────────────────────────────────────────────


def _stub_reparse(monkeypatch, outcomes: dict[str, str]):
    """把 ``reparse_document`` 替换为"按 doc_id 直接写终态"的桩。

    ``outcomes`` 的值为目标 ``embedding_status``，或 ``"raise"`` 表示抛异常。
    """
    from services import bulk_reparse_service as svc

    monkeypatch.setattr(svc, "_POLL_INTERVAL_S", 0.01)

    def _fake(doc_id: str, **_kwargs):
        outcome = outcomes.get(doc_id, "embedded")
        if outcome == "raise":
            raise RuntimeError("simulated reparse outage")
        doc = doc_repo.find_doc_by_id(doc_id)
        doc.embedding_status = outcome
        doc_repo._save_doc_meta(doc)
        return {"status": "pending_index", "doc_id": doc_id}

    monkeypatch.setattr(svc, "reparse_document", _fake)
    return svc


def test_run_bulk_reparse_collects_done_and_failed(kb, monkeypatch):
    """单篇失败不中断整批；done / failed 计数如实反映。"""
    ok = _add_doc(kb.id, "ok.pdf", embedding_status="failed", page_count=3)
    bad = _add_doc(kb.id, "bad.pdf", embedding_status="failed", page_count=3)
    svc = _stub_reparse(monkeypatch, {ok.id: "embedded", bad.id: "failed"})

    result = svc.run_bulk_reparse(kb.id, svc.list_target_docs(kb.id), concurrency=2)

    assert result.done == [ok.id]
    assert [doc_id for doc_id, _reason in result.failed] == [bad.id]
    assert result.skipped == []
    assert result.total == 2


def test_run_bulk_reparse_reports_raised_exception_as_failure(kb, monkeypatch):
    """``reparse_document`` 抛异常 → 该篇计 failed，异常类型进原因串，其余照跑。"""
    boom = _add_doc(kb.id, "boom.pdf", embedding_status="failed", page_count=3)
    fine = _add_doc(kb.id, "fine.pdf", embedding_status="failed", page_count=3)
    svc = _stub_reparse(monkeypatch, {boom.id: "raise"})

    result = svc.run_bulk_reparse(kb.id, svc.list_target_docs(kb.id), concurrency=2)

    assert result.done == [fine.id]
    failures = dict(result.failed)
    assert "RuntimeError" in failures[boom.id]


def test_run_bulk_reparse_skips_over_cost_limit_without_raising(kb, monkeypatch):
    """超拆分成本阈值的文档进 skipped，不被触发、不抛错。"""
    from core.settings import BULK_REPARSE_SPLIT_COST_LIMIT_PAGES

    huge = _add_doc(kb.id, "huge.pdf", embedding_status="failed", page_count=None)
    small = _add_doc(kb.id, "small.pdf", embedding_status="failed", page_count=3)
    svc = _stub_reparse(monkeypatch, {})
    huge.page_count = BULK_REPARSE_SPLIT_COST_LIMIT_PAGES + 1
    doc_repo._save_doc_meta(huge)

    result = svc.run_bulk_reparse(kb.id, svc.list_target_docs(kb.id), concurrency=1)

    assert [s.doc.id for s in result.skipped] == [huge.id]
    assert result.skipped[0].reason == svc.SKIP_REASON_COST_EXCEEDED
    assert result.done == [small.id]
    # 跳过的那篇状态没被动过
    assert doc_repo.get_doc(kb.id, huge.id).embedding_status == "failed"


def test_run_bulk_reparse_split_eligible_docs_go_to_runnable_not_skipped(kb, monkeypatch):
    """#181 验收 #1：超 ``PADDLEOCR_PAGE_LIMIT`` 的文档**进 runnable** 而非 skipped。

    拆分路径让"超 PADDLEOCR_PAGE_LIMIT"不再是跳过理由 —— 它只是要走分块解析。
    """
    huge = _add_doc(kb.id, "huge.pdf", embedding_status="failed", page_count=None)
    svc = _stub_reparse(monkeypatch, {})
    huge.page_count = PADDLEOCR_PAGE_LIMIT + 50
    doc_repo._save_doc_meta(huge)

    result = svc.run_bulk_reparse(kb.id, svc.list_target_docs(kb.id), concurrency=1)

    # 不进 skipped
    assert result.skipped == []
    # 进 runnable → done 列表里能找到它（被 stub 标 embedded）
    assert huge.id in result.done


def test_run_bulk_reparse_force_does_not_bypass_cost_guard(kb, monkeypatch, caplog):
    """#181 验收 #3 + #8：``force=True`` + 超成本 → 仍进 skipped，整批不 abort，跑完 result.usage 正常。"""
    from core.settings import BULK_REPARSE_SPLIT_COST_LIMIT_PAGES

    huge = _add_doc(kb.id, "huge.pdf", embedding_status="failed", page_count=None)
    small = _add_doc(kb.id, "small.pdf", embedding_status="failed", page_count=3)
    svc = _stub_reparse(monkeypatch, {})
    huge.page_count = BULK_REPARSE_SPLIT_COST_LIMIT_PAGES + 1
    doc_repo._save_doc_meta(huge)

    with caplog.at_level(logging.WARNING):
        result = svc.run_bulk_reparse(
            kb.id, svc.list_target_docs(kb.id, force=True),
            concurrency=1, forced=True,
        )

    # cost-exceeded → skipped +1（huge）；force 不能绕过
    assert [s.doc.id for s in result.skipped] == [huge.id]
    # runnable（小）继续跑完
    assert small.id in result.done
    # failed 列表空
    assert result.failed == []
    # result.usage 正常算出（_aggregate_usage 跑过 empty list 不炸）
    assert result.usage.actual_ocr_pages >= 0
    # WARNING 日志（banner 单一 sink；不再用 print 双发）
    assert any(
        "--force 不能绕过成本护栏" in rec.getMessage() for rec in caplog.records
    )


def test_run_bulk_reparse_ignore_cost_limit_lets_overcost_doc_through(kb, monkeypatch):
    """#181 验收 #3：``ignore_cost_limit=True`` → 超成本 doc 进 runnable（而非 skipped）。"""
    from core.settings import BULK_REPARSE_SPLIT_COST_LIMIT_PAGES

    huge = _add_doc(kb.id, "huge.pdf", embedding_status="failed", page_count=None)
    svc = _stub_reparse(monkeypatch, {})
    huge.page_count = BULK_REPARSE_SPLIT_COST_LIMIT_PAGES + 1
    doc_repo._save_doc_meta(huge)

    result = svc.run_bulk_reparse(
        kb.id, svc.list_target_docs(kb.id),
        concurrency=1, ignore_cost_limit=True,
    )

    assert result.skipped == []
    assert huge.id in result.done


def test_run_bulk_reparse_invokes_reap_scratch_at_entry(kb, monkeypatch):
    """#181 验收 #7：``run_bulk_reparse`` 入口至少调一次 :func:`pdf_splitter.reap_scratch`。"""
    from services import bulk_reparse_service as svc

    _add_doc(kb.id, "a.pdf", embedding_status="failed", page_count=3)
    _stub_reparse(monkeypatch, {})

    reaped: list[int] = []
    real_reap = svc.reap_scratch

    def _spy_reap():
        reaped.append(1)
        return real_reap()

    monkeypatch.setattr(svc, "reap_scratch", _spy_reap)

    svc.run_bulk_reparse(kb.id, svc.list_target_docs(kb.id), concurrency=1)

    assert len(reaped) >= 1, f"入口应至少调一次 reap_scratch，实际 {len(reaped)} 次"


def test_run_bulk_reparse_invokes_progress_callback_per_doc(kb, monkeypatch):
    """逐篇回调让 CLI（以及后续的 API 进度）能渲染 ``[n/total]`` 行。"""
    a = _add_doc(kb.id, "a.pdf", embedding_status="failed", page_count=3)
    b = _add_doc(kb.id, "b.pdf", embedding_status="failed", page_count=3)
    svc = _stub_reparse(monkeypatch, {})

    seen = []
    svc.run_bulk_reparse(
        kb.id, svc.list_target_docs(kb.id), concurrency=1,
        on_doc_complete=lambda completed, total, doc, outcome: seen.append(
            (completed, total, doc.id, outcome)
        ),
    )

    assert [s[0] for s in seen] == [1, 2]
    assert {s[1] for s in seen} == {2}
    assert {s[2] for s in seen} == {a.id, b.id}
    assert {s[3] for s in seen} == {"embedded"}


def test_reparse_one_times_out_without_terminal_status(kb, monkeypatch):
    """轮询超时 → outcome=``timeout``（不挂死整批）。"""
    from services import bulk_reparse_service as svc

    doc = _add_doc(kb.id, "stuck.pdf", embedding_status="failed", page_count=3)
    monkeypatch.setattr(svc, "_POLL_INTERVAL_S", 0.01)

    def _fake(doc_id: str, **_kwargs):
        stuck = doc_repo.find_doc_by_id(doc_id)
        stuck.embedding_status = "indexing"  # 永不进终态
        doc_repo._save_doc_meta(stuck)
        return {"status": "pending_index", "doc_id": doc_id}

    monkeypatch.setattr(svc, "reparse_document", _fake)

    doc_id, outcome = svc.reparse_one(kb.id, doc, timeout_s=0.05)

    assert (doc_id, outcome) == (doc.id, "timeout")


# ── ``embedding_status="truncated"`` 进入终态机（spec #173 / issue #175）─────────


def test_terminal_statuses_includes_truncated():
    """``_TERMINAL_STATUSES`` 含 ``truncated``（验收 #3）—— 否则 ``_wait_for_terminal``
    会把它当作"非终态"继续轮询,白白等满 30 分钟超时。
    """
    from services import bulk_reparse_service as svc

    assert "truncated" in svc._TERMINAL_STATUSES
    # 老成员一个都不丢
    assert {"embedded", "failed", "none"} <= svc._TERMINAL_STATUSES


def test_reparse_one_returns_truncated_when_doc_marked_truncated(kb, monkeypatch):
    """验收 #4：把一篇 doc 标 ``truncated`` 然后调 ``reparse_one``，``_wait_for_terminal``
    返回 ``"truncated"`` 而非 ``"timeout"``。
    """
    from services import bulk_reparse_service as svc

    doc = _add_doc(kb.id, "truncated.pdf", embedding_status="failed", page_count=3)
    monkeypatch.setattr(svc, "_POLL_INTERVAL_S", 0.01)

    def _fake(doc_id: str, **_kwargs):
        # 模拟 ``repair_truncated.py`` 的副作用:把 doc 标 truncated。
        # 走 ``mark_doc_embedding_truncated`` 公开入口 —— 与 T07 脚本同款姿势，
        # 让"谁在写 truncated"在测试里也走唯一公开入口，不留私货。
        d = doc_repo.find_doc_by_id(doc_id)
        doc_repo.mark_doc_embedding_truncated(d.kb_id, doc_id)
        return {"status": "pending_index", "doc_id": doc_id}

    monkeypatch.setattr(svc, "reparse_document", _fake)

    doc_id, outcome = svc.reparse_one(kb.id, doc, timeout_s=5.0)

    assert (doc_id, outcome) == (doc.id, "truncated")


def test_list_target_docs_picks_up_truncated_docs(kb):
    """验收 #5：``list_target_docs`` 把 ``truncated`` doc 收进 target 集 —— 走规则 1
    （``embedding_status != "embedded"``），三条选取规则本身不需显式扩充。

    这正是 spec #175 把 truncated 与 failed / pending_index 同列为"待修"的
    用意：规则 1 已经把它们一网打尽。
    """
    from services import bulk_reparse_service as svc

    truncated = _add_doc(kb.id, "truncated.pdf", embedding_status="truncated", pages=_good_pages())
    _add_doc(kb.id, "healthy.pdf", embedding_status="embedded", pages=_good_pages())

    targets = svc.list_target_docs(kb.id)

    assert [t.doc.id for t in targets] == [truncated.id]
    assert targets[0].reason == svc.REASON_NOT_EMBEDDED


def test_list_target_docs_mixes_truncated_with_other_unembedded(kb):
    """truncated 与 failed / pending_index 共存 → 全部按规则 1 入选,
    按文档遍历顺序保留。"""
    from services import bulk_reparse_service as svc

    failed = _add_doc(kb.id, "failed.pdf", embedding_status="failed", pages=_good_pages())
    truncated = _add_doc(kb.id, "truncated.pdf", embedding_status="truncated", pages=_good_pages())
    pending = _add_doc(kb.id, "pending.pdf", embedding_status="pending_index", pages=_good_pages())
    _add_doc(kb.id, "healthy.pdf", embedding_status="embedded", pages=_good_pages())

    targets = svc.list_target_docs(kb.id)

    assert {t.doc.id for t in targets} == {failed.id, truncated.id, pending.id}
    assert {t.reason for t in targets} == {svc.REASON_NOT_EMBEDDED}


# ── KB 级检索状态稳定性（issue #109 / #147 / #154）─────────────────────────────
#
# 批量跑到一半时 KB **不可能**"此刻可被向量检索"。#93 实测下每完成一篇就写回
# searchable，154 篇 = 上百次 building ⇄ searchable 抖动，前端轮询以
# ``index_status === 'building'`` 为唯一续订条件，于是反复停轮询又重启。
# 编排层现在是 KB 级状态的**唯一写入者**（``KbIndexStatusWriter``，
# issue #147 / #148）：批次开头写一次 building，期间只推进
# ``index_progress / index_current_doc``，末尾写一次终态。
#
# 契约的取证层在 #154 从"``kb_repo.update`` 写入序列"上移到
# "``KbIndexStatusWriter`` 接口调用序列"：前者既冗余（KB 状态本就是
# writer 写的）又给旁路留缝（任何直接 ``kb_repo.update`` 的代码都能让
# spy 看起来正常），后者把契约绑在概念层，旁路自然露馅。


def _spy_kb_writer(monkeypatch):
    """记录 ``KbIndexStatusWriter`` 的批量编排相关 callback 调用，按时间顺序。

    本 spy 只裹 ``run_bulk_reparse`` 实际触发的 4 个 callback：

    - ``begin()`` —— 批次头一次调用，落 ``building`` + ``progress=0``；
    - ``note_in_flight(name)`` / ``advance(done)`` —— 期间唯一允许的推进；
    - ``finish(failed=...)`` / ``finish(interrupted=...)`` —— 末尾各调一次，
      终态只在它里面写。

    不裹 ``fail_doc`` / ``clear_building``：前者只在 ``total == 1`` 的单篇
    writer 路径上做事，与批量编排语义无关；后者是运维解卡按钮，不在批量
    自动流程里。需要那两条路径的测试应放在 ``test_kb_index_status.py``
    的 writer 单测里，不该混到本文件。

    "整批期间只在首尾各写一次终态"契约改在 **writer 接口序列** 上锁
    （issue #147 / #154）：KB 状态字段的唯一写入者是 ``KbIndexStatusWriter``，
    旧 ``_spy_kb_writes`` 绑在 ``kb_repo.update`` 上既冗余又给旁路留缝。

    调用通过 ``real(self, ...)`` 转发，KB 真实状态仍会更新；断言"最终 KB 长
    什么样" 与"调用序列长什么样" 同源，不会因 spy 改写而走偏。
    """
    from core.kb_index_status import KbIndexStatusWriter

    calls: list[tuple[str, tuple, dict]] = []

    def _wrap(name: str):
        real = getattr(KbIndexStatusWriter, name)

        def _spy(self, *args, **kwargs):
            calls.append((name, args, kwargs))
            return real(self, *args, **kwargs)

        return _spy

    for name in ("begin", "note_in_flight", "advance", "finish"):
        monkeypatch.setattr(KbIndexStatusWriter, name, _wrap(name))
    return calls


def _spy_kb_repo_updates(monkeypatch):
    """记录所有经 ``kb_repo.update`` 落盘的 KB，按时间顺序。

    与 ``_spy_kb_writer`` 配对使用：组合断言 "writer 调用次数 == update
    调用次数" 即可锁死"任何 KB 状态写入都只能经 writer 触发"——旁路写路径
    会让两边对不上，立刻可见。
    """
    updates: list = []
    real_update = kb_repo.update

    def _spy(kb):
        updates.append(kb)
        return real_update(kb)

    monkeypatch.setattr(kb_repo, "update", _spy)
    return updates


def _stub_reparse_observing_kb(monkeypatch, kb_id, outcomes: dict[str, str] | None = None):
    """``_stub_reparse`` 的加强版：每次被调用时顺手快照 KB 状态与入参。

    返回 ``(svc, observed, calls)``；``observed`` 是"某篇正在跑的那一刻"
    KB 对外宣称的状态，``calls`` 是 ``reparse_document`` 收到的 kwargs。
    """
    from services import bulk_reparse_service as svc

    monkeypatch.setattr(svc, "_POLL_INTERVAL_S", 0.01)
    outcomes = outcomes or {}
    observed: list[tuple[str, float | None, str]] = []
    calls: list[tuple[str, dict]] = []

    def _fake(doc_id: str, **kwargs):
        calls.append((doc_id, kwargs))
        snapshot = kb_repo.get(kb_id)
        observed.append(
            (snapshot.index_status, snapshot.index_progress, snapshot.index_current_doc)
        )
        outcome = outcomes.get(doc_id, "embedded")
        if outcome == "raise":
            raise RuntimeError("simulated reparse outage")
        doc = doc_repo.find_doc_by_id(doc_id)
        doc.embedding_status = outcome
        doc_repo._save_doc_meta(doc)
        return {"status": "pending_index", "doc_id": doc_id}

    monkeypatch.setattr(svc, "reparse_document", _fake)
    return svc, observed, calls


def test_bulk_run_passes_its_kb_writer_to_every_doc(kb, monkeypatch):
    """编排层必须对每一篇注入它跨文档共享的 ``KbIndexStatusWriter``，
    否则每篇仍会自己造 total=1 的 writer、单篇一完成就把 KB 写回 searchable
    （#93 抖动症状的源头）。"""
    _add_doc(kb.id, "a.pdf", embedding_status="failed", page_count=3)
    _add_doc(kb.id, "b.pdf", embedding_status="failed", page_count=3)
    svc, _observed, calls = _stub_reparse_observing_kb(monkeypatch, kb.id)

    svc.run_bulk_reparse(kb.id, svc.list_target_docs(kb.id), concurrency=1)

    assert len(calls) == 2
    writers = [kwargs.get("kb_writer") for _doc_id, kwargs in calls]
    assert all(w is not None for w in writers), (
        f"编排层必须对每篇传入 kb_writer，实际 {writers}"
    )
    # 跨文档共享：两次调用拿到的是同一个 writer 实例。
    assert writers[0] is writers[1], (
        "跨文档共享的应是同一个 KbIndexStatusWriter 实例"
        f"（实际 {writers[0]!r} vs {writers[1]!r}）"
    )


def test_bulk_run_never_claims_searchable_mid_batch(kb, monkeypatch):
    """#93 抖动症状的直接回归锁：整批期间 KB 一律 ``building``，终态只写一次。"""
    _add_doc(kb.id, "a.pdf", embedding_status="failed", page_count=3)
    _add_doc(kb.id, "b.pdf", embedding_status="failed", page_count=3)
    _add_doc(kb.id, "c.pdf", embedding_status="failed", page_count=3)
    svc, observed, _calls = _stub_reparse_observing_kb(monkeypatch, kb.id)
    writer_calls = _spy_kb_writer(monkeypatch)

    svc.run_bulk_reparse(kb.id, svc.list_target_docs(kb.id), concurrency=1)

    # 每篇开跑的那一刻，KB 对外都还是 building
    assert [status for status, _p, _d in observed] == ["building"] * 3

    methods = [name for name, _a, _kw in writer_calls]
    assert methods[0] == "begin", f"批次开头必须先 begin()，实际序列 {methods}"
    assert methods[-1] == "finish", f"批次末尾必须 finish()，实际序列 {methods}"
    # 期间只许推进，不许触发 finish（finish 是终态唯一入口）
    middle = methods[1:-1]
    assert all(m in ("note_in_flight", "advance") for m in middle), (
        f"整批期间只许 note_in_flight/advance，实际中间序列 {middle}"
    )
    # finish 只能调一次 → 终态只写一次
    assert methods.count("finish") == 1, (
        f"finish 应只调一次（终态写一次），实际 {methods.count('finish')} 次，"
        f"序列 {methods}"
    )


def test_bulk_run_starts_at_building_with_zero_progress(kb, monkeypatch):
    """触发瞬间 KB 即 ``building`` + ``index_progress = 0``（前端轮询的起点）。

    writer 的 ``begin()`` 是契约的承载点：写一次 ``(status=building,
    progress=0.0, current_doc="")``。该断言从原 KB 写入序列前移至
    writer 首次 callback —— begin() 是空入参的状态机开端，progress=0
    是 writer 内部写死的语义常量。
    """
    _add_doc(kb.id, "a.pdf", embedding_status="failed", page_count=3)
    svc, _observed, _calls = _stub_reparse_observing_kb(monkeypatch, kb.id)
    writer_calls = _spy_kb_writer(monkeypatch)

    svc.run_bulk_reparse(kb.id, svc.list_target_docs(kb.id), concurrency=1)

    # 头一次 callback 是 begin() 且不带任何入参（progress=0 是写死的语义）
    assert writer_calls[0][0] == "begin"
    assert writer_calls[0][1] == ()  # 位置参数为空
    assert writer_calls[0][2] == {}  # 关键字参数也为空


def test_bulk_run_advances_progress_monotonically_as_done_over_total(kb, monkeypatch):
    """``index_progress`` 是 ``done / total`` 且单调不减；``index_current_doc`` 是在跑的那篇。

    改在 writer 接口层取证：``advance(done)`` 的入参单调不减 → 落盘的
    ``index_progress`` 单调不减；``note_in_flight(name)`` 的入参就是"那一刻
    在飞的那篇"。终态由 ``finish()`` 一锤定音，不再靠 ``current_doc == ""``
    兜底（writer 自己清空）。
    """
    a = _add_doc(kb.id, "a.pdf", embedding_status="failed", page_count=3)
    b = _add_doc(kb.id, "b.pdf", embedding_status="failed", page_count=3)
    c = _add_doc(kb.id, "c.pdf", embedding_status="failed", page_count=3)
    names = {a.original_name, b.original_name, c.original_name}
    svc, observed, _calls = _stub_reparse_observing_kb(monkeypatch, kb.id)
    writer_calls = _spy_kb_writer(monkeypatch)

    svc.run_bulk_reparse(kb.id, svc.list_target_docs(kb.id), concurrency=1)

    # advance 入参（已完成计数）单调不减
    advance_args = [
        args[0] for name, args, _kw in writer_calls if name == "advance"
    ]
    assert advance_args == sorted(advance_args), (
        f"advance 入参必须单调不减，实际 {advance_args}"
    )
    # 串行跑 3 篇：完成 1/3、2/3 两个中间刻度都应出现（writer 内部换算成
    # progress=1/3、2/3 落盘；这里看入参序列等效）
    assert advance_args == [1, 2, 3], f"应有三次 advance 入参为 1/2/3，实际 {advance_args}"

    # note_in_flight 入参 = "那一刻在飞的那篇"
    in_flight = [
        args[0] for name, args, _kw in writer_calls if name == "note_in_flight"
    ]
    assert set(in_flight) == names, (
        f"在飞文档名集合应为 {names}，实际 {in_flight}"
    )

    # 终态由 finish() 一锤定音；不留残余的 current_doc
    final = kb_repo.get(kb.id)
    assert final.index_status == "searchable"
    assert final.index_current_doc == "", "成功终态不留残余的在飞文档名"


def test_bulk_run_failure_terminates_failed_with_error_summary(kb, monkeypatch):
    """批次中途有失败 → 终态 ``failed``，``index_current_doc`` 带人能读懂的失败摘要。

    #154 取证点迁到 ``finish(failed=...)`` 调用的入参：writer 自己把失败
    列表收归成 ``"批量重新解析失败 N/M 篇（name: reason）"`` 一行 —— 测试
    只断言"它被以正确入参调了一次"，不再断言"KB 写入序列以 failed 收尾"
    （那是 ``finish`` 的实现细节）。
    """
    ok = _add_doc(kb.id, "ok.pdf", embedding_status="failed", page_count=3)
    bad = _add_doc(kb.id, "bad.pdf", embedding_status="failed", page_count=3)
    svc, _observed, _calls = _stub_reparse_observing_kb(
        monkeypatch, kb.id, {bad.id: "failed"}
    )
    writer_calls = _spy_kb_writer(monkeypatch)

    result = svc.run_bulk_reparse(kb.id, svc.list_target_docs(kb.id), concurrency=1)

    assert result.done == [ok.id]
    methods = [name for name, _a, _kw in writer_calls]
    # 中途的失败不许提前把整库判死（finish 应只在末尾）
    assert "finish" not in methods[:-1], (
        f"批次中间不许调 finish，实际中间序列 {methods[:-1]}"
    )
    assert methods[-1] == "finish", (
        f"批次末尾必须 finish()，实际序列 {methods}"
    )
    assert methods.count("finish") == 1

    # finish 的入参应是失败列表（含 failed 终态串）
    finish_call = writer_calls[-1]
    assert finish_call[0] == "finish"
    # 兼容位置参数与关键字参数两种调用风格
    _args, _kwargs = finish_call[1], finish_call[2]
    failed_list = _args[0] if _args else _kwargs.get("failed", [])
    assert any(
        name == bad.original_name and reason.startswith("failed")
        for name, reason in failed_list
    ), f"finish(failed=...) 应含 bad 文档，实际 {failed_list}"

    # 终态由 writer 落地，KB 真实字段是摘要的最终消费者
    final = kb_repo.get(kb.id)
    assert final.index_status == "failed"
    assert bad.original_name in final.index_current_doc, "摘要要点名是哪篇没跑成"
    assert "1/2" in final.index_current_doc, "摘要要给出失败/总数"


def test_bulk_run_with_nothing_runnable_leaves_kb_status_untouched(kb, monkeypatch):
    """没有可跑的目标（全被拆分成本阈值拦下）= 没发生任何重解析，不该改写 KB 状态。

    #154 改在 writer 层取证：空批次连 ``begin()`` 都不该调，更不该有
    ``finish()`` —— 什么都跑过，没理由告诉前端"我们 building 了"。
    """
    from core.settings import BULK_REPARSE_SPLIT_COST_LIMIT_PAGES

    huge = _add_doc(kb.id, "huge.pdf", embedding_status="failed", page_count=None)
    svc, _observed, _calls = _stub_reparse_observing_kb(monkeypatch, kb.id)
    huge.page_count = BULK_REPARSE_SPLIT_COST_LIMIT_PAGES + 1
    doc_repo._save_doc_meta(huge)
    before = kb_repo.get(kb.id).index_status
    writer_calls = _spy_kb_writer(monkeypatch)

    result = svc.run_bulk_reparse(kb.id, svc.list_target_docs(kb.id), concurrency=1)

    assert result.total == 0
    assert [s.doc.id for s in result.skipped] == [huge.id]
    assert writer_calls == [], (
        f"空批次不该触发任何 writer callback，实际 {writer_calls}"
    )
    assert kb_repo.get(kb.id).index_status == before


def test_bulk_run_kb_writes_go_only_through_writer(kb, monkeypatch):
    """整批期间 ``kb_repo.update`` 只经 ``KbIndexStatusWriter`` 触发。

    issue #147 / #154 决策：KB 状态字段（``index_status`` / ``index_progress``
    / ``index_current_doc``）的唯一写入者是 ``KbIndexStatusWriter``。任何绕开
    writer 直接 ``kb_repo.update`` 的代码路径都属 #93 抖动症状复发。

    本测试是这条契约的"可证伪"层：writer 的每次 callback 内部都恰好调一次
    ``kb_repo.update``；一旦旁路写路径出现，两边计数立刻对不上。
    """
    _add_doc(kb.id, "a.pdf", embedding_status="failed", page_count=3)
    _add_doc(kb.id, "b.pdf", embedding_status="failed", page_count=3)
    svc, _observed, _calls = _stub_reparse_observing_kb(monkeypatch, kb.id)
    writer_calls = _spy_kb_writer(monkeypatch)
    kb_updates = _spy_kb_repo_updates(monkeypatch)

    svc.run_bulk_reparse(kb.id, svc.list_target_docs(kb.id), concurrency=1)

    # load-bearing 断言：两边计数严格相等。差一个就意味着存在绕开 writer 的
    # 写入路径 —— 这正是 #93 抖动症状复发的指纹。
    assert len(kb_updates) == len(writer_calls), (
        f"kb_repo.update 应只经 writer 触发；"
        f"writer callback {len(writer_calls)} 次、kb_repo.update {len(kb_updates)} 次"
        f"—— 差距意味存在绕开 writer 的写入路径"
    )


def test_bulk_run_begin_called_exactly_once_across_all_docs(kb, monkeypatch):
    """issue #155 验收点：批量 N 篇 ⇒ ``begin()`` 恰好 1 次（不是 N+1 次）。

    旧实现里编排层调一次 ``begin()``，每个 per-doc 线程进 ``_reparse_async``
    又调一次 ``begin()`` —— N 篇 ⇒ N+1 次，每次把 ``_progress`` 清零、把
    ``index_progress=0.0`` 写盘，前端轮询会在两帧之间看到 ``0/N`` 回退。
    修法：``begin()`` 由 caller 独家承担，``_reparse_async`` 不再调。
    """
    for n in ("a.pdf", "b.pdf", "c.pdf"):
        _add_doc(kb.id, n, embedding_status="failed", page_count=3)
    svc, _observed, _calls = _stub_reparse_observing_kb(monkeypatch, kb.id)
    writer_calls = _spy_kb_writer(monkeypatch)

    svc.run_bulk_reparse(kb.id, svc.list_target_docs(kb.id), concurrency=1)

    begin_count = sum(1 for name, _a, _kw in writer_calls if name == "begin")
    assert begin_count == 1, (
        f"批量 N 篇 ⇒ begin() 应只调 1 次（编排层独家承担），"
        f"实际 {begin_count} 次；序列 {[n for n, _, _ in writer_calls]}"
    )


def test_bulk_run_interrupted_orchestration_still_lands_terminal_status(kb, monkeypatch):
    """编排层自身抛错（这里用逐篇回调模拟）也不许把 KB 留在 ``building``。

    留在 ``building`` 的 KB 会让前端永远轮询一个永不落地的批次 —— 比落 failed 更糟。
    """
    _add_doc(kb.id, "a.pdf", embedding_status="failed", page_count=3)
    _add_doc(kb.id, "b.pdf", embedding_status="failed", page_count=3)
    svc, _observed, _calls = _stub_reparse_observing_kb(monkeypatch, kb.id)

    def _boom(completed, total, doc, outcome):
        raise RuntimeError("progress renderer exploded")

    with pytest.raises(RuntimeError, match="progress renderer exploded"):
        svc.run_bulk_reparse(
            kb.id, svc.list_target_docs(kb.id), concurrency=1, on_doc_complete=_boom
        )

    final = kb_repo.get(kb.id)
    assert final.index_status == "failed"
    assert "中断" in final.index_current_doc
    assert "progress renderer exploded" in final.index_current_doc


def test_bulk_run_tolerates_missing_kb_without_raising(kb, monkeypatch):
    """``KbIndexStatusWriter._write`` 遇到 ``kb_repo.get`` 返回 ``None`` 时静默跳过。

    真实场景：KB 元数据被运维误删 / 还没创建就被外部触发。编排层自身不应因此炸。

    （原 docstring 引用的 ``_KbIndexStatus._write`` 已在 #154 收归 writer；
    行为本身不变，仍是 ``kb_repo.get`` 返回 ``None`` 时静默 noop。）
    """
    from services import bulk_reparse_service as svc

    _add_doc(kb.id, "a.pdf", embedding_status="failed", page_count=3)
    _stub_reparse(monkeypatch, {})  # 单篇立刻落 embedded，避免轮询挂死
    monkeypatch.setattr(svc.kb_repo, "get", lambda _kb_id: None)

    result = svc.run_bulk_reparse(
        kb.id, svc.list_target_docs(kb.id), concurrency=1
    )

    assert result.done == [doc_repo.list_docs(kb.id)[0].id]
    assert result.failed == []


# ── CLI 薄 wrapper：与 service 同源 ─────────────────────────────────────────────


@pytest.fixture
def cli_module():
    """按文件路径加载 ``scripts/bulk_reparse.py``（scripts 不是包）。"""
    path = Path(__file__).resolve().parent.parent / "scripts" / "bulk_reparse.py"
    spec = importlib.util.spec_from_file_location("bulk_reparse_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_no_longer_owns_domain_logic(cli_module):
    """CLI 不再自带选取 / 估算 / 单篇编排实现（issue #108 验收）。"""
    for name in ("list_target_docs", "estimate_ocr_cost", "reparse_one"):
        assert not hasattr(cli_module, name), (
            f"{name} 应已下沉到 services.bulk_reparse_service，CLI 只许委托"
        )
    src = (Path(__file__).resolve().parent.parent / "scripts" / "bulk_reparse.py").read_text()
    assert "ThreadPoolExecutor" not in src, "线程池编排应在 service 里，CLI 不再自己起池"
    assert "load_dotenv" in src, "CLI 必须自行加载 .env（#93 regression 防线）"


def test_cli_dry_run_matches_service_target_list_and_estimate(kb, cli_module, capsys):
    """同一 KB 上 CLI dry-run 与 service 直调给出同一份名单与成本（story 43）。"""
    from services import bulk_reparse_service as svc

    _add_doc(kb.id, "nopages.pdf", embedding_status="embedded", pages=None)
    _add_doc(kb.id, "failed.pdf", embedding_status="failed", page_count=42, pages=_good_pages())
    _add_doc(kb.id, "healthy.pdf", embedding_status="embedded", pages=_good_pages())

    targets = svc.list_target_docs(kb.id)
    cost = svc.estimate_ocr_cost(targets)

    exit_code = cli_module.bulk_reparse(kb.id, dry_run=True, concurrency=4, skip_confirm=True)
    out = capsys.readouterr().out

    assert exit_code == 2, "dry-run 退出码必须是 2"
    assert f"目标 doc 数: {len(targets)}" in out
    assert f"{cost.pages_uncached} 页" in out
    for target in targets:
        assert target.doc.id in out
    assert len(targets) == 2


def test_cli_force_flag_reaches_service(kb, cli_module, capsys):
    """``--force`` 让 CLI 把整库当目标（与 service 的 force 语义一致）。"""
    _add_doc(kb.id, "healthy.pdf", embedding_status="embedded", pages=_good_pages())

    assert cli_module.bulk_reparse(kb.id, dry_run=True, concurrency=4, skip_confirm=True) == 0
    capsys.readouterr()

    exit_code = cli_module.bulk_reparse(
        kb.id, dry_run=True, concurrency=4, skip_confirm=True, force=True
    )
    out = capsys.readouterr().out

    assert exit_code == 2
    assert "目标 doc 数: 1" in out


def test_cli_argparse_contract_preserved(cli_module, monkeypatch):
    """``--kb-id`` / ``--dry-run`` / ``--concurrency`` / ``--yes`` / ``--force`` 全部在，
    且透传给 service 编排入口；退出码沿用 ``bulk_reparse`` 的返回值。"""
    seen = {}

    def _fake_bulk(kb_id, **kwargs):
        seen.update({"kb_id": kb_id, **kwargs})
        return 0

    monkeypatch.setattr(cli_module, "bulk_reparse", _fake_bulk)
    monkeypatch.setattr(
        "sys.argv",
        [
            "bulk_reparse.py", "--kb-id", "kb_x",
            "--dry-run", "--concurrency", "8",
            "--yes", "--force", "--ignore-cost-limit",
        ],
    )

    assert cli_module.main() == 0
    assert seen == {
        "kb_id": "kb_x",
        "dry_run": True,
        "concurrency": 8,
        "skip_confirm": True,
        "force": True,
        "ignore_cost_limit": True,
    }


def test_cli_dry_run_prints_split_aggregate_segment(kb, cli_module, capsys):
    """#182 验收：dry-run 输出含「将拆 X 篇 / 共 Y 块 / 总 Z 页 OCR」聚合段 +
    per-doc 行（spec #182 / spec §F）。"""
    _add_doc(
        kb.id, "huge.pdf", embedding_status="failed",
        page_count=247,
    )

    exit_code = cli_module.bulk_reparse(
        kb.id, dry_run=True, concurrency=4, skip_confirm=True,
    )
    out = capsys.readouterr().out

    assert exit_code == 2
    # 聚合段
    assert "将拆 1 篇" in out
    assert "共 3 块" in out  # 247 / 99 = 3
    assert "总 247 页 OCR" in out
    # per-doc 行
    assert "huge.pdf" in out
    assert "247 页" in out


def test_cli_dry_run_uses_positive_wording_for_over_cost(kb, cli_module, capsys):
    """#182 验收：dry-run 输出的 cost_exceeded 段改为"⚠️ 超成本阈值将被跳过"。

    旧版"超拆分成本阈值"也带"超"，但本 ticket 把"超成本阈值"作为最终文案落定
    （issue #182 / spec §F 改文案、不动结构）。
    """
    from core.settings import BULK_REPARSE_SPLIT_COST_LIMIT_PAGES

    _add_doc(
        kb.id, "huge.pdf", embedding_status="failed",
        page_count=BULK_REPARSE_SPLIT_COST_LIMIT_PAGES + 1,
    )

    exit_code = cli_module.bulk_reparse(
        kb.id, dry_run=True, concurrency=4, skip_confirm=True,
    )
    out = capsys.readouterr().out

    assert exit_code == 2
    # 新文案
    assert "⚠️  超成本阈值将被跳过" in out
    # 旧文案（"超 PAGE_LIMIT"）已退出
    assert "PAGE_LIMIT" not in out


def test_cli_force_with_cost_exceeded_prints_banner_and_keeps_exit_code(
    kb, cli_module, capsys, monkeypatch, caplog,
):
    """#182 验收：``force=True`` + 存在 cost-exceeded doc → stdout 出现 ⚠️ banner +
    log WARNING，整批**继续**跑，退出码不变。

    与 CLI 既有 "force 不能绕成本护栏" 语义保持一致（issue #181 / spec #181）；
    本 ticket 把这条 banner 从"被 confirm 才有"提升到"run 起始无条件打"，
    让 ``--yes`` 直跑也能看见强提示。
    """
    import logging
    from core.settings import BULK_REPARSE_SPLIT_COST_LIMIT_PAGES

    _add_doc(
        kb.id, "huge.pdf", embedding_status="failed",
        page_count=BULK_REPARSE_SPLIT_COST_LIMIT_PAGES + 1,
    )
    # 让 runnable 非空，确保实际 run 真的跑
    _add_doc(kb.id, "small.pdf", embedding_status="failed", page_count=3)

    svc = _stub_reparse(monkeypatch, outcomes={})

    with caplog.at_level(logging.WARNING, logger="scripts.bulk_reparse"):
        exit_code = cli_module.bulk_reparse(
            kb.id, dry_run=False, concurrency=1, skip_confirm=True, force=True,
        )

    out = capsys.readouterr().out

    # 退出码：整批没失败 → 0（huge 进 skipped，small 进 done）
    assert exit_code == 0
    # stdout 出现 ⚠️ banner
    assert "⚠️" in out
    assert "force 不能绕过成本护栏" in out
    # log WARNING 走 scripts.bulk_reparse logger
    assert any(
        "force 不能绕过成本护栏" in rec.getMessage()
        for rec in caplog.records
    ), f"应有 WARNING 日志，实测 {[r.getMessage() for r in caplog.records]}"


def test_cli_without_force_keeps_existing_confirm_prompt_warning(kb, cli_module, capsys, monkeypatch):
    """非 force + cost-exceeded 仍走既有的 "确认前提示" 路径（issue #181 AC），
    **不**升级到 run 起始 banner —— 那是 force 专属信号（issue #182 / spec §F）。"""
    from core.settings import BULK_REPARSE_SPLIT_COST_LIMIT_PAGES

    _add_doc(
        kb.id, "huge.pdf", embedding_status="failed",
        page_count=BULK_REPARSE_SPLIT_COST_LIMIT_PAGES + 1,
    )
    _add_doc(kb.id, "small.pdf", embedding_status="failed", page_count=3)

    # 取消用户的回答（"n"）让 CLI 提早返回 2 —— 不实际跑，只看 prompt 前那一段
    monkey_input = iter(["n"])
    monkeypatch.setattr("builtins.input", lambda _: next(monkey_input))

    exit_code = cli_module.bulk_reparse(
        kb.id, dry_run=False, concurrency=1, skip_confirm=False, force=False,
    )
    out = capsys.readouterr().out

    # 用户取消 → 2
    assert exit_code == 2
    # 既有提示（不是 force 专属 banner）
    assert "检测到" in out and "run 将自动跳过这些 doc" in out
    # force 专属的强 banner 不会出现（非 force 路径）
    assert "force 不能绕过成本护栏" not in out
