"""标准关联（Standard Linking）单测。

通过注入假 extractor + monkeypatch 模块级 vec_search / search_doc_by_text / _doc_repo，
脱离 LLM 与 FAISS 测试关联策略（搜索 → 精确验证 → 回填、幻觉清除、缓存、best-effort）。
不加载任何模型。
"""
import types

from models.audit_task import AuditIssue, ExtractedStandard, IssueLocation, StandardRef
from services import standard_linker


def _issue(id, *, doc_id=None, standard_name="", standard_id="", description="desc"):
    return AuditIssue(
        id=id,
        type="compliance",
        location=IssueLocation(original_text="原文"),
        description=description,
        severity="medium",
        standard_reference=StandardRef(
            standard_name=standard_name, standard_id=standard_id, doc_id=doc_id
        ),
    )


def _ext(id, numbers=None, names=None):
    """构造 extractor 返回值的一个条目 (id, ExtractedStandard)。"""
    return (id, ExtractedStandard(numbers=numbers or [], names=names or []))


def _fake_repo(doc_ids, names=None):
    """_doc_repo 替身：list_docs 返回带 .id/.name 的伪 doc。names 为 {doc_id: 标题}。"""
    return types.SimpleNamespace(
        list_docs=lambda kb_id: [
            types.SimpleNamespace(id=d, name=(names or {}).get(d))
            for d in doc_ids
        ]
    )


# ── 关联策略 ──────────────────────────────────────────────────────────────────

def test_text_hit_path(monkeypatch):
    """策略1：文本命中 → 向量补 page/chunk → 回填。"""
    issue = _issue(1)
    monkeypatch.setattr(standard_linker, "_doc_repo", _fake_repo(["d1"]))
    monkeypatch.setattr(standard_linker, "search_doc_by_text", lambda n, k: [{"doc_id": "d1"}])
    monkeypatch.setattr(
        standard_linker, "vec_search",
        lambda kb_ids, q, top_k=5: [{"doc_id": "d1", "page_number": 2,
                                     "content": "应符合 GB/T 20145-2006 的要求"}],
    )
    standard_linker.link_standards(
        [issue], ["kb1"], extractor=lambda pending: dict([_ext(1, ["GB/T 20145-2006"])])
    )
    sr = issue.standard_reference
    assert sr.doc_id == "d1"
    assert sr.page_number == 3            # raw 2 + 1
    assert "GB/T 20145-2006" in sr.chunk_text
    assert sr.standard_name == "GB/T 20145-2006"   # 编号回填


def test_vector_fallback_path(monkeypatch):
    """策略2：文本无果 → 向量按 name 验证 → 回填。"""
    issue = _issue(1)
    monkeypatch.setattr(standard_linker, "_doc_repo", _fake_repo([]))
    monkeypatch.setattr(standard_linker, "search_doc_by_text", lambda n, k: [])
    monkeypatch.setattr(
        standard_linker, "vec_search",
        lambda kb_ids, q, top_k=5: [{"doc_id": "d2", "page_number": 5,
                                     "content": "灯和灯系统的光生物安全性 规定"}],
    )
    standard_linker.link_standards(
        [issue], ["kb1"], extractor=lambda pending: dict([_ext(1, [], ["灯和灯系统的光生物安全性"])])
    )
    sr = issue.standard_reference
    assert sr.doc_id == "d2"
    assert sr.page_number == 6


def test_verification_failure_text_fallback_backfills(monkeypatch):
    """#23 行为变更：文本命中但 vec chunk content 不含编号时，回填 doc_id。

    旧行为是"vec 不验证通过就完全不回填"，但 #23 修复后，文本搜索已确认
    含编号的文档存在 → 直接从 text_hits 回填。`chunk_text` 设为标准编号
    自身（`page_number` 为 None 因为 text 搜索不带页码）。
    """
    issue = _issue(1)
    monkeypatch.setattr(standard_linker, "_doc_repo", _fake_repo([]))
    monkeypatch.setattr(
        standard_linker, "search_doc_by_text",
        lambda n, k: [{"doc_id": "d1", "page_number": None, "content": "..."}],
    )
    monkeypatch.setattr(
        standard_linker, "vec_search",
        lambda kb_ids, q, top_k=5: [{"doc_id": "d1", "page_number": 1, "content": "完全无关的内容"}],
    )
    standard_linker.link_standards(
        [issue], ["kb1"], extractor=lambda pending: dict([_ext(1, ["GB 50016"])])
    )
    sr = issue.standard_reference
    assert sr.doc_id == "d1"               # 文本回填生效
    assert sr.page_number is None          # 文本搜索不带页码
    assert sr.chunk_text == "GB 50016"     # chunk_text = 编号自身
    assert sr.standard_name == "GB 50016"  # 编号回填


