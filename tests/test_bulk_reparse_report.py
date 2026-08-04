"""**批量重新解析报告 (Bulk Reparse Report)** 契约测试（issue #110 / spec #102 第 3 步）。

本 ticket 补上 #102 Problem Statement 第 1 条的缺口：**OCR 消耗只有预估、没有实测**。
#90 报的 "1694/1705 页 OCR 消耗" 事后被 #91 证明是 **0 页**（整库静默走了 fallback），
而当时唯一的地面真相是事后 ``ls data/.cache/paddleocr/`` 数 ``source=paddleocr`` 条目。

所以测试盯住三件事：

1. **实测分桶** —— 每篇完成的 doc 回读它的缓存条目 ``source``，按
   ``paddleocr``（真烧配额）/ ``cache_hit``（跑前就有条目）/ 非 OCR 来源（``pymupdf`` 等）
   / ``unknown``（跑完仍无条目）分桶。这正是 #91 那类 bug 的指纹显影液。
2. **报告落盘** —— ``data/kbs/{kb_id}/bulk_reparse_report.json``，与 ``pages/`` 同级，
   预检估算与实测值**并列**呈现（差异是信号，不是自动拦截）；done / failed / skipped
   三类明细各自带 doc id、原始文件名、原因串。
3. **layout 非空回归**（spec #102 story 46）—— 批量跑完后目标文档的
   ``pages/{doc_id}.json`` 的 ``layout`` 必须非空。这是 #86 症状的直接防线，
   走真实 ``reparse_service`` 链路（只桩掉解析器与向量索引）。

不触碰真实 OCR / 向量索引。
"""
from __future__ import annotations

import json

import pytest

import storage.doc_repo as doc_repo
import storage.kb_repo as kb_repo
from core import bulk_reparse_report_store, paddleocr_cache, pages_store
from models.knowledge_base import KnowledgeBase


# ── fixture：隔离数据目录 + 造 KB 与文档 ────────────────────────────────────────


@pytest.fixture
def isolated_data_dir(tmp_path, monkeypatch):
    """把所有按 import 绑定 ``DATA_DIR`` 的模块指到 tmp_path（同 test_bulk_reparse_service）。"""
    monkeypatch.setattr(doc_repo, "DATA_DIR", tmp_path)
    monkeypatch.setattr(kb_repo, "DATA_DIR", tmp_path)
    monkeypatch.setattr(kb_repo, "KBS_DIR", tmp_path / "kbs")
    monkeypatch.setattr(pages_store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(bulk_reparse_report_store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(paddleocr_cache, "CACHE_DIR", tmp_path / ".cache" / "paddleocr")
    return tmp_path


@pytest.fixture
def kb(isolated_data_dir):
    return kb_repo.create(KnowledgeBase(id="kb_bulk_report", name="批量库", category="national"))


def _add_doc(
    kb_id: str,
    name: str,
    *,
    embedding_status: str = "failed",
    page_count: int | None = 5,
    content_hash: str | None = None,
    pages: dict | None = None,
):
    doc = doc_repo.save_doc(kb_id, name, b"%PDF-1.4 dummy " + name.encode(), "pdf")
    doc.embedding_status = embedding_status
    doc.page_count = page_count
    doc.content_hash = content_hash
    doc_repo._save_doc_meta(doc)
    if pages is not None:
        pages_store.save_pages(kb_id, doc.id, pages)
    return doc


def _pages(page_count: int = 3) -> dict:
    return {
        "by_page": [{"page": i, "text": "x" * 50} for i in range(page_count)],
        "full_text": "x" * 50,
        "layout": [{"page": i, "blocks": [{"block_order": 0}]} for i in range(page_count)],
    }


def _write_cache_entry(content_hash: str, *, source: str) -> None:
    path = paddleocr_cache.CACHE_DIR / f"{content_hash}_{paddleocr_cache._MODEL_VERSION}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": paddleocr_cache._MODEL_VERSION, "source": source, "result": {}}),
        encoding="utf-8",
    )


