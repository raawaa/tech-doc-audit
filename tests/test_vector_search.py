"""vector_search 测试。

通过 fake_models 注入确定性假 embedder，**不加载 bge-m3** 测向量主路径
（index_document → vec_search 往返）；ripgrep 路径用 monkeypatch _run_rga
测解析逻辑（无需 rga/rg 二进制）。
"""
import pytest

from services import vector_search
import storage.kb_repo as kb_repo
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


def test_index_and_vec_search_round_trip(tmp_path, seed_searchable_kb):
    """index_document → vec_search 往返：建索引后能搜到（结构断言，不依赖语义）。"""
    f = tmp_path / "std.md"
    f.write_text("# 技术标准\n\n本标准规定质保期不少于24个月，验收应符合 GB/T 20145 的要求。")

    kb_id = seed_searchable_kb("vs_kb_roundtrip")
    # 把 doc 关联到 KB 上（重建时按 document_ids 处理；不写不进 KB 会丢失）
    import storage.kb_repo as _kb_repo
    kb = _kb_repo.get(kb_id)
    kb.document_ids = ["d1"]
    _kb_repo.update(kb)

    index_document(kb_id, "d1", str(f), source_name="技术标准.md")

    results = vec_search([kb_id], "质保期", top_k=5)
    assert len(results) >= 1
    r = results[0]
    assert r["kb_id"] == kb_id
    assert r.get("source") == "vec_search"
    assert r.get("content") is not None


def test_search_wrapper_delegates_to_vec_search(tmp_path, seed_searchable_kb):
    """search() 是 vec_search 的兼容包装。"""
    f = tmp_path / "d.md"
    f.write_text("网络安全等级保护基本要求 GB/T 22239 全文内容规定。")
    kb_id = seed_searchable_kb("vs_kb_wrapper")
    import storage.kb_repo as _kb_repo
    kb = _kb_repo.get(kb_id)
    kb.document_ids = ["d1"]
    _kb_repo.update(kb)

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


# ── search_doc_by_text（V5 #29 / pages/{doc_id}.json 内存 grep）────────────────


def _seed_doc_with_pages(kb_id: str, doc_id: str, by_page: list[dict], original_name: str = "test.pdf"):
    """落一页 KB + doc + pages 文件，模拟已 reparse 的状态。"""
    import services.kb_service as kb_svc
    import storage.doc_repo as doc_repo_mod
    from core.pages_store import save_pages
    from models.knowledge_base import KnowledgeBase
    import storage.kb_repo

    if kb_repo.get(kb_id) is None:
        kb_repo.update(KnowledgeBase(id=kb_id, name="t", category="national"))

    kb_loaded = kb_repo.get(kb_id)
    if doc_id not in (kb_loaded.document_ids or []):
        kb_loaded.document_ids = list((kb_loaded.document_ids or [])) + [doc_id]
        kb_repo.update(kb_loaded)

    save_pages(
        kb_id, doc_id,
        {"by_page": by_page, "full_text": "\n\n".join(p["text"] for p in by_page), "layout": []},
    )
    # 顺手存一个 doc meta 以便 list_docs 正常工作
    try:
        doc_repo_mod._doc_meta_file(kb_id, doc_id).parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def test_search_doc_by_text_pages_hit_returns_page_number():
    """pages 文件命中 → 返回带 page_number（0-based）的结构化命中。"""
    by_page = [
        {"page": 0, "text": "封面 + 目录"},
        {"page": 1, "text": "应符合 GB/T 20145-2006 的光生物安全性要求。"},
        {"page": 2, "text": "其他内容"},
    ]
    _seed_doc_with_pages("vs_t5_a", "doc_a", by_page)

    hits = search_doc_by_text("GB/T 20145", ["vs_t5_a"])
    assert len(hits) == 1
    assert hits[0]["doc_id"] == "doc_a"
    assert hits[0]["kb_id"] == "vs_t5_a"
    assert hits[0]["page_number"] == 1, f"应命中第 2 页（0-based=1）"
    assert "GB/T 20145" in hits[0]["content"]


def test_search_doc_by_text_pages_case_insensitive():
    """大小写不敏感：needle 小写也能匹配页面里大写。"""
    _seed_doc_with_pages("vs_t5_b", "doc_b", [
        {"page": 0, "text": "GB/T 20145-2006 标准条款"},
    ])

    hits = search_doc_by_text("gb/t 20145", ["vs_t5_b"])
    assert len(hits) == 1
    assert hits[0]["page_number"] == 0


def test_search_doc_by_text_pages_no_match_returns_empty():
    """pages 文件存在但命中不上 → 返回空列表。"""
    _seed_doc_with_pages("vs_t5_c", "doc_c", [
        {"page": 0, "text": "完全无关的内容"},
    ])
    assert search_doc_by_text("GB/T 99999", ["vs_t5_c"]) == []


def test_search_doc_by_text_pages_missing_returns_empty():
    """KB 不存在 → 无 pages 文件 → 返回空列表（不抛）。"""
    assert search_doc_by_text("anything", ["no_such_kb"]) == []


def test_search_doc_by_text_pages_multi_kb():
    """跨 KB 搜索：pages 文件分散在多个 KB，每个 doc 取首个命中页。"""
    _seed_doc_with_pages("vs_t5_x", "doc_x1", [{"page": 0, "text": "alpha 文档"}])
    _seed_doc_with_pages("vs_t5_x", "doc_x2", [{"page": 0, "text": "GB/T 99999 在这里"}])
    _seed_doc_with_pages("vs_t5_y", "doc_y1", [{"page": 0, "text": "GB/T 99999 另一处"}])

    hits = search_doc_by_text("GB/T 99999", ["vs_t5_x", "vs_t5_y"])
    assert len(hits) == 2
    docs = {h["doc_id"] for h in hits}
    assert {"doc_x2", "doc_y1"} <= docs
    assert all(h["page_number"] == 0 for h in hits)