def test_hallucinated_doc_id_cleared_and_relinked(monkeypatch):
    """幻觉 doc_id：指向不存在的文档 → 清空 → 重新搜索 → 关联到真实文档。"""
    issue = _issue(1, doc_id="ghost")
    monkeypatch.setattr(standard_linker, "_doc_repo", _fake_repo(["real_doc"]))
    monkeypatch.setattr(standard_linker, "search_doc_by_text", lambda n, k: [{"doc_id": "real_doc"}])
    monkeypatch.setattr(
        standard_linker, "vec_search",
        lambda kb_ids, q, top_k=5: [{"doc_id": "real_doc", "page_number": 0,
                                     "content": "GB 50016 条文"}],
    )
    standard_linker.link_standards(
        [issue], ["kb1"], extractor=lambda pending: dict([_ext(1, ["GB 50016"])])
    )
    sr = issue.standard_reference
    assert sr.doc_id == "real_doc"         # 原 ghost 被清除后重连
    assert sr.page_number == 1


def test_search_cache_dedupes(monkeypatch):
    """同一标准编号只文本搜索一次（缓存命中第二次）。"""
    i1, i2 = _issue(1), _issue(2)
    monkeypatch.setattr(standard_linker, "_doc_repo", _fake_repo([]))
    counter = {"n": 0}

    def search_doc_by_text(n, k):
        counter["n"] += 1
        return [{"doc_id": "d1"}]

    monkeypatch.setattr(standard_linker, "search_doc_by_text", search_doc_by_text)
    monkeypatch.setattr(
        standard_linker, "vec_search",
        lambda kb_ids, q, top_k=5: [{"doc_id": "d1", "page_number": 1, "content": "GB 50016"}],
    )
    extractor = lambda pending: dict([_ext(1, ["GB 50016"]), _ext(2, ["GB 50016"])])
    standard_linker.link_standards([i1, i2], ["kb1"], extractor=extractor)
    assert counter["n"] == 1
    assert i1.standard_reference.doc_id == "d1"
    assert i2.standard_reference.doc_id == "d1"


# ── best-effort ───────────────────────────────────────────────────────────────

def test_best_effort_extractor_empty(monkeypatch):
    """extractor 返回 {} → 不搜索、不抛、issues 不变。"""
    issue = _issue(1)
    monkeypatch.setattr(standard_linker, "_doc_repo", _fake_repo([]))
    monkeypatch.setattr(standard_linker, "search_doc_by_text",
                        lambda n, k: pytest_fail("search should not run"))
    standard_linker.link_standards([issue], ["kb1"], extractor=lambda pending: {})
    assert issue.standard_reference.doc_id is None


def test_best_effort_search_raises(monkeypatch):
    """搜索抛异常 → link_standards 吞掉，不向上抛。"""
    issue = _issue(1)
    monkeypatch.setattr(standard_linker, "_doc_repo", _fake_repo([]))
    monkeypatch.setattr(standard_linker, "search_doc_by_text", lambda n, k: [])  # 走策略2

    def boom(kb_ids, q, top_k=5):
        raise RuntimeError("FAISS down")

    monkeypatch.setattr(standard_linker, "vec_search", boom)
    standard_linker.link_standards(  # 不应抛
        [issue], ["kb1"], extractor=lambda pending: dict([_ext(1, ["GB 50016"])])
    )


# ── 边界 ──────────────────────────────────────────────────────────────────────