def _stub_reparse(monkeypatch, *, outcomes=None, on_parse=None):
    """把 ``reparse_document`` 替换为"直接写终态"的桩。

    ``on_parse(doc)`` 模拟解析副作用（写 pages 文件 / 写缓存条目），
    让实测分桶有真实的地面真相可读。
    """
    from services import bulk_reparse_service as svc

    outcomes = outcomes or {}
    monkeypatch.setattr(svc, "_POLL_INTERVAL_S", 0.01)

    def _fake(doc_id: str, *, caller_manages_kb_status: bool = False):
        outcome = outcomes.get(doc_id, "embedded")
        if outcome == "raise":
            raise RuntimeError("simulated reparse outage")
        doc = doc_repo.find_doc_by_id(doc_id)
        if on_parse is not None:
            on_parse(doc)
        doc.embedding_status = outcome
        doc_repo._save_doc_meta(doc)
        return {"status": "pending_index", "doc_id": doc_id}

    monkeypatch.setattr(svc, "reparse_document", _fake)
    return svc


# ── 实测 OCR 分桶：#91 指纹的显影液 ─────────────────────────────────────────────


def test_fresh_ocr_run_counts_pages_into_the_paddleocr_bucket(kb, monkeypatch):
    """跑前无缓存条目、跑后条目 ``source=paddleocr`` → 真烧了配额，进 OCR 桶。"""
    doc = _add_doc(kb.id, "scan.pdf", page_count=4, content_hash="h_fresh")

    def _parse(d):
        pages_store.save_pages(kb.id, d.id, _pages(4))
        _write_cache_entry("h_fresh", source="paddleocr")

    svc = _stub_reparse(monkeypatch, on_parse=_parse)
    result = svc.run_bulk_reparse(kb.id, svc.list_target_docs(kb.id), concurrency=1)

    assert result.usage.actual_ocr_pages == 4
    assert result.usage.pages_by_source == {"paddleocr": 4}
    assert result.usage.docs_by_source == {"paddleocr": 1}
    assert result.done == [doc.id]


def test_cache_hit_before_the_run_burns_no_quota(kb, monkeypatch):
    """跑前**已有**缓存条目 → 这一篇不烧配额，进 ``cache_hit`` 桶而非 OCR 桶。

    条目的 ``source`` 仍是 ``paddleocr`` —— 只看 source 会把缓存命中误报成真实 OCR。
    区分二者的唯一依据是"跑之前有没有条目"，所以快照必须在 run 开始前取。
    """
    _add_doc(kb.id, "cached.pdf", page_count=6, content_hash="h_hit")
    _write_cache_entry("h_hit", source="paddleocr")

    svc = _stub_reparse(monkeypatch, on_parse=lambda d: pages_store.save_pages(kb.id, d.id, _pages(6)))
    result = svc.run_bulk_reparse(kb.id, svc.list_target_docs(kb.id), concurrency=1)

    assert result.usage.actual_ocr_pages == 0
    assert result.usage.pages_by_source == {svc.SOURCE_CACHE_HIT: 6}


def test_non_ocr_source_gets_its_own_bucket(kb, monkeypatch):
    """文字层 PDF 走 PyMuPDF（零配额）→ 独立成桶，不混进 OCR 计数。

    "没花配额"是因为缓存命中还是因为解析走了另一条路，必须能分辨（story 27）。
    """
    _add_doc(kb.id, "textlayer.pdf", page_count=8, content_hash="h_mupdf")

    def _parse(d):
        pages_store.save_pages(kb.id, d.id, _pages(8))
        _write_cache_entry("h_mupdf", source="pymupdf")

    svc = _stub_reparse(monkeypatch, on_parse=_parse)
    result = svc.run_bulk_reparse(kb.id, svc.list_target_docs(kb.id), concurrency=1)

    assert result.usage.actual_ocr_pages == 0
    assert result.usage.pages_by_source == {"pymupdf": 8}


def test_missing_cache_entry_after_the_run_is_unknown_not_ocr(kb, monkeypatch):
    """跑完仍读不到缓存条目 → 记 ``unknown``，绝不默认算成 OCR。

    静默降级的另一种形态：解析成功但没留下任何来源证据。宁可显式报"不知道"，
    也不要凭空补一个好看的 OCR 页数（那正是 #90 的错法）。
    """
    _add_doc(kb.id, "nohash.pdf", page_count=5, content_hash=None)

    svc = _stub_reparse(monkeypatch, on_parse=lambda d: pages_store.save_pages(kb.id, d.id, _pages(5)))
    result = svc.run_bulk_reparse(kb.id, svc.list_target_docs(kb.id), concurrency=1)

    assert result.usage.actual_ocr_pages == 0
    assert result.usage.pages_by_source == {svc.SOURCE_UNKNOWN: 5}


