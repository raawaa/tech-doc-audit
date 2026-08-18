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
from pathlib import Path

import pytest

import storage.doc_repo as doc_repo
import storage.kb_repo as kb_repo
from core import paddleocr_cache, pages_store
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


def test_over_page_limit_doc_lands_in_warnings_and_is_billed_at_page_limit(kb):
    """超 ``PAGE_LIMIT`` → 进 ``over_page_limit`` 清单；成本按 PAGE_LIMIT 封顶（服务端会截断）。"""
    from services import bulk_reparse_service as svc

    doc = _add_doc(kb.id, "huge.pdf", embedding_status="failed", page_count=svc.PAGE_LIMIT + 50)

    cost = svc.estimate_ocr_cost(svc.list_target_docs(kb.id))

    assert [over.doc.id for over in cost.over_page_limit] == [doc.id]
    assert cost.over_page_limit[0].page_count == svc.PAGE_LIMIT + 50
    assert cost.over_page_limit[0].reason == svc.SKIP_REASON_PAGE_LIMIT
    assert cost.pages_uncached == svc.PAGE_LIMIT


def test_split_by_page_limit_separates_runnable_from_skipped(kb):
    """分类器把"会跑"与"会跳过"分开，跳过项带原因。"""
    from services import bulk_reparse_service as svc

    small = _add_doc(kb.id, "small.pdf", embedding_status="failed", page_count=10)
    huge = _add_doc(kb.id, "huge.pdf", embedding_status="failed", page_count=svc.PAGE_LIMIT + 1)

    runnable, skipped = svc.split_by_page_limit(svc.list_target_docs(kb.id))

    assert [t.doc.id for t in runnable] == [small.id]
    assert [s.doc.id for s in skipped] == [huge.id]
    assert skipped[0].reason == svc.SKIP_REASON_PAGE_LIMIT
    assert skipped[0].page_count == svc.PAGE_LIMIT + 1


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


def test_run_bulk_reparse_skips_over_page_limit_without_raising(kb, monkeypatch):
    """超页数上限的文档进 skipped，不被触发、不抛错。"""
    huge = _add_doc(kb.id, "huge.pdf", embedding_status="failed")
    small = _add_doc(kb.id, "small.pdf", embedding_status="failed", page_count=3)
    svc = _stub_reparse(monkeypatch, {})
    huge.page_count = svc.PAGE_LIMIT + 1
    doc_repo._save_doc_meta(huge)

    result = svc.run_bulk_reparse(kb.id, svc.list_target_docs(kb.id), concurrency=1)

    assert [s.doc.id for s in result.skipped] == [huge.id]
    assert result.skipped[0].reason == svc.SKIP_REASON_PAGE_LIMIT
    assert result.done == [small.id]
    # 跳过的那篇状态没被动过
    assert doc_repo.get_doc(kb.id, huge.id).embedding_status == "failed"


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


# ── KB 级检索状态稳定性（issue #109）────────────────────────────────────────────
#
# 批量跑到一半时 KB **不可能**"此刻可被向量检索"。#93 实测下每完成一篇就写回
# searchable，154 篇 = 上百次 building ⇄ searchable 抖动，前端轮询以
# ``index_status === 'building'`` 为唯一续订条件，于是反复停轮询又重启。
# 编排层现在是 KB 级状态的**唯一写入者**：批次开头写一次 building，
# 期间只推进 index_progress / index_current_doc，末尾写一次终态。


def _spy_kb_writes(monkeypatch):
    """记录所有经 ``kb_repo.update`` 落盘的 KB 状态快照，按时间顺序。

    "整批期间只在首尾各写一次终态"这条契约只有看写入序列才能证伪，
    看最终快照是看不出中间抖动的。
    """
    writes: list[tuple[str, float | None, str]] = []
    real_update = kb_repo.update

    def _spy(kb):
        writes.append((kb.index_status, kb.index_progress, kb.index_current_doc))
        return real_update(kb)

    monkeypatch.setattr(kb_repo, "update", _spy)
    return writes


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
    writes = _spy_kb_writes(monkeypatch)

    svc.run_bulk_reparse(kb.id, svc.list_target_docs(kb.id), concurrency=1)

    # 每篇开跑的那一刻，KB 对外都还是 building
    assert [status for status, _p, _d in observed] == ["building"] * 3

    statuses = [status for status, _p, _d in writes]
    assert statuses[0] == "building", "批次开头必须先写一次 building"
    assert statuses[-1] == "searchable", "批次末尾必须落终态"
    assert all(s == "building" for s in statuses[:-1]), (
        f"整批期间只许停在 building，实际写入序列 {statuses}"
    )
    assert statuses.count("searchable") == 1, "终态只许写一次"