def test_issue_without_standard_reference_skipped(monkeypatch):
    """无 standard_reference 的 issue 被跳过，extractor 不被调用。"""
    issue = _issue(1)
    issue.standard_reference = None
    monkeypatch.setattr(standard_linker, "_doc_repo", _fake_repo([]))

    def must_not_call(pending):
        raise AssertionError("extractor should not be called")

    standard_linker.link_standards([issue], ["kb1"], extractor=must_not_call)
    assert issue.standard_reference is None


def test_standard_name_backfill_without_doc_link(monkeypatch):
    """搜不到文档时，仍从编号回填 standard_name / standard_id。"""
    issue = _issue(1)
    monkeypatch.setattr(standard_linker, "_doc_repo", _fake_repo([]))
    monkeypatch.setattr(standard_linker, "search_doc_by_text", lambda n, k: [])
    monkeypatch.setattr(standard_linker, "vec_search", lambda kb_ids, q, top_k=5: [])
    standard_linker.link_standards(
        [issue], ["kb1"], extractor=lambda pending: dict([_ext(1, ["CJJ 101-2016"])])
    )
    sr = issue.standard_reference
    assert sr.doc_id is None
    assert sr.standard_name == "CJJ 101-2016"
    assert sr.standard_id == "CJJ 101-2016"


def test_name_corrected_when_doc_hit_mismatches_prefilled_name(monkeypatch):
    """#3 回归：best_hit 命中正确 KB 文档时，issue 上错误预填的 standard_name
    必须被校正为反映命中文档，而非原样保留。

    复现用户症状：issue 预填 standard_name="JG_T578-2021 装配式建筑用墙板技术要求"
    （agent 抄了被审核文档里的错名），但命中的真实文档是 GB 50034-2013
    建筑照明设计标准。前端用 standard_doc_id 拼链接（指向 GB 50034，正确）、
    用 standard_name 做显示文本，二者不一致 → 显示名错。
    """
    issue = _issue(
        1,
        standard_name="JG_T578-2021 装配式建筑用墙板技术要求",
        standard_id="JG_T578-2021",
    )
    monkeypatch.setattr(
        standard_linker, "_doc_repo",
        _fake_repo(["gb50034_doc"], names={"gb50034_doc": "GB 50034-2013 建筑照明设计标准"}),
    )
    monkeypatch.setattr(
        standard_linker, "search_doc_by_text",
        lambda n, k: [{"doc_id": "gb50034_doc"}],
    )
    monkeypatch.setattr(
        standard_linker, "vec_search",
        lambda kb_ids, q, top_k=5: [{
            "doc_id": "gb50034_doc", "page_number": 0,
            "content": "GB 50034-2013 建筑照明设计标准 引用标准名录",
        }],
    )
    standard_linker.link_standards(
        [issue], ["kb1"],
        extractor=lambda pending: dict([_ext(1, ["GB 50034-2013"], ["建筑照明设计标准"])]),
    )
    sr = issue.standard_reference
    assert sr.doc_id == "gb50034_doc"
    assert sr.standard_name == "GB 50034-2013 建筑照明设计标准"
    assert sr.standard_id == "GB 50034-2013 建筑照明设计标准"


def test_empty_inputs_no_op():
    """issues 或 kb_ids 为空 → 直接返回。"""
    standard_linker.link_standards([], ["kb1"])           # 无 issues
    standard_linker.link_standards([_issue(1)], [])       # 无 kb_ids


def pytest_fail(msg):
    """在 lambda 中用作"不应被调用"哨兵。"""
    raise AssertionError(msg)

# ── 文本搜索回填（#23 修复：多 KB 召回稀释下仍能关联） ─────────────────────

