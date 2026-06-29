"""vector_search 测试。

通过 fake_models 注入确定性假 embedder，**不加载 bge-m3** 测向量主路径
（index_document → vec_search 往返）；ripgrep 路径用 monkeypatch _run_rga
测解析逻辑（无需 rga/rg 二进制）。
"""
import os
import shutil

import pytest

from services import vector_search
from services.vector_search import (
    _format_kb_results,
    index_document,
    vec_search,
    search,
    get_kb_content,
    get_kb_content_for_audit,
    search_by_keywords,
    search_doc_by_text,
)


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    data_dir = os.environ["AUDIT_DATA_DIR"]
    if os.path.exists(data_dir):
        shutil.rmtree(data_dir)


@pytest.fixture(autouse=True)
def _use_fake_models(fake_models):
    """本文件统一注入假模型，不加载 bge-m3。"""
    yield


# ── 纯函数：结果格式化 ─────────────────────────────────────────────────────────


def test_format_kb_results_basic():
    out = _format_kb_results([
        {"doc_source": "CJJ101-2016", "clause_number": "3.2.1", "content": "条文原文内容"},
    ])
    assert "【知识库参考依据（向量检索）】" in out
    assert "【CJJ101-2016】" in out
    assert "第3.2.1条" in out
    assert "条文原文内容" in out


def test_format_kb_results_section_when_no_clause():
    """无条款号时用 section_path 作标签（不出现"第X条"）。"""
    out = _format_kb_results([{"doc_source": "GB/T X", "section_path": "第二章", "content": "c"}])
    assert "第二章" in out
    assert "条" not in out  # 无 clause_number → 不出现"第X条"标签


def test_format_kb_results_empty():
    assert _format_kb_results([]) == ""


def test_format_kb_results_custom_prefix():
    out = _format_kb_results([{"doc_source": "S", "content": "c"}], prefix="参考标准依据")
    assert out.startswith("【参考标准依据】")


# ── 向量主路径往返（fake embedder，无 bge-m3）──────────────────────────────────


def test_index_and_vec_search_round_trip(tmp_path):
    """index_document → vec_search 往返：建索引后能搜到（结构断言，不依赖语义）。"""
    f = tmp_path / "std.md"
    f.write_text("# 技术标准\n\n本标准规定质保期不少于24个月，验收应符合 GB/T 20145 的要求。")

    kb_id = "vs_kb_roundtrip"
    index_document(kb_id, "d1", str(f), source_name="技术标准.md")

    results = vec_search([kb_id], "质保期", top_k=5)
    assert len(results) >= 1
    r = results[0]
    assert r["kb_id"] == kb_id
    assert r.get("source") == "vec_search"
    assert r.get("content") is not None


def test_search_wrapper_delegates_to_vec_search(tmp_path):
    """search() 是 vec_search 的兼容包装。"""
    f = tmp_path / "d.md"
    f.write_text("网络安全等级保护基本要求 GB/T 22239 全文内容规定。")
    kb_id = "vs_kb_wrapper"
    index_document(kb_id, "d1", str(f), source_name="d.md")

    results = search([kb_id], "安全", max_results=5)
    assert len(results) >= 1


def test_vec_search_empty_inputs():
    assert vec_search([], "q") == []
    assert vec_search(["kb1"], "") == []


# ── KB 内容获取（被审核调用）────────────────────────────────────────────────


def test_get_kb_content_formats_results(monkeypatch):
    monkeypatch.setattr(vector_search, "vec_search",
                        lambda *a, **k: [{"doc_source": "GB/T 20145", "content": "条文"}])
    out = get_kb_content(["kb1"], "q")
    assert "参考标准依据（向量检索）" in out
    assert "GB/T 20145" in out


def test_get_kb_content_empty_returns_fallback(monkeypatch):
    monkeypatch.setattr(vector_search, "vec_search", lambda *a, **k: [])
    assert get_kb_content(["kb1"], "q") == "未找到相关标准依据。"