def test_search_doc_by_text_pages_each_doc_one_hit():
    """每个 doc 仅贡献首个命中页（per doc break），保证结果不过载。"""
    _seed_doc_with_pages("vs_t5_z", "doc_z", [
        {"page": 0, "text": "首段"},
        {"page": 1, "text": "GB/T 99999 第一处"},
        {"page": 2, "text": "GB/T 99999 第二处（不应被取）"},
    ])
    hits = search_doc_by_text("GB/T 99999", ["vs_t5_z"])
    assert len(hits) == 1
    assert hits[0]["page_number"] == 1


def test_search_doc_by_text_empty_inputs():
    assert search_doc_by_text("", ["kb1"]) == []
    assert search_doc_by_text("kw", []) == []


# ── #27 容错匹配：标准编号格式不匹配时仍能命中 ────────────────────────────────


def test_search_doc_by_text_tolerates_missing_space_in_prefix():
    """#27: 提取器产出 ``IEC61547``（缺空格），文档正文写 ``IEC 61547`` →
    大小写不敏感的精确 ``str.find`` 失败,需走归一化匹配兜底命中。"""
    _seed_doc_with_pages("vs_t27_a", "doc_a", [
        {"page": 0, "text": "应符合 IEC 61547 对灯具电磁兼容的要求"},
    ])
    hits = search_doc_by_text("IEC61547", ["vs_t27_a"])
    assert len(hits) == 1, f"归一化匹配未命中,实际 hits={hits}"
    assert hits[0]["doc_id"] == "doc_a"
    assert hits[0]["page_number"] == 0
    assert "IEC 61547" in hits[0]["content"]


def test_search_doc_by_text_tolerates_dash_vs_dot_separator():
    """#27: 提取器产出 ``GB 7000-.202``（多余横线），文档正文写 ``GB 7000.202`` →
    归一化匹配需把 ``-`` 与 ``.`` 都视为等价分隔符并命中。"""
    _seed_doc_with_pages("vs_t27_b", "doc_b", [
        {"page": 0, "text": "本标准与 GB 7000.202 灯具标准配套使用"},
    ])
    hits = search_doc_by_text("GB 7000-.202", ["vs_t27_b"])
    assert len(hits) == 1, f"归一化匹配未命中,实际 hits={hits}"
    assert hits[0]["doc_id"] == "doc_b"
    assert hits[0]["page_number"] == 0
    assert "GB 7000.202" in hits[0]["content"]


def test_search_doc_by_text_normalization_does_not_split_unrelated_digits():
    """#27 容错不能误命中:不同编号 ``GB 50016`` 不应匹配 ``GB 50034`` 文档。"""
    _seed_doc_with_pages("vs_t27_c", "doc_c", [
        {"page": 0, "text": "建筑照明设计标准 GB 50034 全文"},
    ])
    assert search_doc_by_text("GB 50016", ["vs_t27_c"]) == [], (
        "归一化匹配需保持足够区分度,不应把不同编号视为同一串"
    )


def test_search_doc_by_text_exact_match_still_wins():
    """#27: 归一化匹配是兜底,不破坏原有精确匹配路径。"""
    _seed_doc_with_pages("vs_t27_d", "doc_d", [
        {"page": 0, "text": "网络等级保护 GB/T 22239-2019 要求"},
    ])
    # 精确匹配(原路径)正常命中
    hits = search_doc_by_text("GB/T 22239-2019", ["vs_t27_d"])
    assert len(hits) == 1
    assert hits[0]["page_number"] == 0


def test_search_doc_by_text_normalized_strips_slash_in_gb_t():
    """#27: 归一化剥离 ``/`` 后,``GB/T 20145-2006`` 与 ``GB T 20145 2006``
    归一化串等价 —— needle 写错成 ``GB T 20145 2006``(缺斜线) 也能命中正文。
    """
    _seed_doc_with_pages("vs_t27_e", "doc_e", [
        {"page": 0, "text": "光生物安全 GB/T 20145-2006 标准全文"},
    ])
    # 极端:needle 写错成 ``GB T 20145 2006``(剥空白+``/``+``-`` 等价于原文)
    hits = search_doc_by_text("GB T 20145 2006", ["vs_t27_e"])
    assert len(hits) == 1
    assert "GB/T 20145-2006" in hits[0]["content"]


def test_search_doc_by_text_fallback_snippet_contains_match():
    """#27 snippet 钳位回归：归一化命中后,``idx = min(norm_idx, len(raw_text))``
    必须保证 snippet 仍包含标准编号（不能让 ``norm_idx > len(raw_text)`` 导致
    切片为空）。"""
    _seed_doc_with_pages("vs_t27_f", "doc_f", [
        # 多分母文本: needle ``IEC61547`` 命中位置 ≈ 距 page 末尾 30 字符。
        # 归一化剥空白 + ``.`` 后, raw_text 与 page_norm 长度差会放大 norm_idx,
        # 但钳位后 idx ≤ len(raw_text), snippet 仍能取到原文。
        {"page": 0, "text": "前言 本章 与 ... 标准 ... 与 " * 50 + "IEC 61547 兼容要求 ..."},
    ])
    hits = search_doc_by_text("IEC61547", ["vs_t27_f"])
    assert len(hits) == 1
    assert "IEC 61547" in hits[0]["content"], (
        f"snippet 应含编号,实际 {hits[0]['content']!r}"
    )

