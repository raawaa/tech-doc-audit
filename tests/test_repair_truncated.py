"""``scripts/repair_truncated.py`` 契约测试（spec #173 / issue #180）。

薄 wrapper 契约：检测全在 :func:`core.truncated_doc_detector.find_truncated_docs`，
修复动作全在 :func:`services.bulk_reparse_service.reparse_one` —— CLI 只负责
``.env`` 加载、argparse、终端渲染、退出码与 ``truncated_report.json`` 落盘。

与 :mod:`tests.test_bulk_reparse_service` 同款 ``cli_module`` fixture
（:func:`importlib.util.spec_from_file_location` 加载 scripts 目录里的 CLI），
``services.bulk_reparse_service.reparse_document`` 被 monkeypatch 掉 —— 不触发
真实 OCR 与向量索引写入。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

import storage.doc_repo as doc_repo
import storage.kb_repo as kb_repo
from core import paddleocr_cache, pages_store, truncated_report_store
from models.knowledge_base import KnowledgeBase


# ── fixture / helper：隔离数据目录 + 造 KB + 造 truncated doc ─────────────────────


@pytest.fixture
def isolated_data_dir(tmp_path):
    """数据目录隔离由 conftest 的 per-test ``AUDIT_DATA_DIR`` 保证（issue #137）。

    存储层 ``get_data_dir()`` 每次调用解析 env，无需再 monkeypatch 模块属性。
    保留此 fixture 与 :mod:`tests.test_bulk_reparse_service` 同源。
    """
    return tmp_path


@pytest.fixture
def kb(isolated_data_dir):
    return kb_repo.create(KnowledgeBase(id="kb_repair_trunc", name="截短返修库", category="national"))


def _add_doc(
    kb_id: str,
    name: str,
    *,
    embedding_status: str = "embedded",
    page_count: int | None = None,
    content_hash: str | None = None,
    pages: dict | None = None,
):
    """造一篇 doc（可选带 pages 文件 + content_hash）。返回 KBDocument。"""
    doc = doc_repo.save_doc(kb_id, name, b"%PDF-1.4 dummy " + name.encode(), "pdf")
    doc.embedding_status = embedding_status
    doc.page_count = page_count
    doc.content_hash = content_hash
    doc_repo._save_doc_meta(doc)
    if pages is not None:
        pages_store.save_pages(kb_id, doc.id, pages)
    return doc


def _pages(n: int) -> dict:
    return {
        "by_page": [{"page": i, "text": "x" * 50} for i in range(n)],
        "full_text": "x" * 50,
        "layout": [{"page": i, "blocks": [{"block_order": 0}]} for i in range(n)],
    }


def _write_cache_entry(content_hash: str, *, source: str) -> Path:
    """按 ``(content_hash, model_version)`` 写一条缓存条目。"""
    path = (
        paddleocr_cache.get_cache_dir()
        / f"{content_hash}_{paddleocr_cache._MODEL_VERSION}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "version": paddleocr_cache._MODEL_VERSION,
            "source": source,
            "result": {},
        }),
        encoding="utf-8",
    )
    return path


def _real_pdf(kb_id: str, doc_id: str, *, n_pages: int) -> Path:
    """造一个真 ``n_pages`` 页 PDF，替换 ``doc.file_path`` 让 ``pdf_page_count``
    读到真实页数（``%PDF-1.4 dummy`` 字节读不出页数，正对应"读不到"分支）。

    无 pymupdf wheel 则 skip，与 :mod:`tests.test_truncated_doc_detector` 同口径。
    """
    try:
        import pymupdf
    except Exception:
        pytest.skip("pymupdf wheel not installed")
    from core.parse_document import pdf_page_count

    doc_dir = Path(doc_repo._kb_docs_dir(kb_id))  # noqa: SLF001 — 同包内复用 helper
    doc_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = doc_dir / f"_real_{doc_id}.pdf"
    with pymupdf.open() as pdf:
        for _ in range(n_pages):
            pdf.new_page()
        pdf.save(str(pdf_path))
    assert pdf_page_count(str(pdf_path)) == n_pages, (
        f"test fixture sanity：真 PDF 应是 {n_pages} 页，"
        f"实际 {pdf_page_count(str(pdf_path))}"
    )
    return pdf_path


def _add_truncated_doc(kb_id: str, name: str, *, parsed: int = 100, physical: int = 247):
    """造一个 truncated doc：嵌入成功但只解析了 ``parsed`` 页（物理 ``physical`` 页）。

    同时落一份真 PDF 与 ``source=paddleocr`` 缓存条目，让
    :func:`core.truncated_doc_detector.find_truncated_docs` 四条判据全部命中。
    """
    content_hash = "h_" + name.replace(".", "_")
    doc = _add_doc(
        kb_id, name,
        embedding_status="embedded",
        page_count=physical,
        content_hash=content_hash,
        pages=_pages(parsed),
    )
    _write_cache_entry(content_hash, source="paddleocr")
    real_pdf = _real_pdf(kb_id, doc.id, n_pages=physical)
    doc.file_path = str(real_pdf)
    doc_repo._save_doc_meta(doc)
    return doc


def _stub_reparse_document(monkeypatch, kb_id: str, *, final_pages: int):
    """把 :func:`services.bulk_reparse_service.reparse_document` patch 成
    "立刻写完整 pages + 标 embedded"，模拟真实 reparse 成功路径。

    返回 ``bulk_reparse_service`` 模块（便于调用方直接访问其他符号）。

    注意 patch 的是 ``services.bulk_reparse_service`` 模块里的
    ``reparse_document`` —— :func:`services.bulk_reparse_service.reparse_one`
    内部以模块全局名字引用它，patch 这一处的全局绑定即可拦截（与
    :func:`tests.test_bulk_reparse_service._stub_reparse` 同口径）。
    """
    from services import bulk_reparse_service as svc

    monkeypatch.setattr(svc, "_POLL_INTERVAL_S", 0.01)

    def _fake(doc_id: str, **_kwargs):
        doc = doc_repo.find_doc_by_id(doc_id)
        if doc is not None:
            pages_store.save_pages(kb_id, doc_id, _pages(final_pages))
            doc.embedding_status = "embedded"
            doc_repo._save_doc_meta(doc)
        return {"status": "pending_index", "doc_id": doc_id}

    monkeypatch.setattr(svc, "reparse_document", _fake)
    return svc


# ── cli_module fixture ──────────────────────────────────────────────────────────


@pytest.fixture
def cli_module():
    """按文件路径加载 ``scripts/repair_truncated.py``（scripts 不是包）。"""
    path = Path(__file__).resolve().parent.parent / "scripts" / "repair_truncated.py"
    spec = importlib.util.spec_from_file_location("repair_truncated_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── 验收 #1：--dry-run 无副作用 ────────────────────────────────────────────────


def test_repair_truncated_dry_run_produces_report_without_modifying_state(
    kb, cli_module, isolated_data_dir,
):
    """``--dry-run`` 在含一篇截短 doc 的 KB 上产出 ``truncated_report.json``，
    且 ``embedding_status`` 与 ``pages/{doc_id}.json`` 字节不变（验收 #1）。

    "字节不变"是 dry-run 契约最强的取证 —— 不是"看起来没改"，而是 file bytes
    同一份。``truncated_report.json`` 是 sibling 文件，写它不会改 pages 字节。
    """
    doc = _add_truncated_doc(kb.id, "truncated.pdf", parsed=100, physical=247)

    # 字节快照：dry-run 前抓一份，run 完比对
    before_status = doc_repo.get_doc(kb.id, doc.id).embedding_status
    pages_path = isolated_data_dir / "kbs" / kb.id / "pages" / f"{doc.id}.json"
    before_pages_bytes = pages_path.read_bytes()

    exit_code = cli_module.repair_truncated(kb_id=kb.id, dry_run=True)

    assert exit_code == 2, f"dry-run 必须返回 2，实际 {exit_code}"

    # doc 状态不变
    final = doc_repo.get_doc(kb.id, doc.id)
    assert final.embedding_status == before_status, (
        f"dry-run 不许改 embedding_status，实际 "
        f"{final.embedding_status} != {before_status}"
    )

    # pages 字节不变
    after_pages_bytes = pages_path.read_bytes()
    assert before_pages_bytes == after_pages_bytes, (
        "dry-run 不许改 pages/{doc_id}.json 字节"
    )

    # 报告落盘且 found 明细含本 doc
    report_path = isolated_data_dir / "kbs" / kb.id / truncated_report_store.REPORT_FILENAME
    assert report_path.exists(), (
        f"dry-run 必须写出 {truncated_report_store.REPORT_FILENAME}，实际 {report_path} 不存在"
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["dry_run"] is True
    assert report["kb_id"] == kb.id
    assert any(t["doc_id"] == doc.id for t in report["found"]), (
        f"report found 应含 doc {doc.id}，实际 {report['found']}"
    )


# ── 验收 #2：非 dry-run 跑完后 embedded + by_page 满 ─────────────────────────────


def test_repair_truncated_repair_path_ends_with_embedded_and_full_pages(
    kb, cli_module, monkeypatch,
):
    """非 dry-run：被识别出的 doc 跑一遍后 ``embedding_status == "embedded"``
    且 ``len(pages["by_page"]) == physical_pages``（验收 #2）。

    ``reparse_document`` 被 patch 成"立刻写 247 页 + 标 embedded"，
    模拟真实 reparse 成功路径（不触发 OCR 与向量索引写入）。
    """
    doc = _add_truncated_doc(kb.id, "truncated.pdf", parsed=100, physical=247)
    _stub_reparse_document(monkeypatch, kb.id, final_pages=247)

    exit_code = cli_module.repair_truncated(kb_id=kb.id, dry_run=False)

    assert exit_code == 0, f"全成功应返回 0，实际 {exit_code}"
    final = doc_repo.get_doc(kb.id, doc.id)
    assert final.embedding_status == "embedded"
    pages = pages_store.load_pages(kb.id, doc.id)
    assert pages is not None, "reparse 成功应落盘 pages 文件"
    assert len(pages["by_page"]) == 247, (
        f"修复后 by_page 长度应等于 physical_pages (247)，"
        f"实际 {len(pages['by_page'])}"
    )


# ── 验收 #4：有失败 → 退出码 1 ─────────────────────────────────────────────────


def test_repair_truncated_failed_reparse_returns_exit_code_1(
    kb, cli_module, monkeypatch,
):
    """``reparse_document`` stub 让 doc 落到 ``failed`` → 退出码 1（验收 #4）。

    报告的 failed 明细要带 doc id 与失败原因串 —— 与 :mod:`tests.test_bulk_reparse_report`
    的 failed 明细契约同口径。
    """
    from services import bulk_reparse_service as svc

    doc = _add_truncated_doc(kb.id, "truncated.pdf", parsed=100, physical=247)
    monkeypatch.setattr(svc, "_POLL_INTERVAL_S", 0.01)

    def _fake(doc_id: str, **_kwargs):
        d = doc_repo.find_doc_by_id(doc_id)
        if d is not None:
            d.embedding_status = "failed"
            doc_repo._save_doc_meta(d)
        return {"status": "pending_index", "doc_id": doc_id}

    monkeypatch.setattr(svc, "reparse_document", _fake)

    exit_code = cli_module.repair_truncated(kb_id=kb.id, dry_run=False)

    assert exit_code == 1, f"有失败应返回 1，实际 {exit_code}"
    final = doc_repo.get_doc(kb.id, doc.id)
    assert final.embedding_status == "failed"

    # 报告 failed 明细带原因
    import os
    report_path = Path(os.environ["AUDIT_DATA_DIR"]) / "kbs" / kb.id / truncated_report_store.REPORT_FILENAME
    assert report_path.exists(), "非 dry-run 也应落盘报告（同套心智模型）"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert len(report["failed"]) == 1
    assert report["failed"][0]["doc_id"] == doc.id
    assert "failed" in report["failed"][0]["reason"]


# ── 验收 #3：不传 --kb-id → 扫描 data/kbs/* 全部 KB ────────────────────────────


def test_repair_truncated_scans_all_kbs_when_kb_id_is_omitted(
    cli_module, isolated_data_dir, monkeypatch,
):
    """不传 ``--kb-id`` 时扫描 ``data/kbs/*`` 全部 KB（验收 #3）。

    用 ``--dry-run`` 验证：每个含截短 doc 的 KB 都产出一份
    ``truncated_report.json``；不触发任何 reparse。
    """
    # 两个 KB，各含一篇截短 doc
    kb_a = kb_repo.create(KnowledgeBase(id="kb_repair_a", name="库A", category="national"))
    kb_b = kb_repo.create(KnowledgeBase(id="kb_repair_b", name="库B", category="national"))
    doc_a = _add_truncated_doc(kb_a.id, "a_truncated.pdf", parsed=50, physical=120)
    doc_b = _add_truncated_doc(kb_b.id, "b_truncated.pdf", parsed=30, physical=80)

    exit_code = cli_module.repair_truncated(kb_id=None, dry_run=True)

    assert exit_code == 2, f"dry-run 必须返回 2，实际 {exit_code}"

    # 两个 KB 都产报告
    report_a = isolated_data_dir / "kbs" / kb_a.id / truncated_report_store.REPORT_FILENAME
    report_b = isolated_data_dir / "kbs" / kb_b.id / truncated_report_store.REPORT_FILENAME
    assert report_a.exists(), f"{report_a} 应存在"
    assert report_b.exists(), f"{report_b} 应存在"

    # 两个 doc 都被列在对应 KB 的报告里
    rep_a = json.loads(report_a.read_text(encoding="utf-8"))
    rep_b = json.loads(report_b.read_text(encoding="utf-8"))
    assert any(t["doc_id"] == doc_a.id for t in rep_a["found"])
    assert any(t["doc_id"] == doc_b.id for t in rep_b["found"])