def test_get_kb_content_for_audit_swallows_exception(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("vec down")
    monkeypatch.setattr(vector_search, "get_kb_content", boom)
    assert get_kb_content_for_audit(["kb1"], "q") == "未找到相关标准依据。"


# ── search_by_keywords：0.35 阈值策略 ──────────────────────────────────────────


def test_search_by_keywords_high_relevance_uses_vector(monkeypatch):
    """relevance > 0.35 → 用向量结果格式化（不走文本降级）。"""
    monkeypatch.setattr(vector_search, "vec_search",
                        lambda *a, **k: [{"relevance": 0.8, "doc_source": "S", "content": "c"}])
    monkeypatch.setattr(vector_search, "_text_search_fallback", lambda *a, **k: "FALLBACK")
    out = search_by_keywords(["kb1"], ["kw"])
    assert "S" in out
    assert "FALLBACK" not in out


def test_search_by_keywords_low_relevance_falls_back(monkeypatch):
    """relevance 全 <= 0.35 → 降级到文本搜索。"""
    monkeypatch.setattr(vector_search, "vec_search",
                        lambda *a, **k: [{"relevance": 0.1, "doc_source": "S"}])
    monkeypatch.setattr(vector_search, "_text_search_fallback", lambda *a, **k: "FALLBACK")
    assert search_by_keywords(["kb1"], ["kw"]) == "FALLBACK"


def test_search_by_keywords_uses_topic_name_as_query(monkeypatch):
    """topic_name 非空时优先用作 query。"""
    captured = {}
    def fake_vec(kb_ids, query, top_k=6):
        captured["query"] = query
        return []
    monkeypatch.setattr(vector_search, "vec_search", fake_vec)
    monkeypatch.setattr(vector_search, "_text_search_fallback", lambda *a, **k: "")
    search_by_keywords(["kb1"], [], topic_name="质保期主题")
    assert captured["query"] == "质保期主题"


# ── search_doc_by_text：rga 输出解析（monkeypatch _run_rga，无需二进制）─────────


def test_search_doc_by_text_parses_match_line(tmp_path, monkeypatch):
    """rga 匹配行（:分隔）被正确解析为 {doc_id, kb_id, content}。"""
    import storage.doc_repo as doc_repo
    import storage.kb_repo as kb_repo
    from models.knowledge_base import KnowledgeBase

    kb_id = "vs_kb_text"
    kb_repo.update(KnowledgeBase(id=kb_id, name="t", category="national"))

    # 建一篇真实落盘的 doc（search_doc_by_text 用 doc.file_path 做 rga 输出匹配）
    doc = doc_repo.save_doc(kb_id, "std.md", "GB/T 20145-2006 条文内容".encode(), "md")
    resolved_fp = str(__import__("pathlib").Path(doc.file_path).resolve())

    # 模拟 rga 输出：匹配行 ":行号:内容"
    canned = f"{resolved_fp}:12:应符合 GB/T 20145-2006 的光生物安全性要求"
    monkeypatch.setattr(vector_search, "_run_rga", lambda kw, paths: canned)

    hits = search_doc_by_text("GB/T 20145", [kb_id])
    assert len(hits) == 1
    assert hits[0]["doc_id"] == doc.id
    assert hits[0]["kb_id"] == kb_id
    assert "GB/T 20145" in hits[0]["content"]


def test_search_doc_by_text_empty_inputs():
    assert search_doc_by_text("", ["kb1"]) == []
    assert search_doc_by_text("kw", []) == []


def test_search_doc_by_text_no_paths_returns_empty(monkeypatch):
    """KB 无文档目录 → _get_kb_search_paths 空 → 返回 []。"""
    monkeypatch.setattr(vector_search, "_run_rga", lambda kw, paths: "should not be called")
    assert search_doc_by_text("kw", ["nonexistent_kb"]) == []
