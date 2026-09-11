"""``core.truncated_doc_detector`` 契约测试（spec #173 / issue #179）。

issue #93 教训:同一个问题第二份实现必然分叉(三条选取规则的第三条
就是这么丢的)。本模块是"哪些 doc 被服务端静默截短过"的**唯一**判定
实现,本文件盯住五条判据契约:

1. ``embedding_status == "embedded"`` —— 不查 failed / truncated /
   pending_index (它们早就在待重解析文档名单里,重复报告无价值)。
2. 缓存条目 ``source ∈ {"paddleocr", "paddleocr_split"}`` —— PyMuPDF
   路径不截断,先把它挡掉避免误报。
3. ``physical = doc.page_count or pdf_page_count(doc.file_path)``;
   ``physical`` 为 ``None`` → 跳过(读不到就不下结论,绝不为读不到
   的信息发明坏结论 —— spec AC#42 的精神)。
4. ``len(pages_store.load_pages(...)["by_page"]) < physical``。

helper 沿用 :mod:`tests.test_bulk_reparse_service` 的
``_add_doc`` / ``_write_cache_entry`` / ``pages_store.save_pages``
三件套,零网络;判据 #3 的"源 PDF 可读 → ``pdf_page_count`` 回落"
fixture 用 ``_real_pdf`` 现场造一个真 pymupdf PDF 替换 ``doc.file_path``
(dummy 19 字节 PDF 在 ``pdf_page_count`` 里读不出页数,正好对应
"读不到 → 跳过"分支)。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import storage.doc_repo as doc_repo
import storage.kb_repo as kb_repo
from core import paddleocr_cache, pages_store
from core.parse_document import pdf_page_count
from models.document import KBDocument
from models.knowledge_base import KnowledgeBase


# ── PyMuPDF 探测(spec #179 验收 #5 用真 PDF 替换 ``doc.file_path``,
#    无 wheel 则整文件 skip,与 tests/test_pdf_splitter.py 同口径)────────────


try:
    import pymupdf  # type: ignore[import-not-found]
    _HAVE_PYMUPDF = True
except Exception:
    pymupdf = None  # type: ignore[assignment]
    _HAVE_PYMUPDF = False


pytestmark = [
    pytest.mark.skipif(
        not _HAVE_PYMUPDF,
        reason="pymupdf wheel not installed (issue #99 / #176)",
    ),
]


# ── fixture / helper:隔离数据目录 + 造 KB + 造 doc + 写真缓存条目 ─────────────────


@pytest.fixture
def isolated_data_dir(tmp_path):
    """数据目录隔离由 conftest 的 per-test ``AUDIT_DATA_DIR`` 保证（issue #137）。

    存储层 ``get_data_dir()`` 每次调用解析 env,无需再 monkeypatch 模块属性。
    保留此 fixture 只为给测试一个指向本用例数据目录的 Path。
    """
    return tmp_path


@pytest.fixture
def kb(isolated_data_dir):
    """一个空 KB。"""
    return kb_repo.create(KnowledgeBase(id="kb_trunc", name="截短探测库", category="national"))


def _add_doc(
    kb_id: str,
    name: str,
    *,
    embedding_status: str = "embedded",
    page_count: int | None = 247,
    content_hash: str | None = None,
    pages: dict | None = None,
):
    """造一篇 doc(可选带 pages 文件 + content_hash)。返回 KBDocument。"""
    doc = doc_repo.save_doc(kb_id, name, b"%PDF-1.4 dummy " + name.encode(), "pdf")
    doc.embedding_status = embedding_status
    doc.page_count = page_count
    doc.content_hash = content_hash
    doc_repo._save_doc_meta(doc)
    if pages is not None:
        pages_store.save_pages(kb_id, doc.id, pages)
    return doc


def _pages(n: int) -> dict:
    """造一份 ``n`` 页的 pages 文件(每页 50 字符 + 一个 layout block)。"""
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
            "result": {"by_page": [{"page": 0, "text": ""}], "full_text": "", "layout": []},
        }),
        encoding="utf-8",
    )
    return path


def _real_pdf(kb_id: str, doc_id: str, *, n_pages: int) -> Path:
    """造一个真 ``n_pages`` 页 PDF,落盘到该 doc 的 ``file_path`` 同级。

    替换 ``doc.file_path`` → 落地一份能 ``pdf_page_count`` 读到 ``n_pages``
    的真 PDF,让判据 #3 的"源 PDF 可读 → 回落"路径可证。
    """
    import pymupdf

    doc_dir = Path(doc_repo._kb_docs_dir(kb_id))  # noqa: SLF001 — 同包内复用 helper
    doc_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = doc_dir / f"_real_{doc_id}.pdf"
    with pymupdf.open() as pdf:
        for _ in range(n_pages):
            pdf.new_page()
        pdf.save(str(pdf_path))
    assert pdf_page_count(str(pdf_path)) == n_pages, (
        f"test fixture sanity: 真 PDF 应是 {n_pages} 页,实际 {pdf_page_count(str(pdf_path))}"
    )
    return pdf_path


# ── 判据 #1:#93 教训,失败 / truncated / pending_index 都不再报告 ────────────────


def test_failed_doc_is_not_reported(kb):
    """判据 #1:``embedding_status="failed"`` 不报告 —— 已在待重解析名单。"""
    from core.truncated_doc_detector import find_truncated_docs

    _add_doc(
        kb.id, "failed.pdf",
        embedding_status="failed",
        page_count=247,
        content_hash="h_failed",
        pages=_pages(100),
    )
    _write_cache_entry("h_failed", source="paddleocr")

    assert find_truncated_docs(kb.id) == []