def test_text_fallback_when_vec_verification_fails(monkeypatch):
    """#23 主修复：多 KB 召回稀释场景下，vec 检索的 chunk content 不含标准编号
    （被不相关 KB 的 chunk 挤出 top_k），但 text search 已确认含编号的文档存在
    → 必须从 text_hits 回填 doc_id。旧代码因强制 vec 二次验证，doc_id 留 None。
    """
    issue = _issue(1)
    monkeypatch.setattr(standard_linker, "_doc_repo", _fake_repo(["d1"]))
    monkeypatch.setattr(
        standard_linker, "search_doc_by_text",
        lambda n, k: [{"doc_id": "d1", "page_number": None, "content": "本标准 规定..."}],
    )
    # vec 搜索：chunk content 不含编号（被稀释出去）
    monkeypatch.setattr(
        standard_linker, "vec_search",
        lambda kb_ids, q, top_k=5: [{"doc_id": "other_doc", "page_number": 0,
                                     "content": "完全不相关的内容 不含编号"}],
    )
    standard_linker.link_standards(
        [issue], ["kb1"], extractor=lambda pending: dict([_ext(1, ["GB/T 20145-2006"])])
    )
    sr = issue.standard_reference
    assert sr.doc_id == "d1"               # 文本命中即回填
    assert sr.page_number is None          # 文本搜索不带页码
    assert sr.chunk_text == "GB/T 20145-2006"  # chunk_text = 标准编号自身


def test_text_fallback_disambiguation_by_filename(monkeypatch):
    """#23: text_hits 命中多文档时，优先选 name 含标准编号的文档。"""
    issue = _issue(1)
    monkeypatch.setattr(
        standard_linker, "_doc_repo",
        _fake_repo(["list_doc", "std_doc"],
                    names={"list_doc": "适用标准名录.pdf",
                           "std_doc": "GB 50034-2013 建筑照明设计标准.pdf"}),
    )
    monkeypatch.setattr(
        standard_linker, "search_doc_by_text",
        lambda n, k: [{"doc_id": "list_doc", "page_number": None,
                       "content": "本项目适用以下标准：GB 50034-2013 ..."},
                      {"doc_id": "std_doc", "page_number": None,
                       "content": "GB 50034-2013 建筑照明设计标准..."}],
    )
    monkeypatch.setattr(standard_linker, "vec_search", lambda kb_ids, q, top_k=5: [])
    standard_linker.link_standards(
        [issue], ["kb1"], extractor=lambda pending: dict([_ext(1, ["GB 50034-2013"])])
    )
    sr = issue.standard_reference
    assert sr.doc_id == "std_doc"          # 优先选 name 含编号的文档
    assert sr.standard_name == "GB 50034-2013 建筑照明设计标准.pdf"  # name 回填


def test_text_fallback_disambiguation_by_standard_name(monkeypatch):
    """#23: text_hits 命中多文档但都无编号在 name → 退到 name 含标准中文名。"""
    issue = _issue(1)
    monkeypatch.setattr(
        standard_linker, "_doc_repo",
        _fake_repo(["other_doc", "std_doc"],
                    names={"other_doc": "采购清单.pdf",
                           "std_doc": "建筑照明设计标准.pdf"}),
    )
    monkeypatch.setattr(
        standard_linker, "search_doc_by_text",
        lambda n, k: [{"doc_id": "other_doc", "page_number": None, "content": "..."},
                      {"doc_id": "std_doc", "page_number": None, "content": "..."}],
    )
    monkeypatch.setattr(standard_linker, "vec_search", lambda kb_ids, q, top_k=5: [])
    standard_linker.link_standards(
        [issue], ["kb1"],
        extractor=lambda pending: dict([_ext(1, ["GB 50034-2013"], ["建筑照明设计标准"])]),
    )
    sr = issue.standard_reference
    assert sr.doc_id == "std_doc"          # 名字含标准中文名的赢


def test_text_fallback_disambiguation_number_beats_chinese_name(monkeypatch):
    """#23: 优先级交叉验证 — 文档 A 的 name 含标准编号，文档 B 的 name 只含中文名。
    编号-name 规则必须赢，即使 B 在 text_hits 中排在前面。
    """
    issue = _issue(1)
    monkeypatch.setattr(
        standard_linker, "_doc_repo",
        _fake_repo(["chinese_named", "number_named"],
                    names={"chinese_named": "建筑照明设计标准.pdf",
                           "number_named": "GB 50034-2013 适用清单.pdf"}),
    )
    # 注意：B (chinese_named) 排在前面，诱骗实现误选 B
    monkeypatch.setattr(
        standard_linker, "search_doc_by_text",
        lambda n, k: [{"doc_id": "chinese_named", "page_number": None, "content": "..."},
                      {"doc_id": "number_named", "page_number": None, "content": "..."}],
    )
    monkeypatch.setattr(standard_linker, "vec_search", lambda kb_ids, q, top_k=5: [])
    standard_linker.link_standards(
        [issue], ["kb1"],
        extractor=lambda pending: dict([_ext(1, ["GB 50034-2013"], ["建筑照明设计标准"])]),
    )
    sr = issue.standard_reference
    assert sr.doc_id == "number_named"     # 编号-name 规则赢，即使它排在第二位
    assert sr.standard_name == "GB 50034-2013 适用清单.pdf"