def test_actual_pages_come_from_the_parsed_pages_file_not_the_estimate(kb, monkeypatch):
    """实测页数读 ``pages/{doc_id}.json`` 的真实页数，而非 doc 元数据里的估值。"""
    _add_doc(kb.id, "wrongmeta.pdf", page_count=99, content_hash="h_real")

    def _parse(d):
        pages_store.save_pages(kb.id, d.id, _pages(3))  # 真实只有 3 页
        _write_cache_entry("h_real", source="paddleocr")

    svc = _stub_reparse(monkeypatch, on_parse=_parse)
    result = svc.run_bulk_reparse(kb.id, svc.list_target_docs(kb.id), concurrency=1)

    assert result.usage.actual_ocr_pages == 3


def test_failed_docs_do_not_contribute_to_actual_ocr_pages(kb, monkeypatch):
    """失败的 doc 不进实测分桶（它的来源仍记在 failed 明细里，见报告测试）。"""
    ok = _add_doc(kb.id, "ok.pdf", page_count=2, content_hash="h_ok")
    bad = _add_doc(kb.id, "bad.pdf", page_count=2, content_hash="h_bad")

    def _parse(d):
        pages_store.save_pages(kb.id, d.id, _pages(2))
        _write_cache_entry(d.content_hash, source="paddleocr")

    svc = _stub_reparse(monkeypatch, outcomes={bad.id: "failed"}, on_parse=_parse)
    result = svc.run_bulk_reparse(kb.id, svc.list_target_docs(kb.id), concurrency=1)

    assert result.done == [ok.id]
    assert result.usage.actual_ocr_pages == 2
    # 失败篇入显式 "failed" 桶，**不** 进 paddleocr / cache_hit 桶
    assert result.usage.docs_by_source == {"paddleocr": 1, "failed": 1}


def test_parse_source_is_logged_per_completed_doc(kb, monkeypatch, caplog):
    """每篇的解析来源进 run log —— 静默降级在日志里就该暴露（AC 8）。"""
    _add_doc(kb.id, "logged.pdf", page_count=2, content_hash="h_log")

    def _parse(d):
        pages_store.save_pages(kb.id, d.id, _pages(2))
        _write_cache_entry("h_log", source="pymupdf")

    svc = _stub_reparse(monkeypatch, on_parse=_parse)
    with caplog.at_level("INFO"):
        svc.run_bulk_reparse(kb.id, svc.list_target_docs(kb.id), concurrency=1)

    assert any("pymupdf" in rec.getMessage() for rec in caplog.records)


# ── 报告落盘 ───────────────────────────────────────────────────────────────────


def _run_and_load_report(kb_id, svc, **kwargs) -> dict:
    force = kwargs.pop("force", False)
    svc.run_bulk_reparse(kb_id, svc.list_target_docs(kb_id, force=force), **kwargs)
    report = bulk_reparse_report_store.load_report(kb_id)
    assert report is not None
    return report


def test_report_lands_next_to_the_pages_dir(kb, monkeypatch, isolated_data_dir):
    """报告与 ``pages/`` 同级落在 ``data/kbs/{kb_id}/``，且是可读 JSON（AC 2）。"""
    _add_doc(kb.id, "a.pdf", page_count=2, content_hash="h_a")
    svc = _stub_reparse(monkeypatch, on_parse=lambda d: pages_store.save_pages(kb.id, d.id, _pages(2)))

    svc.run_bulk_reparse(kb.id, svc.list_target_docs(kb.id), concurrency=1)

    path = isolated_data_dir / "kbs" / kb.id / bulk_reparse_report_store.REPORT_FILENAME
    assert path.exists()
    assert path.parent == pages_store._pages_dir(kb.id).parent
    assert json.loads(path.read_text(encoding="utf-8"))["kb_id"] == kb.id


def test_report_puts_estimate_and_actual_side_by_side(kb, monkeypatch):
    """预检估算与实测值并列 —— 背离时一眼看得出（AC 3 / story 26）。

    构造正是 #91 的形状：预检说"要烧 7 页"，实测说"0 页 OCR、7 页 unknown"。
    报告不拦截，只如实并排呈现。
    """
    _add_doc(kb.id, "diverge.pdf", page_count=7, content_hash="h_div")
    svc = _stub_reparse(monkeypatch, on_parse=lambda d: pages_store.save_pages(kb.id, d.id, _pages(7)))

    report = _run_and_load_report(kb.id, svc, concurrency=1)

    assert report["estimated_ocr_pages"] == 7
    assert report["actual_ocr_pages"] == 0
    assert report["actual_pages_by_source"] == {svc.SOURCE_UNKNOWN: 7}