# ── 判据 #2:PyMuPDF 路径不截断,挡在外面避免误报 ────────────────────────────────


def test_pymupdf_doc_is_not_reported_even_when_parsed_pages_under_page_count(kb):
    """判据 #2 + #3:``pymupdf`` doc 即便 ``page_count is None`` 也不报告。

    缓存 ``source="pymupdf"`` 在判据 #2 就把它挡掉,根本不会落到 #3 的
    ``pdf_page_count`` 回落与 #4 的页数比较。
    """
    from core.truncated_doc_detector import find_truncated_docs

    _add_doc(
        kb.id, "textlayer.pdf",
        embedding_status="embedded",
        page_count=None,  # issue 明确用 page_count=None 作 fixture
        content_hash="h_mupdf",
        pages=_pages(50),
    )
    _write_cache_entry("h_mupdf", source="pymupdf")

    assert find_truncated_docs(kb.id) == []


# ── 判据 #3 + #4:健康 paddleocr 247 页 → 不报 ──────────────────────────────────


def test_healthy_247_page_paddleocr_doc_is_not_reported(kb):
    """判据 #3 + #4:健康 247 页 paddleocr 文档(247 by_page 条目)→ 不报。"""
    from core.truncated_doc_detector import find_truncated_docs

    _add_doc(
        kb.id, "healthy.pdf",
        embedding_status="embedded",
        page_count=247,
        content_hash="h_healthy",
        pages=_pages(247),
    )
    _write_cache_entry("h_healthy", source="paddleocr")

    assert find_truncated_docs(kb.id) == []


# ── 验收 #2:截断 100/247 paddleocr 文档 → 报,且 parsed_pages/physical_pages 对 ──


def test_truncated_100_of_247_paddleocr_doc_is_reported(kb):
    """验收 #2:截断 100/247 paddleocr 文档 → 报,
    ``parsed_pages == 100`` / ``physical_pages == 247``。"""
    from core.truncated_doc_detector import find_truncated_docs

    _add_doc(
        kb.id, "truncated.pdf",
        embedding_status="embedded",
        page_count=247,
        content_hash="h_trunc",
        pages=_pages(100),
    )
    _write_cache_entry("h_trunc", source="paddleocr")

    found = find_truncated_docs(kb.id)

    assert len(found) == 1
    t = found[0]
    assert t.parsed_pages == 100
    assert t.physical_pages == 247


# ── 验收 #2 的 paddleocr_split 分桶(spec #173:超 PADDLEOCR_PAGE_LIMIT 拆分
#    解析同样会出现服务端截短,故 source ∈ {paddleocr, paddleocr_split} 都是
#    截短判定对象。漏掉 split 一支会让"返修工具只看 paddleocr 不看 split",
#    与 #178 翻转的 #90 错法同源。)─────────────────────────────────────


def test_truncated_paddleocr_split_doc_is_reported(kb):
    """``source="paddleocr_split"`` + 100/247 → 报。

    与 :func:`test_truncated_100_of_247_paddleocr_doc_is_reported` 同口径,
    仅替换 source 一字:这条用例锁定"判据 #2 的字面量集合不被悄悄去掉 split"。
    """
    from core.truncated_doc_detector import find_truncated_docs

    _add_doc(
        kb.id, "split.pdf",
        embedding_status="embedded",
        page_count=247,
        content_hash="h_split",
        pages=_pages(100),
    )
    _write_cache_entry("h_split", source="paddleocr_split")

    found = find_truncated_docs(kb.id)

    assert len(found) == 1
    t = found[0]
    assert t.parsed_pages == 100
    assert t.physical_pages == 247


# ── 验收 #5:page_count=None + 源 PDF 可读 → pdf_page_count 回落并判定截断 ────────


def test_page_count_none_falls_back_to_pdf_page_count_and_detects_truncation(kb):
    """验收 #5:``page_count=None`` + 源 PDF 真有 247 页 → 回落读到 247,
    解析了 100 页 → 报。

    这是判据 #3 的回落路径(``doc.page_count or pdf_page_count(...)``):
    元数据缺页数但源 PDF 还在 → 绝不能简单放弃结论。
    """
    from core.truncated_doc_detector import find_truncated_docs

    doc = _add_doc(
        kb.id, "no_meta.pdf",
        embedding_status="embedded",
        page_count=None,
        content_hash="h_fallback",
        pages=_pages(100),
    )
    _write_cache_entry("h_fallback", source="paddleocr")

    real_pdf = _real_pdf(kb.id, doc.id, n_pages=247)
    doc.file_path = str(real_pdf)
    doc_repo._save_doc_meta(doc)

    found = find_truncated_docs(kb.id)

    assert len(found) == 1
    t = found[0]
    assert t.parsed_pages == 100
    assert t.physical_pages == 247