def test_bulk_run_starts_at_building_with_zero_progress(kb, monkeypatch):
    """触发瞬间 KB 即 ``building`` + ``index_progress = 0``（前端轮询的起点）。"""
    _add_doc(kb.id, "a.pdf", embedding_status="failed", page_count=3)
    svc, _observed, _calls = _stub_reparse_observing_kb(monkeypatch, kb.id)
    writes = _spy_kb_writes(monkeypatch)

    svc.run_bulk_reparse(kb.id, svc.list_target_docs(kb.id), concurrency=1)

    assert writes[0][:2] == ("building", 0.0)


def test_bulk_run_advances_progress_monotonically_as_done_over_total(kb, monkeypatch):
    """``index_progress`` 是 ``done / total`` 且单调不减；``index_current_doc`` 是在跑的那篇。"""
    a = _add_doc(kb.id, "a.pdf", embedding_status="failed", page_count=3)
    b = _add_doc(kb.id, "b.pdf", embedding_status="failed", page_count=3)
    c = _add_doc(kb.id, "c.pdf", embedding_status="failed", page_count=3)
    names = {a.original_name, b.original_name, c.original_name}
    svc, observed, _calls = _stub_reparse_observing_kb(monkeypatch, kb.id)
    writes = _spy_kb_writes(monkeypatch)

    svc.run_bulk_reparse(kb.id, svc.list_target_docs(kb.id), concurrency=1)

    progresses = [p for _s, p, _d in writes]
    assert progresses == sorted(progresses), f"进度必须单调不减，实际 {progresses}"
    # 串行跑 3 篇：完成 1/3、2/3 两个中间刻度都应出现过（第 3 篇并入终态 1.0）
    assert 1 / 3 in progresses and 2 / 3 in progresses
    assert progresses[-1] == 1.0

    # 每篇开跑时 index_current_doc 指向在飞的那一篇
    in_flight = [current for _s, _p, current in observed]
    assert set(in_flight) == names
    assert writes[-1][2] == "", "成功终态不留残余的在飞文档名"


def test_bulk_run_failure_terminates_failed_with_error_summary(kb, monkeypatch):
    """批次中途有失败 → 终态 ``failed``，``index_current_doc`` 带人能读懂的失败摘要。"""
    ok = _add_doc(kb.id, "ok.pdf", embedding_status="failed", page_count=3)
    bad = _add_doc(kb.id, "bad.pdf", embedding_status="failed", page_count=3)
    svc, _observed, _calls = _stub_reparse_observing_kb(
        monkeypatch, kb.id, {bad.id: "failed"}
    )
    writes = _spy_kb_writes(monkeypatch)

    result = svc.run_bulk_reparse(kb.id, svc.list_target_docs(kb.id), concurrency=1)

    assert result.done == [ok.id]
    statuses = [status for status, _p, _d in writes]
    assert all(s == "building" for s in statuses[:-1]), (
        f"中途那篇失败不许提前把整库判死，实际 {statuses}"
    )
    assert statuses[-1] == "failed"
    assert statuses.count("failed") == 1

    final = kb_repo.get(kb.id)
    assert final.index_status == "failed"
    assert bad.original_name in final.index_current_doc, "摘要要点名是哪篇没跑成"
    assert "1/2" in final.index_current_doc, "摘要要给出失败/总数"


def test_bulk_run_with_nothing_runnable_leaves_kb_status_untouched(kb, monkeypatch):
    """没有可跑的目标（全被页数上限拦下）= 没发生任何重解析，不该改写 KB 状态。"""
    huge = _add_doc(kb.id, "huge.pdf", embedding_status="failed")
    svc, _observed, _calls = _stub_reparse_observing_kb(monkeypatch, kb.id)
    huge.page_count = svc.PAGE_LIMIT + 1
    doc_repo._save_doc_meta(huge)
    before = kb_repo.get(kb.id).index_status
    writes = _spy_kb_writes(monkeypatch)

    result = svc.run_bulk_reparse(kb.id, svc.list_target_docs(kb.id), concurrency=1)

    assert result.total == 0
    assert [s.doc.id for s in result.skipped] == [huge.id]
    assert writes == [], "空批次不该产生任何 KB 状态写入"
    assert kb_repo.get(kb.id).index_status == before


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
    """``_KbIndexStatus._write`` 遇到 ``kb_repo.get`` 返回 ``None`` 时静默跳过。

    真实场景：KB 元数据被运维误删 / 还没创建就被外部触发。编排层自身不应因此炸。
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
        ["bulk_reparse.py", "--kb-id", "kb_x", "--dry-run", "--concurrency", "8", "--yes", "--force"],
    )

    assert cli_module.main() == 0
    assert seen == {
        "kb_id": "kb_x",
        "dry_run": True,
        "concurrency": 8,
        "skip_confirm": True,
        "force": True,
    }