def test_report_done_entries_carry_id_name_reason_and_source(kb, monkeypatch):
    """done 明细带 doc id / 原始文件名 / 入选原因 / 解析来源（AC 4）。"""
    doc = _add_doc(kb.id, "done.pdf", page_count=2, content_hash="h_done")

    def _parse(d):
        pages_store.save_pages(kb.id, d.id, _pages(2))
        _write_cache_entry("h_done", source="paddleocr")

    svc = _stub_reparse(monkeypatch, on_parse=_parse)
    report = _run_and_load_report(kb.id, svc, concurrency=1)

    assert report["done"] == [
        {
            "doc_id": doc.id,
            "original_name": "done.pdf",
            "reason": svc.REASON_NOT_EMBEDDED,
            "source": "paddleocr",
            "pages": 2,
        }
    ]


def test_report_failed_entries_carry_the_failure_reason(kb, monkeypatch):
    """failed 明细带 doc id / 原始文件名 / 失败原因串（AC 4）。"""
    bad = _add_doc(kb.id, "bad.pdf", page_count=2)
    svc = _stub_reparse(monkeypatch, outcomes={bad.id: "raise"})

    report = _run_and_load_report(kb.id, svc, concurrency=1)

    assert len(report["failed"]) == 1
    entry = report["failed"][0]
    assert entry["doc_id"] == bad.id
    assert entry["original_name"] == "bad.pdf"
    assert "RuntimeError" in entry["reason"]


def test_report_lists_skipped_docs_instead_of_dropping_them(kb, monkeypatch):
    """超页数上限的 doc 出现在报告里，带原因与页数 —— 跳过不许静默（AC 5）。"""
    from services import bulk_reparse_service as _svc

    huge = _add_doc(kb.id, "huge.pdf", page_count=_svc.PAGE_LIMIT + 20)
    small = _add_doc(kb.id, "small.pdf", page_count=2)
    svc = _stub_reparse(monkeypatch, on_parse=lambda d: pages_store.save_pages(kb.id, d.id, _pages(2)))

    report = _run_and_load_report(kb.id, svc, concurrency=1)

    assert report["skipped"] == [
        {
            "doc_id": huge.id,
            "original_name": "huge.pdf",
            "reason": svc.SKIP_REASON_PAGE_LIMIT,
            "page_count": svc.PAGE_LIMIT + 20,
        }
    ]
    assert report["counts"] == {"done": 1, "failed": 0, "skipped": 1}
    assert [e["doc_id"] for e in report["done"]] == [small.id]




def test_report_forced_field_promoted_to_top_level(kb, monkeypatch):
    """强制（--force）重解析是运维的强意图信号，报告必须一眼可见 —— 深埋在 preflight 块里没用。"""
    _add_doc(kb.id, "forced.pdf", page_count=2, pages=_pages(2))
    svc = _stub_reparse(monkeypatch, on_parse=lambda d: pages_store.save_pages(kb.id, d.id, _pages(2)))

    report = _run_and_load_report(kb.id, svc, concurrency=1, forced=True, force=True)

    assert report["forced"] is True

def test_report_records_run_shape_and_timestamps(kb, monkeypatch):
    """起止时间、耗时、并发、是否 force —— 复盘不依赖终端 scrollback（AC 3 / story 32）。"""
    _add_doc(kb.id, "t.pdf", page_count=2, embedding_status="embedded", pages=_pages(2))
    svc = _stub_reparse(monkeypatch, on_parse=lambda d: pages_store.save_pages(kb.id, d.id, _pages(2)))

    report = _run_and_load_report(kb.id, svc, concurrency=3, forced=True, force=True)

    assert report["forced"] is True
    assert report["concurrency"] == 3
    assert report["started_at"] <= report["finished_at"]
    assert report["duration_seconds"] >= 0
    assert report["target_count"] == 1


def test_report_preflight_block_explains_why_each_doc_is_a_target(kb, monkeypatch):
    """预检清单保留每篇的入选原因与缓存状态（AC 3 —— 目标集是报告的一部分）。"""
    doc = _add_doc(kb.id, "why.pdf", page_count=2, content_hash="h_why")
    _write_cache_entry("h_why", source="paddleocr")
    svc = _stub_reparse(monkeypatch, on_parse=lambda d: pages_store.save_pages(kb.id, d.id, _pages(2)))

    report = _run_and_load_report(kb.id, svc, concurrency=1)

    assert report["preflight"]["cached_docs"] == 1
    assert report["preflight"]["uncached_docs"] == 0
    assert report["preflight"]["targets"] == [
        {
            "doc_id": doc.id,
            "original_name": "why.pdf",
            "page_count": 2,
            "reason": svc.REASON_NOT_EMBEDDED,
            "cache_state": paddleocr_cache.CACHE_STATE_HIT,
        }
    ]


