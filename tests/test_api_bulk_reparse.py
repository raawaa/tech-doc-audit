"""``/api/v1/knowledge-bases/{kb_id}/bulk-reparse/...`` 三个 HTTP 端点契约测试。

issue #111 — 把批量重新解析 (Bulk Reparse) 从 CLI 升级为产品入口：

- ``GET  /bulk-reparse/preflight?force=...`` —— 无副作用预检
- ``POST /bulk-reparse`` —— 异步触发（接受 ``concurrency`` / ``force``）
- ``GET  /bulk-reparse/report`` —— 上一次运行的报告

所有测试都在 HTTP seam 上跑（``TestClient``），只桩掉编排层真正会触发的副作用
（``reparse_document``），其余都走真路径。轮询 KB 状态用 ``time.sleep`` —
跟 ``test_api_index_status_contract.py`` 同款。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import app

client = TestClient(app)


# ── fixtures：清理数据 + 假模型 + 等待异步线程 ─────────────────────────────


@pytest.fixture(autouse=True)
def _use_fake_models(fake_models):
    """避免加载真 bge-m3；触发批量时各端点不需要 embedder，但 fake_models 让单
    篇的 ``embedding_status`` 推进走 fake 链路，不去碰真模型加载。"""
    yield


# 后台线程 join 由 ``tests/conftest._wait_for_async_rebuild_threads``（autouse）
# 全局处理；这里不再重复造一遍。


# ── helpers ──────────────────────────────────────────────────────────────


def _create_kb(name: str = "bulk API 测试库") -> str:
    resp = client.post(
        "/api/v1/knowledge-bases",
        json={"name": name, "category": "national"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _add_doc(
    kb_id: str,
    name: str,
    *,
    embedding_status: str = "embedded",
    page_count: int | None = 5,
    content_hash: str | None = None,
):
    """通过 storage API 直接造一篇 doc（不走 doc_svc，避免触发真解析链路）。"""
    import storage.doc_repo as doc_repo

    doc = doc_repo.save_doc(kb_id, name, b"%PDF-1.4 dummy " + name.encode(), "pdf")
    doc.embedding_status = embedding_status
    doc.page_count = page_count
    doc.content_hash = content_hash
    doc_repo._save_doc_meta(doc)
    return doc


def _stub_reparse(monkeypatch, outcomes: dict | None = None, side_effect=None, *, delay: float = 0.0):
    """把 ``reparse_document`` 桩成"按 doc_id 直接落终态"。

    路由层 ``from services.bulk_reparse_service import run_bulk_reparse``
    没有 import ``reparse_document``；``run_bulk_reparse`` 内部从
    ``services.reparse_service`` import 这个名字，所以 monkeypatch
    ``services.bulk_reparse_service.reparse_document`` 是改源模块，
    路由里的 ``run_bulk_reparse`` 调用的就是这个引用 —— patch 会生效。

    ``outcomes`` 的 value 是目标 ``embedding_status``；``side_effect(doc)`` 让
    测试模拟解析副作用（写 pages 文件 / 缓存条目）。两个互斥：传 ``side_effect``
    时所有 doc 都走它（用于 happy path）；传 ``outcomes`` 用于造失败样本。
    ``delay`` 给单篇加 sleep，让批量跑得慢一点（用于"中段观察 building"测试）。
    """
    import time as _time
    import storage.doc_repo as doc_repo
    from services import bulk_reparse_service as svc

    monkeypatch.setattr(svc, "_POLL_INTERVAL_S", 0.01)
    outcomes = outcomes or {}

    def _fake(doc_id: str, **kwargs):
        if delay:
            _time.sleep(delay)
        if side_effect is not None:
            side_effect(doc_repo.find_doc_by_id(doc_id))
            outcome = "embedded"
        else:
            outcome = outcomes.get(doc_id, "embedded")
        if outcome == "raise":
            raise RuntimeError("simulated reparse outage")
        d = doc_repo.find_doc_by_id(doc_id)
        d.embedding_status = outcome
        doc_repo._save_doc_meta(d)
        return {"status": "pending_index", "doc_id": doc_id}

    monkeypatch.setattr(svc, "reparse_document", _fake)
    return svc


def _poll_until_terminal(kb_id: str, statuses: set, *, timeout: float = 30.0):
    """轮询 KB 直到 ``index_status`` 落入 ``statuses``。返回最后一次看到的 body。"""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        body = client.get(f"/api/v1/knowledge-bases/{kb_id}").json()
        last = body
        if body["index_status"] in statuses:
            return body
        time.sleep(0.05)
    raise AssertionError(
        f"KB {kb_id} 在 {timeout}s 内未达到 {statuses}, "
        f"last status={last and last['index_status']!r}"
    )


# ── preflight：无副作用 dry-run ──────────────────────────────────────────


def test_preflight_returns_target_count_and_estimate():
    """预检返回目标数 / 缓存命中未命中 / 预估 OCR 页数 / 超限清单 / 每篇原因。"""
    from core import pages_store

    kb_id = _create_kb("preflight-target")
    ok = _add_doc(kb_id, "ok.pdf", embedding_status="failed", page_count=4)
    bad = _add_doc(kb_id, "bad.pdf", embedding_status="failed", page_count=4)
    healthy = _add_doc(
        kb_id, "healthy.pdf", embedding_status="embedded", page_count=4,
    )
    # healthy 要真正"健康"：有 pages 文件 + layout 非空，否则会被规则 2/3 兜底捞回。
    pages_store.save_pages(kb_id, healthy.id, {
        "by_page": [{"page": 0, "text": "x" * 50}],
        "full_text": "x" * 50,
        "layout": [{"page": 0, "blocks": [{"block_order": 0}]}],
    })

    resp = client.get(f"/api/v1/knowledge-bases/{kb_id}/bulk-reparse/preflight")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["kb_id"] == kb_id
    assert body["force"] is False
    assert body["target_count"] == 2  # ok + bad，healthy 不进名单
    assert body["cached_docs"] == 0
    assert body["uncached_docs"] == 2
    assert body["estimated_ocr_pages"] == 8  # 4 + 4（都是 uncached）
    assert {t["doc_id"] for t in body["targets"]} == {ok.id, bad.id}
    # over_page_limit 列表对这篇 KB 是空的
    assert body["over_page_limit"] == []
    # 每篇带原因
    reasons = {t["reason"] for t in body["targets"]}
    assert "not_embedded" in reasons


def test_preflight_marks_over_page_limit_in_warning():
    """超 PAGE_LIMIT 的 doc 进 ``over_page_limit`` 清单（AC 5）。"""
    kb_id = _create_kb("preflight-over")
    huge = _add_doc(kb_id, "huge.pdf", embedding_status="failed", page_count=150)

    body = client.get(
        f"/api/v1/knowledge-bases/{kb_id}/bulk-reparse/preflight"
    ).json()

    assert body["target_count"] == 1
    over = body["over_page_limit"]
    assert len(over) == 1
    assert over[0]["doc_id"] == huge.id
    assert over[0]["page_count"] == 150
    assert over[0]["reason"] == "page_limit"


def test_preflight_force_selects_all_docs():
    """``?force=true`` 绕过三条规则，整库皆是目标（AC 8）。"""
    kb_id = _create_kb("preflight-force")
    _add_doc(kb_id, "healthy.pdf", embedding_status="embedded", page_count=4)
    _add_doc(kb_id, "broken.pdf", embedding_status="failed", page_count=4)

    body = client.get(
        f"/api/v1/knowledge-bases/{kb_id}/bulk-reparse/preflight?force=true"
    ).json()

    assert body["force"] is True
    assert body["target_count"] == 2
    assert all(t["reason"] == "forced" for t in body["targets"])


def test_preflight_404_on_unknown_kb():
    """KB 不存在 → 404（不在 issue 验收里写明，但属契约一致）。"""
    resp = client.get(
        "/api/v1/knowledge-bases/kb_does_not_exist/bulk-reparse/preflight"
    )
    assert resp.status_code == 404


def test_preflight_has_zero_side_effects():
    """预检无副作用：不触发 parse、不改 embedding_status、不写 pages/ 或缓存（AC 2）。"""
    import storage.doc_repo as doc_repo
    from core import paddleocr_cache, pages_store

    kb_id = _create_kb("preflight-pure")
    doc = _add_doc(
        kb_id, "pure.pdf",
        embedding_status="failed", page_count=3, content_hash="h_pure",
    )
    pages_dir = pages_store._pages_dir(kb_id)
    cache_dir = paddleocr_cache.get_cache_dir()
    cache_before = (
        set(p.name for p in cache_dir.glob(f"h_pure_*")) if cache_dir.exists() else set()
    )
    pages_before = (
        set(p.name for p in pages_dir.glob("*.json")) if pages_dir.exists() else set()
    )

    # 调三次预检（含 force），确认可反复调
    for url in (
        f"/api/v1/knowledge-bases/{kb_id}/bulk-reparse/preflight",
        f"/api/v1/knowledge-bases/{kb_id}/bulk-reparse/preflight?force=true",
        f"/api/v1/knowledge-bases/{kb_id}/bulk-reparse/preflight",
    ):
        r = client.get(url)
        assert r.status_code == 200, r.text

    # doc 状态没变
    after = doc_repo.get_doc(kb_id, doc.id)
    assert after.embedding_status == "failed"

    # pages/ 没新建
    pages_after = (
        set(p.name for p in pages_dir.glob("*.json")) if pages_dir.exists() else set()
    )
    assert pages_after == pages_before

    # 缓存没新建
    cache_after = (
        set(p.name for p in cache_dir.glob(f"h_pure_*")) if cache_dir.exists() else set()
    )
    assert cache_after == cache_before


def test_preflight_does_not_invoke_parse_document(monkeypatch):
    """AC 2 的另一面：预检根本不触发解析器。

    把 ``parse_document`` 在源模块 + 间接引用处都桩成"被调就抛"——
    任何意外触发会让测试立刻炸。预检走纯只读路径，应该一次都不调它。
    """
    import core.parse_document
    import services.reparse_service

    def _explode(*_a, **_kw):
        raise AssertionError("parse_document must not be called from preflight")

    monkeypatch.setattr(core.parse_document, "parse_document", _explode)
    monkeypatch.setattr(services.reparse_service, "parse_document", _explode)

    kb_id = _create_kb("preflight-no-parse")
    _add_doc(kb_id, "no_parse.pdf", embedding_status="failed", page_count=3)

    r = client.get(f"/api/v1/knowledge-bases/{kb_id}/bulk-reparse/preflight")
    assert r.status_code == 200, r.text
    assert r.json()["target_count"] == 1


def test_preflight_exposes_polluted_cached_count_field():
    """issue #111 AC 1 列出 ``cached / uncached / polluted-cached`` 三类计数，
    即使现在 polluted 桶恒为 0（V8 cache defense 已删），schema 必须留位。
    """
    kb_id = _create_kb("preflight-polluted")
    _add_doc(kb_id, "x.pdf", embedding_status="failed", page_count=3)

    body = client.get(
        f"/api/v1/knowledge-bases/{kb_id}/bulk-reparse/preflight"
    ).json()

    assert "polluted_cached_docs" in body
    assert body["polluted_cached_docs"] == 0


# ── trigger：异步触发 + KB 状态机 + 409 守卫 ──────────────────────────────


def test_trigger_returns_202_and_eventually_heals_to_searchable(monkeypatch):
    """POST /bulk-reparse → 202；KB 立刻 building → 终态 searchable（AC 3 / AC 5）。"""
    kb_id = _create_kb("trigger-happy")
    _add_doc(kb_id, "a.pdf", embedding_status="failed", page_count=3)
    _add_doc(kb_id, "b.pdf", embedding_status="failed", page_count=3)
    _stub_reparse(monkeypatch)

    resp = client.post(f"/api/v1/knowledge-bases/{kb_id}/bulk-reparse")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["kb_id"] == kb_id
    assert body["target_count"] == 2
    # AC 3：响应里就带 index_status=building，不必再多发一次 GET
    assert body["index_status"] == "building"

    final = _poll_until_terminal(kb_id, {"searchable", "failed"})
    assert final["index_status"] == "searchable", (
        f"happy path 终态应为 searchable, got {final['index_status']}"
    )


def test_trigger_with_zero_targets_does_not_get_kb_stuck_in_building(monkeypatch):
    """空批次（全 healthy / 全超页数上限）= 没发生任何重解析。

    若 handler 仍预写 building 而 ``run_bulk_reparse`` 的 ``if total:`` 不进，
    KB 会**永远卡在 building** —— 比触发失败更糟（前端轮询永不落地）。

    期望：202 / target_count=0 / index_status 反映 KB 真实状态（非 building）/
    KB 状态全程不被改写。
    """
    from core import pages_store

    kb_id = _create_kb("trigger-empty")
    healthy = _add_doc(
        kb_id, "healthy.pdf", embedding_status="embedded", page_count=3,
    )
    pages_store.save_pages(kb_id, healthy.id, {
        "by_page": [{"page": 0, "text": "x" * 50}],
        "full_text": "x" * 50,
        "layout": [{"page": 0, "blocks": [{"block_order": 0}]}],
    })
    _stub_reparse(monkeypatch)

    import storage.kb_repo as kb_repo
    before = kb_repo.get(kb_id).index_status

    r = client.post(f"/api/v1/knowledge-bases/{kb_id}/bulk-reparse")
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["target_count"] == 0
    # index_status 反映 KB 真实状态（不应是 building）
    assert body["index_status"] != "building"

    # 给任何 daemon 线程一点时间落盘（虽然这里根本没起线程）
    time.sleep(0.1)
    after = kb_repo.get(kb_id).index_status
    assert after != "building", (
        f"空批次不该把 KB 卡在 building, got {after}"
    )
    assert after == before, (
        f"空批次不该改写 KB 状态: before={before} after={after}"
    )


def test_trigger_accepts_concurrency_and_force(monkeypatch):
    """``concurrency`` / ``force`` 都透传到 service —— 通过落盘的报告字段验证。

    不直接 spy ``run_bulk_reparse``：路由层 ``from services.bulk_reparse_service
    import run_bulk_reparse`` 已在 import 时绑定了原对象，再 ``setattr``
    ``svc.run_bulk_reparse`` 改不到路由里的引用。报告 schema 里的
    ``concurrency`` 字段由 service 自己写入，所以查报告就能证透传。
    """
    kb_id = _create_kb("trigger-args")
    _add_doc(kb_id, "a.pdf", embedding_status="failed", page_count=3)
    _add_doc(kb_id, "healthy.pdf", embedding_status="embedded", page_count=3)
    _stub_reparse(monkeypatch)

    resp = client.post(
        f"/api/v1/knowledge-bases/{kb_id}/bulk-reparse",
        json={"concurrency": 8, "force": True},
    )
    assert resp.status_code == 202, resp.text
    _poll_until_terminal(kb_id, {"searchable", "failed"})

    # force=true → healthy 也算目标 → target_count == 2
    assert resp.json()["target_count"] == 2

    rep = client.get(f"/api/v1/knowledge-bases/{kb_id}/bulk-reparse/report").json()
    assert rep["concurrency"] == 8
    assert rep["forced"] is True


def test_trigger_kb_is_building_during_the_run(monkeypatch):
    """批次期间 KB 一律 ``building``，从不见 searchable（AC 5 关键回归锁）。

    用 ``delay=0.3`` 把单篇解析拉慢，串行 3 篇需要 ~1s，让中段轮询真能看见
    building 而非被瞬时串行掩盖。
    """
    kb_id = _create_kb("trigger-state")
    for i in range(3):
        _add_doc(kb_id, f"d{i}.pdf", embedding_status="failed", page_count=3)
    _stub_reparse(monkeypatch, delay=0.3)

    client.post(f"/api/v1/knowledge-bases/{kb_id}/bulk-reparse")

    seen_statuses: set[str] = set()
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        kb = client.get(f"/api/v1/knowledge-bases/{kb_id}").json()
        seen_statuses.add(kb["index_status"])
        if kb["index_status"] in {"searchable", "failed"}:
            break
        time.sleep(0.05)

    assert "building" in seen_statuses, (
        f"整批期间至少应观察到 building, 实际 {seen_statuses}"
    )
    # 整批期间**绝不许**提前看见 searchable —— 这是 #93 抖动症状的回归锁
    # （除非终态那一刻自然落到 searchable，本断言约束的是"中段"）。
    assert seen_statuses - {"searchable", "failed"} == {"building"}, (
        f"中段只许见到 building，实际看到 {seen_statuses}"
    )
    # 终态最终落到 searchable / failed
    assert "searchable" in seen_statuses or "failed" in seen_statuses


def test_trigger_409_when_bulk_already_running():
    """已有批量在跑（KB building）→ 再触发直接 409（AC 3）。

    不真起批量线程（那需要穿透路由到 service 的 monkeypatch，且要解决
    import-time 绑定问题）；直接由 ``kb.index_status="building"`` 模拟
    一个 in-flight bulk，验证互斥守门生效。
    """
    kb_id = _create_kb("trigger-lock")
    _add_doc(kb_id, "a.pdf", embedding_status="failed", page_count=3)

    import storage.kb_repo as kb_repo
    kb = kb_repo.get(kb_id)
    kb.index_status = "building"
    kb_repo.update(kb)

    r = client.post(f"/api/v1/knowledge-bases/{kb_id}/bulk-reparse")
    assert r.status_code == 409, r.text


def test_trigger_409_when_reindex_is_running():
    """反方向：reindex 触发的 ``building`` 也能挡住批量触发（AC 7）。"""
    kb_id = _create_kb("trigger-rev")
    _add_doc(kb_id, "a.pdf", embedding_status="failed", page_count=3)

    # 直接把 KB 标成 building 模拟一个 reindex 进行中
    import storage.kb_repo as kb_repo
    kb = kb_repo.get(kb_id)
    kb.index_status = "building"
    kb_repo.update(kb)

    r = client.post(f"/api/v1/knowledge-bases/{kb_id}/bulk-reparse")
    assert r.status_code == 409, r.text


def test_reindex_409_when_bulk_is_running():
    """另一方向：批量进行中也挡住 reindex 触发（AC 7）。

    同 ``test_trigger_409_when_bulk_already_running``，用直接写
    ``index_status=building`` 的方式模拟 in-flight bulk，验证 reindex 端点
    也走同一道守门。这是 issue #111 AC 7 的关键回归锁。
    """
    kb_id = _create_kb("trigger-rev2")
    _add_doc(kb_id, "a.pdf", embedding_status="failed", page_count=3)

    import storage.kb_repo as kb_repo
    kb = kb_repo.get(kb_id)
    kb.index_status = "building"
    kb_repo.update(kb)

    r = client.post(f"/api/v1/knowledge-bases/{kb_id}/reindex")
    assert r.status_code == 409, r.text


def test_trigger_404_on_unknown_kb():
    """KB 不存在 → 404。"""
    r = client.post("/api/v1/knowledge-bases/kb_nope/bulk-reparse")
    assert r.status_code == 404


def test_trigger_failure_terminates_kb_failed(monkeypatch):
    """整批中有失败 → KB 终态 ``failed`` + 报告 done/failed 计数如实反映（AC 6）。"""
    kb_id = _create_kb("trigger-mix")
    ok = _add_doc(kb_id, "ok.pdf", embedding_status="failed", page_count=3)
    bad = _add_doc(kb_id, "bad.pdf", embedding_status="failed", page_count=3)
    _stub_reparse(monkeypatch, outcomes={bad.id: "raise"})

    r = client.post(f"/api/v1/knowledge-bases/{kb_id}/bulk-reparse")
    assert r.status_code == 202, r.text

    final = _poll_until_terminal(kb_id, {"searchable", "failed"})
    assert final["index_status"] == "failed"
    # 失败摘要要点名 + 给出失败/总数
    assert bad.original_name in final["index_current_doc"]
    assert "1/2" in final["index_current_doc"]

    # 报告的 done/failed 计数（AC 6 第二条）
    rep = client.get(f"/api/v1/knowledge-bases/{kb_id}/bulk-reparse/report").json()
    assert rep["counts"] == {"done": 1, "failed": 1, "skipped": 0}
    assert [e["doc_id"] for e in rep["done"]] == [ok.id]
    assert [e["doc_id"] for e in rep["failed"]] == [bad.id]


# ── report：上次运行的报告 ──────────────────────────────────────────────


def test_report_404_when_no_bulk_has_run():
    """从未跑过批量 → 报告端点 404。"""
    kb_id = _create_kb("report-norun")

    r = client.get(f"/api/v1/knowledge-bases/{kb_id}/bulk-reparse/report")
    assert r.status_code == 404, r.text


def test_report_404_on_unknown_kb():
    """KB 不存在 → 404。"""
    r = client.get(
        "/api/v1/knowledge-bases/kb_does_not_exist/bulk-reparse/report"
    )
    assert r.status_code == 404


def test_report_returns_persisted_json_after_a_run(monkeypatch):
    """跑完一次 → 报告端点返回上次落盘的 JSON（AC 4）。"""
    kb_id = _create_kb("report-after")
    _add_doc(kb_id, "a.pdf", embedding_status="failed", page_count=4)
    _stub_reparse(monkeypatch)

    r = client.post(f"/api/v1/knowledge-bases/{kb_id}/bulk-reparse")
    assert r.status_code == 202
    _poll_until_terminal(kb_id, {"searchable", "failed"})

    rep = client.get(f"/api/v1/knowledge-bases/{kb_id}/bulk-reparse/report")
    assert rep.status_code == 200, rep.text
    body = rep.json()

    assert body["kb_id"] == kb_id
    assert body["counts"] == {"done": 1, "failed": 0, "skipped": 0}
    assert body["target_count"] == 1
    # 报告 schema 关键字段
    for key in (
        "started_at", "finished_at", "duration_seconds",
        "estimated_ocr_pages", "actual_ocr_pages",
        "actual_pages_by_source", "actual_docs_by_source",
        "preflight", "done", "failed", "skipped",
    ):
        assert key in body, f"report missing {key}"


def test_report_actual_ocr_pages_bucket_matches_cached_sources(monkeypatch):
    """实测页数按缓存 ``source`` 分桶：paddleocr / pymupdf / cache_hit（AC 8）。"""
    from core import paddleocr_cache, pages_store

    kb_id = _create_kb("report-buckets")

    def _write_pages(doc_id, n):
        pages_store.save_pages(
            kb_id, doc_id,
            {
                "by_page": [{"page": i, "text": "x" * 50} for i in range(n)],
                "full_text": "x" * 50,
                "layout": [{"page": i, "blocks": [{"block_order": 0}]} for i in range(n)],
            },
        )

    def _write_cache(content_hash, source):
        p = (
            paddleocr_cache.get_cache_dir()
            / f"{content_hash}_{paddleocr_cache._MODEL_VERSION}.json"
        )
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(
                {"version": paddleocr_cache._MODEL_VERSION, "source": source, "result": {}}
            ),
            encoding="utf-8",
        )

    # 三篇 doc：跑前无条目 → 跑后 paddleocr（真 OCR）/ 跑前无条目 → 跑后 pymupdf（零配额）/
    # 跑前已有 → cache_hit
    ocr_doc = _add_doc(
        kb_id, "ocr.pdf",
        embedding_status="failed", page_count=4, content_hash="h_ocr",
    )
    mupdf_doc = _add_doc(
        kb_id, "mupdf.pdf",
        embedding_status="failed", page_count=5, content_hash="h_mupdf",
    )
    _add_doc(
        kb_id, "cached.pdf",
        embedding_status="failed", page_count=6, content_hash="h_hit",
    )
    _write_cache("h_hit", source="paddleocr")  # 跑前就有条目

    pages_per_hash = {
        ocr_doc.content_hash: 4,
        mupdf_doc.content_hash: 5,
        "h_hit": 6,  # 跑前已有 cache，run 仍写 pages（解析路径跑过）
    }

    def _on_parse(d):
        _write_pages(d.id, pages_per_hash[d.content_hash])
        if d.content_hash == "h_ocr":
            _write_cache("h_ocr", source="paddleocr")
        elif d.content_hash == "h_mupdf":
            _write_cache("h_mupdf", source="pymupdf")

    _stub_reparse(monkeypatch, side_effect=_on_parse)

    r = client.post(f"/api/v1/knowledge-bases/{kb_id}/bulk-reparse")
    assert r.status_code == 202
    _poll_until_terminal(kb_id, {"searchable", "failed"})

    rep = client.get(
        f"/api/v1/knowledge-bases/{kb_id}/bulk-reparse/report"
    ).json()

    assert rep["actual_ocr_pages"] == 4  # 只有 paddleocr 桶计入
    assert rep["actual_pages_by_source"]["paddleocr"] == 4
    assert rep["actual_pages_by_source"]["pymupdf"] == 5
    assert rep["actual_pages_by_source"]["cache_hit"] == 6
    assert rep["counts"]["done"] == 3