def test_text_fallback_first_when_no_disambiguator(monkeypatch):
    """#23: 都没编号/名字线索 → 取 text_hits 第一个。"""
    issue = _issue(1)
    monkeypatch.setattr(
        standard_linker, "_doc_repo",
        _fake_repo(["first_doc", "second_doc"],
                    names={"first_doc": "A.pdf", "second_doc": "B.pdf"}),
    )
    monkeypatch.setattr(
        standard_linker, "search_doc_by_text",
        lambda n, k: [{"doc_id": "first_doc", "page_number": None, "content": "..."},
                      {"doc_id": "second_doc", "page_number": None, "content": "..."}],
    )
    monkeypatch.setattr(standard_linker, "vec_search", lambda kb_ids, q, top_k=5: [])
    standard_linker.link_standards(
        [issue], ["kb1"], extractor=lambda pending: dict([_ext(1, ["GB 50016"])])
    )
    sr = issue.standard_reference
    assert sr.doc_id == "first_doc"        # 第一个


def test_text_fallback_survives_vec_error(monkeypatch):
    """#23 best-effort: 文本回填路径不能因 vec 抛错而失败。"""
    issue = _issue(1)
    monkeypatch.setattr(standard_linker, "_doc_repo", _fake_repo(["d1"]))
    monkeypatch.setattr(
        standard_linker, "search_doc_by_text",
        lambda n, k: [{"doc_id": "d1", "page_number": None, "content": "..."}],
    )
    monkeypatch.setattr(
        standard_linker, "vec_search",
        lambda kb_ids, q, top_k=5: (_ for _ in ()).throw(RuntimeError("vec down")),
    )
    standard_linker.link_standards(  # 不应抛
        [issue], ["kb1"], extractor=lambda pending: dict([_ext(1, ["GB 50016"])])
    )
    # vec 错误后文本回填仍应成功
    sr = issue.standard_reference
    assert sr.doc_id == "d1"               # 文本回填仍生效
    assert sr.standard_name == "GB 50016"


def test_text_fallback_no_vec_at_all(monkeypatch):
    """#23: text 命中、vec 彻底返回空 → 仍然从 text 回填 doc_id。"""
    issue = _issue(1)
    monkeypatch.setattr(standard_linker, "_doc_repo", _fake_repo(["d1"]))
    monkeypatch.setattr(
        standard_linker, "search_doc_by_text",
        lambda n, k: [{"doc_id": "d1", "page_number": None, "content": "..."}],
    )
    monkeypatch.setattr(standard_linker, "vec_search", lambda kb_ids, q, top_k=5: [])
    standard_linker.link_standards(
        [issue], ["kb1"], extractor=lambda pending: dict([_ext(1, ["GB 50016"])])
    )
    sr = issue.standard_reference
    assert sr.doc_id == "d1"
    assert sr.chunk_text == "GB 50016"


def test_text_fallback_chunk_text_uses_first_standard_number(monkeypatch):
    """#23: chunk_text 等于 standard_numbers[0]（编号自身）。"""
    issue = _issue(1)
    monkeypatch.setattr(standard_linker, "_doc_repo", _fake_repo(["d1"]))
    monkeypatch.setattr(
        standard_linker, "search_doc_by_text",
        lambda n, k: [{"doc_id": "d1", "page_number": None, "content": "..."}],
    )
    monkeypatch.setattr(standard_linker, "vec_search", lambda kb_ids, q, top_k=5: [])
    standard_linker.link_standards(
        [issue], ["kb1"],
        extractor=lambda pending: dict([_ext(1, ["GB/T 20145-2006", "GB 7000.1-2015"])]),
    )
    sr = issue.standard_reference
    assert sr.chunk_text == "GB/T 20145-2006"  # 取第一个