def test_latest_run_overwrites_the_previous_report(kb, monkeypatch):
    """只留最近一次；历史归档明确 out of scope（AC 6）。"""
    first = _add_doc(kb.id, "first.pdf", page_count=2)
    svc = _stub_reparse(monkeypatch, on_parse=lambda d: pages_store.save_pages(kb.id, d.id, _pages(2)))

    report_1 = _run_and_load_report(kb.id, svc, concurrency=1)
    assert [e["doc_id"] for e in report_1["done"]] == [first.id]

    second = _add_doc(kb.id, "second.pdf", page_count=2)
    report_2 = _run_and_load_report(kb.id, svc, concurrency=1)

    assert [e["doc_id"] for e in report_2["done"]] == [second.id]


def test_no_report_before_the_first_run(kb):
    """从没跑过批量 → 读不到报告（#111 的报告端点据此返回 404）。"""
    assert bulk_reparse_report_store.load_report(kb.id) is None


def test_corrupt_report_reads_as_missing(kb, isolated_data_dir):
    """报告文件损坏 → 降级为"没有报告"，不让复盘路径抛异常。"""
    path = isolated_data_dir / "kbs" / kb.id / bulk_reparse_report_store.REPORT_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert bulk_reparse_report_store.load_report(kb.id) is None


# ── layout 非空回归：#86 症状的直接防线（story 46 / AC 7）─────────────────────────


def test_every_target_doc_has_non_empty_layout_after_a_bulk_run(kb, monkeypatch):
    """批量跑完后，每一篇目标 doc 的 ``pages/{doc_id}.json`` 的 ``layout`` 必须非空。

    这是 #86 "chip 预览显示未解析" 症状的直接防线，也是本 ticket 唯一走**真实**
    ``reparse_service`` 链路的测试：只桩掉解析器（``parse_document``）与向量索引
    （``index_document`` / ``remove_document``），``pages_store.save_pages`` 用真的 ——
    断言的是**落盘产物**，不是 mock 的调用次数。
    """
    from unittest.mock import patch

    from core.parse_document import Block, PageLayout, PageText, ParseResult
    from services import bulk_reparse_service as svc

    monkeypatch.setattr(svc, "_POLL_INTERVAL_S", 0.01)
    docs = [_add_doc(kb.id, f"scan{i}.pdf", page_count=2, content_hash=f"h_{i}") for i in range(3)]

    parsed = ParseResult(
        by_page=[PageText(page=0, text="页面文本" * 20)],
        full_text="页面文本" * 20,
        layout=[PageLayout(page=0, blocks=[Block(block_order=0, block_label="text", block_content="块")])],
    )

    with patch("services.reparse_service.parse_document", return_value=parsed), \
         patch("services.reparse_service.index_document"), \
         patch("services.reparse_service.remove_document"):
        result = svc.run_bulk_reparse(kb.id, svc.list_target_docs(kb.id), concurrency=2)

    assert sorted(result.done) == sorted(d.id for d in docs), result.failed
    for doc in docs:
        pages = pages_store.load_pages(kb.id, doc.id)
        assert pages is not None, f"{doc.original_name} 没有落盘 pages 文件"
        assert pages["layout"], f"{doc.original_name} 的 layout 为空 —— #86 症状复发"


# ── CLI：报告的终端视图（薄 wrapper，只测渲染）────────────────────────────────


def test_cli_summary_shows_estimated_next_to_actual_and_the_report_path(kb, monkeypatch, capsys):
    """CLI 摘要把预估与实测并排打出来，并指向落盘的报告。

    CLI 不是独立 seam（逻辑全在 service），但"操作员跑完看到什么"是 #110 的
    交付面：#91 那次误报之所以骗过人，正是因为终端只印了预估那一个数字。
    """
    import importlib.util
    from pathlib import Path

    _add_doc(kb.id, "cli.pdf", page_count=4, content_hash="h_cli")

    path = Path(__file__).resolve().parent.parent / "scripts" / "bulk_reparse.py"
    spec = importlib.util.spec_from_file_location("bulk_reparse_cli_report", path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setattr(cli, "kb_repo", kb_repo)

    def _parse(d):
        pages_store.save_pages(kb.id, d.id, _pages(4))
        _write_cache_entry("h_cli", source="paddleocr")

    _stub_reparse(monkeypatch, on_parse=_parse)

    exit_code = cli.bulk_reparse(kb.id, dry_run=False, concurrency=1, skip_confirm=True)
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "预估 4 页 / 实测 4 页" in out
    assert "paddleocr" in out
    assert bulk_reparse_report_store.REPORT_FILENAME in out


# ── 报告与实测一致性：snapshot 必须在 run 之前取 ─────────────────────────────


def test_report_preflight_uses_the_pre_run_snapshot_not_a_post_run_reread(kb, monkeypatch):
    """回归 #91 的反向翻版：跑前 ``uncached``、跑后 ``cached`` 的 doc，
    preflight 块必须如实记 ``uncached`` —— **不能** 跑完再回读，那样会与
    ``actual_pages_by_source`` 里的 ``cache_hit`` 互相打脸。
    """
    doc = _add_doc(kb.id, "migrate.pdf", page_count=3, content_hash="h_migrate")

    def _parse(d):
        pages_store.save_pages(kb.id, d.id, _pages(3))
        _write_cache_entry("h_migrate", source="paddleocr")  # 跑前没条目，跑后写入

    svc = _stub_reparse(monkeypatch, on_parse=_parse)
    report = _run_and_load_report(kb.id, svc, concurrency=1)

    # 实测：跑前无 → 跑后写 paddleocr → 走真 OCR 桶
    assert report["actual_pages_by_source"] == {"paddleocr": 3}
    assert report["preflight"]["cached_docs"] == 0
    # 关键：preflight 块的 cache_state 不能"事后变聪明"
    preflight_target = report["preflight"]["targets"][0]
    assert preflight_target["doc_id"] == doc.id
    assert preflight_target["cache_state"] == paddleocr_cache.CACHE_STATE_MISS, (
        "preflight 必须用 run 触发前的快照；事后回读会与实测块矛盾"
    )


def test_report_surfaces_failed_docs_in_the_source_breakdown(kb, monkeypatch):
    """失败篇入显式 ``failed`` 桶 —— 不入这一桶就会让"claim done 但 burn 0"
    的指纹（#90 类）凭空消失，与"差异要被看见"（story 26）正面冲突。
    """
    bad = _add_doc(kb.id, "bad.pdf", page_count=2, content_hash="h_bad")
    _stub_reparse(monkeypatch, outcomes={bad.id: "failed"})
    report = _run_and_load_report(kb.id, svc_for := _stub_reparse(monkeypatch, outcomes={bad.id: "failed"}), concurrency=1)

    assert report["actual_docs_by_source"] == {"failed": 1}
    assert report["actual_pages_by_source"] == {}
    assert report["actual_ocr_pages"] == 0
    assert report["failed"][0]["doc_id"] == bad.id


# ── failed 原因传播：异步失败也要带可读理由（AC4 / story 30）────────────────────


def test_async_failure_message_from_kb_state_surfaces_in_report(kb, monkeypatch):
    """``reparse_document`` 异步失败 → ``kb.index_current_doc`` 写入错误串；
    ``_wait_for_terminal`` 把它读出来，报告的 failed.reason **必须** 带上原文。
    """
    from services import bulk_reparse_service as svc

    monkeypatch.setattr(svc, "_POLL_INTERVAL_S", 0.01)
    bad = _add_doc(kb.id, "async_bad.pdf", page_count=2)

    def _async_fail(doc_id: str, *, caller_manages_kb_status: bool = False):
        # 模拟 reparse_service 异步分支的 _mark_failed 写错误消息
        k = kb_repo.get(bad.kb_id)
        k.index_current_doc = "reparse 错误: 解析 PaddleOCR API 504"
        kb_repo.update(k)
        doc = doc_repo.get_doc(bad.kb_id, doc_id)
        doc.embedding_status = "failed"
        doc_repo._save_doc_meta(doc)

    monkeypatch.setattr(svc, "reparse_document", _async_fail)
    report = _run_and_load_report(kb.id, svc, concurrency=1)

    assert report["failed"][0]["doc_id"] == bad.id
    assert "PaddleOCR API 504" in report["failed"][0]["reason"]
