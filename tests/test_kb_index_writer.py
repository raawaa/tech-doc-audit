"""``core.kb_index_writer.KBIndexWriter`` 单元测试(issue #169 / PR-3 AC #4)。

覆盖 5 类契约:
1. **chunking** —— 单篇 doc 切 chunks 进 FAISS。
2. **page-num 注入** —— chunk.metadata["page_number"] 按 by_page 定位。
3. **block-range 注入** —— chunk.metadata["block_range"] 按 layout 映射。
4. **per-doc 失败隔离**(ADR-0007 §3)—— 任一 doc 抛错,该 doc 记
   ``embedding_status=failed``,**其余 doc 继续走通**。
5. **embed 重试派发** —— ``embed_batch_with_retry`` 是 ADR-0007 重试
   owner;writer 通过 lazy-resolve 走 ``core.index_manager.embed_batch_with_retry``,
   ``monkeypatch.setattr("core.index_manager.embed_batch_with_retry", ...)``
   仍能拦截写入路径(向后兼容)。

公开 API 形状:
  - ``KBIndexWriter(kb_id)``
  - ``.index_documents(docs, *, kb_status_writer=None) -> list[DocResult]``
  - ``.rebuild_kb_index(*, progress_callback=None) -> None``
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.kb_index_writer import Doc, DocResult, KBIndexWriter
from core.kb_index_status import KbIndexStatusWriter
from core.kb_index_store import KBIndexStore
from core.parse_document import PageLayout, PageText


# ── fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _use_fake_models(fake_models):
    """opt-in fake embedder,所有测试零模型加载。"""
    yield


@pytest.fixture(autouse=True)
def _reset_stores(tmp_path, monkeypatch):
    """每条用例隔离 KBIndexStore 单例(issue #168 单例表需要重置)。

    ``_per_test_data_dir`` 已在 conftest 里给 ``AUDIT_DATA_DIR`` 写过
    ``tmp_path``,这里只需清 KBIndexStore 实例缓存即可。
    """
    from core.kb_index_store import reset_singletons
    reset_singletons()
    yield
    reset_singletons()


def _seed_kb_meta(kb_id: str) -> None:
    """写 ``index.meta.json`` + KB 元数据,让 ``KBIndexStore.add_doc`` 的
    ``assert_embedding_system_matches`` 不抛。"""
    import storage.kb_repo as kb_repo
    from models.knowledge_base import KnowledgeBase
    kb = KnowledgeBase(id=kb_id, name="seed", category="national")
    kb_repo.update(kb)
    kb = kb_repo.get(kb_id)
    kb.index_status = "searchable"
    kb.document_ids = []
    kb_repo.update(kb)
    KBIndexStore.open(kb_id)._write_index_meta(force=True)


def _make_layout(*pages_blocks):
    return [
        PageLayout(page=i, blocks=list(blocks), width=0, height=0)
        for i, blocks in enumerate(pages_blocks)
    ]


def _make_block(block_content: str, block_order: int, page: int = 0):
    return SimpleNamespace(
        block_content=block_content,
        block_order=block_order,
        page=page,
        bbox_norm=[],
        block_label="text",
    )


# ── 1. chunking ──────────────────────────────────────────────────────────


def test_index_documents_chunks_text_and_writes_faiss():
    """``index_documents`` 切 chunk 后 chunks 进 FAISS(docstore.docs 非空)。

    V4 跨页章节不被腰斩:整篇 text 走一套分块器,不分页硬切。
    """
    _seed_kb_meta("test_writer_chunk")
    docs = [Doc(
        doc_id="doc_a",
        text=(
            "网络安全等级保护基本要求 GB/T 22239-2019 最新版本。\n\n"
            "本标准规定了网络安全等级保护的基本要求与实施方法。\n\n"
            "各组织应根据自身情况选择相应的安全保护等级。"
        ),
        source_name="doc_a.txt",
    )]
    results = KBIndexWriter("test_writer_chunk").index_documents(docs)
    assert len(results) == 1
    assert results[0].status == "done"
    assert results[0].doc_id == "doc_a"

    # FAISS docstore 至少有一个 node
    index = KBIndexStore.open("test_writer_chunk")._get_index()
    assert len(list(index.docstore.docs.values())) >= 1


def test_index_documents_skips_short_text():
    """``text < 20 字符`` → ``status="skipped"``,不进 embed / 不进 FAISS。

    沿袭旧 ``index_document`` 的 "if not text or len(text) < 20: return"
    早退 —— 短文本无语义价值,直接 skip 不烧 embed 配额。
    """
    _seed_kb_meta("test_writer_short")
    docs = [Doc(doc_id="short", text="太短", source_name="x.txt")]
    results = KBIndexWriter("test_writer_short").index_documents(docs)
    assert len(results) == 1
    assert results[0].status == "skipped"

    # FAISS 应为空
    index = KBIndexStore.open("test_writer_short")._get_index()
    assert len(list(index.docstore.docs.values())) == 0


def test_index_documents_empty_text_skipped():
    """``text == ""`` → ``status="skipped"``。"""
    _seed_kb_meta("test_writer_empty")
    docs = [Doc(doc_id="empty", text="", source_name="x.txt")]
    results = KBIndexWriter("test_writer_empty").index_documents(docs)
    assert results[0].status == "skipped"


def test_index_documents_multiple_docs_all_done():
    """多 doc 批量处理 → 每个都 ``done``,results 顺序与输入一致。"""
    _seed_kb_meta("test_writer_multi")
    docs = [
        Doc(doc_id="d1", text="这是第一篇文档的内容。" * 5, source_name="d1.txt"),
        Doc(doc_id="d2", text="这是第二篇文档的内容。" * 5, source_name="d2.txt"),
        Doc(doc_id="d3", text="这是第三篇文档的内容。" * 5, source_name="d3.txt"),
    ]
    results = KBIndexWriter("test_writer_multi").index_documents(docs)
    assert [r.status for r in results] == ["done", "done", "done"]
    assert [r.doc_id for r in results] == ["d1", "d2", "d3"]


# ── 2. page-num 注入 ─────────────────────────────────────────────────────


def test_index_documents_injects_page_number():
    """``index_documents`` 写 ``node.metadata["page_number"]``。

    跨页章节 chunk 起始文本所在的物理页号被写入(0-based)。
    by_page 每页文本要**长**过 chunk prefix(200 字符)才能 match —— 这是
    ``_inject_page_number`` 的契约:chunk 前缀在 by_page[*].text 里 ``find``。
    """
    _seed_kb_meta("test_writer_page_num")
    # chunk 起始文本("第一段内容引导文字")必须落在 by_page[0].text 内
    page0_text = "封面 + 目录 + 一些引言\n第一段内容引导文字" + ("x" * 200)
    page1_text = "第二段内容关于其他章节内容" + ("y" * 200)
    full_text = "第一段内容引导文字" + ("x" * 200) + "\n\n" + "第二段内容关于其他章节内容" + ("y" * 200)
    by_page = [
        PageText(page=0, text=page0_text),
        PageText(page=1, text=page1_text),
    ]
    docs = [Doc(
        doc_id="page_doc",
        text=full_text,
        source_name="page.txt",
        by_page=by_page,
    )]
    results = KBIndexWriter("test_writer_page_num").index_documents(docs)
    assert results[0].status == "done"

    index = KBIndexStore.open("test_writer_page_num")._get_index()
    nodes = list(index.docstore.docs.values())
    assert len(nodes) >= 1
    # 至少有节点的 page_number 是 0(非 None)
    page_numbers = [n.metadata.get("page_number") for n in nodes]
    assert any(p == 0 for p in page_numbers), (
        f"至少有一个 chunk 的 page_number=0,实际 {page_numbers}"
    )


def test_index_documents_no_by_page_yields_none_page_number():
    """没传 by_page → 所有 chunk.page_number = None(不阻塞)。"""
    _seed_kb_meta("test_writer_no_page")
    docs = [Doc(doc_id="no_page", text="纯文本没有任何按页信息。" * 5)]
    KBIndexWriter("test_writer_no_page").index_documents(docs)

    index = KBIndexStore.open("test_writer_no_page")._get_index()
    nodes = list(index.docstore.docs.values())
    for n in nodes:
        assert n.metadata.get("page_number") is None


# ── 3. block-range 注入 ──────────────────────────────────────────────────


def test_index_documents_injects_block_range_for_pdf_layout():
    """``index_documents`` + ``by_layout`` → chunk.metadata["block_range"]`` 非空。

    V8-S2 核心不变量:chunk 覆盖 layout blocks 区间被写进 metadata。
    """
    _seed_kb_meta("test_writer_block_range")
    full_text = "公司各应急保障单位应当配置无线对讲设备至少两套。"
    by_page = [PageText(page=0, text=full_text)]
    by_layout = _make_layout([
        _make_block("公司各应急保障单位", 0),
        _make_block("应当配置无线对讲", 1),
        _make_block("设备至少两套", 2),
    ])
    docs = [Doc(
        doc_id="br_doc",
        text=full_text,
        source_name="br.txt",
        by_page=by_page,
        by_layout=by_layout,
    )]
    results = KBIndexWriter("test_writer_block_range").index_documents(docs)
    assert results[0].status == "done"

    index = KBIndexStore.open("test_writer_block_range")._get_index()
    nodes = list(index.docstore.docs.values())
    block_ranges = [n.metadata.get("block_range") for n in nodes]
    # 至少有一个 chunk 命中 (0, 2)(全文单 chunk 匹配 3 个 blocks)
    assert (0, 2) in block_ranges, f"应至少一个 chunk 的 block_range=(0,2),实际 {block_ranges}"


def test_index_documents_no_layout_yields_none_block_range():
    """没传 by_layout → 所有 chunk.block_range = None(走 fallback 高亮)。"""
    _seed_kb_meta("test_writer_no_br")
    docs = [Doc(doc_id="no_br", text="纯文本没有 layout 信息。" * 5)]
    KBIndexWriter("test_writer_no_br").index_documents(docs)

    index = KBIndexStore.open("test_writer_no_br")._get_index()
    nodes = list(index.docstore.docs.values())
    for n in nodes:
        assert n.metadata.get("block_range") is None


# ── 4. per-doc 失败隔离(ADR-0007 §3)─────────────────────────────────────


def test_index_documents_isolates_per_doc_embedding_failure(monkeypatch):
    """任一 doc 抛错 → 该 doc 记 ``failed``,其余 doc 继续走通。

    三 doc 批量中,doc_b 嵌入抛 ``APIConnectionError``:doc_a "done",
    doc_b "failed"(失败原因持久化到 doc_repo),doc_c "done"。整批不抛。
    """
    import httpx
    from openai import APIConnectionError
    import storage.doc_repo as doc_repo
    _seed_kb_meta("test_writer_iso")

    doc_repo.save_doc(
        "test_writer_iso", "doc_a.md",
        "# A\n建筑工程设计文件编制深度规定内容与标准要求。".encode("utf-8"),
        "md",
    )
    doc_repo.save_doc(
        "test_writer_iso", "doc_b.md",
        "# B\n建筑施工组织设计规范标准要求与实施指南内容。".encode("utf-8"),
        "md",
    )
    doc_repo.save_doc(
        "test_writer_iso", "doc_c.md",
        "# C\n建筑施工质量验收统一标准内容与实施细则。".encode("utf-8"),
        "md",
    )
    docs_meta = doc_repo.list_docs("test_writer_iso")
    doc_a, doc_b, doc_c = sorted(docs_meta, key=lambda d: d.id)

    boom = APIConnectionError(
        request=httpx.Request("POST", "https://api.siliconflow.cn/v1/embeddings"),
    )

    def _patched_batch(embed_model, texts):
        if any("BOOM_TOKEN_B" in t for t in texts):
            raise boom
        return [[float(i)] * 1024 for i in range(len(texts))]

    monkeypatch.setattr(
        "core.index_manager.embed_batch_with_retry", _patched_batch,
    )

    docs = [
        Doc(doc_id=doc_a.id, text="建筑工程设计文件编制深度规定内容与标准要求。", source_name="doc_a.md"),
        Doc(doc_id=doc_b.id, text="建筑施工组织设计规范 BOOM_TOKEN_B 标准要求与实施指南内容。", source_name="doc_b.md"),
        Doc(doc_id=doc_c.id, text="建筑施工质量验收统一标准内容与实施细则。", source_name="doc_c.md"),
    ]
    results = KBIndexWriter("test_writer_iso").index_documents(docs)

    assert [r.status for r in results] == ["done", "failed", "done"]
    assert results[1].doc_id == doc_b.id
    assert "APIConnectionError" in (results[1].error or "")

    # doc_b 失败原因持久化到 doc_repo
    meta_b = doc_repo.get_doc("test_writer_iso", doc_b.id)
    assert meta_b.embedding_status == "failed"
    assert "APIConnectionError" in meta_b.metadata.get("embedding_error", "")


def test_index_documents_failed_doc_has_no_vector_file(monkeypatch):
    """失败 doc 不写 ``.npy`` 也不插 FAISS(防半完成状态污染索引)。

    验证:doc_a 完成后 ``vectors/doc_a.npy`` 存在;doc_b 失败后
    ``vectors/doc_b.npy`` 不存在。
    """
    import httpx
    from openai import APIConnectionError
    _seed_kb_meta("test_writer_no_vec")

    boom = APIConnectionError(
        request=httpx.Request("POST", "https://api.siliconflow.cn/v1/embeddings"),
    )

    def _patched_batch(embed_model, texts):
        if any("施工组织" in t for t in texts):
            raise boom
        return [[0.0] * 1024 for _ in texts]

    monkeypatch.setattr(
        "core.index_manager.embed_batch_with_retry", _patched_batch,
    )

    docs = [
        Doc(doc_id="doc_a", text="建筑工程设计文件编制深度规定内容与标准要求。" * 5, source_name="a.md"),
        Doc(doc_id="doc_b", text="建筑施工组织设计规范标准要求与实施指南内容。" * 5, source_name="b.md"),
    ]
    KBIndexWriter("test_writer_no_vec").index_documents(docs)

    from core.kb_index_store import KBIndexStore
    store = KBIndexStore.open("test_writer_no_vec")
    vectors_dir = store._vectors_dir()
    assert (vectors_dir / "doc_a.npy").exists(), "成功 doc 应写 .npy"
    assert not (vectors_dir / "doc_b.npy").exists(), "失败 doc 不应写 .npy"


def test_index_documents_does_not_abort_batch_on_runtime_error(monkeypatch):
    """``ValueError``(不可重试错误)同样不中止整批。

    ADR-0007 §1:不可重试错误不进入 tenacity 重试,但 batch 层应继续
    处理其余 doc(不抛)。
    """
    _seed_kb_meta("test_writer_runtime")

    def _patched_batch(embed_model, texts):
        if any("施工组织" in t for t in texts):
            raise ValueError("模型未加载")
        return [[0.0] * 1024 for _ in texts]

    monkeypatch.setattr(
        "core.index_manager.embed_batch_with_retry", _patched_batch,
    )

    docs = [
        Doc(doc_id="d1", text="建筑工程设计文件编制深度规定内容与标准要求。" * 5),
        Doc(doc_id="d2", text="建筑施工组织设计规范标准要求与实施指南内容。" * 5),
        Doc(doc_id="d3", text="建筑施工质量验收统一标准内容与实施细则。" * 5),
    ]
    # 不应抛
    results = KBIndexWriter("test_writer_runtime").index_documents(docs)
    assert [r.status for r in results] == ["done", "failed", "done"]


def test_index_documents_skips_docs_not_in_doc_repo(monkeypatch):
    """doc 不在 doc_repo 里时,失败 doc 也不抛异常(doc_repo 缺失 best-effort)。"""
    _seed_kb_meta("test_writer_orphan")

    def _patched_batch(embed_model, texts):
        if any("施工组织" in t for t in texts):
            raise ValueError("simulated")
        return [[0.0] * 1024 for _ in texts]

    monkeypatch.setattr(
        "core.index_manager.embed_batch_with_retry", _patched_batch,
    )

    docs = [
        Doc(doc_id="orphan_a", text="建筑工程设计文件编制深度规定内容与标准要求。" * 5),
        Doc(doc_id="orphan_b", text="建筑施工组织设计规范标准要求与实施指南内容。" * 5),
    ]
    # 不应抛
    results = KBIndexWriter("test_writer_orphan").index_documents(docs)
    assert results[1].status == "failed"


# ── 5. embed 重试派发 ────────────────────────────────────────────────────


def test_index_documents_dispatches_via_embed_batch_with_retry(monkeypatch):
    """``index_documents`` 通过 ``embed_batch_with_retry`` 调用 embedder。

    重试 owner 是 ``core.embed_retry.embed_batch_with_retry``,writer
    不另起一层重试。验证:monkeypatch ``core.index_manager.embed_batch_with_retry``
    后,writer 仍走它(通过 lazy-resolve)。
    """
    _seed_kb_meta("test_writer_retry_dispatch")

    called = []

    def _patched_batch(embed_model, texts):
        called.append(texts)
        return [[float(i)] * 1024 for i in range(len(texts))]

    monkeypatch.setattr(
        "core.index_manager.embed_batch_with_retry", _patched_batch,
    )

    docs = [Doc(
        doc_id="retry_doc",
        text="测试 embed_batch_with_retry 是否被调用。" * 5,
        source_name="r.txt",
    )]
    KBIndexWriter("test_writer_retry_dispatch").index_documents(docs)

    assert len(called) == 1, f"embed_batch_with_retry 应被调 1 次,实际 {len(called)}"
    assert len(called[0]) >= 1, "传入 embed_batch_with_retry 的 texts 至少 1 项"


# ── 6. kb_status_writer 集成 ─────────────────────────────────────────────


def test_index_documents_with_kb_status_writer_calls_callbacks():
    """注入 ``kb_status_writer`` → writer 走 ``note_in_flight`` / ``advance``。

    验证:KbIndexStatusWriter 的 ``_progress`` 在 index_documents 后被推进
    (因为 ``advance`` 被调)。
    """
    _seed_kb_meta("test_writer_writer_integration")

    writer = KbIndexStatusWriter("test_writer_writer_integration", total=2)
    docs = [
        Doc(doc_id="d1", text="第一篇文档。" * 5, source_name="d1.txt"),
        Doc(doc_id="d2", text="第二篇文档。" * 5, source_name="d2.txt"),
    ]
    KBIndexWriter("test_writer_writer_integration").index_documents(
        docs, kb_status_writer=writer,
    )
    # advance 至少调过 2 次 → writer._progress = 1.0(终态)
    assert writer._progress == 1.0


def test_index_documents_single_doc_with_kb_status_writer_writes_failed_state(monkeypatch):
    """单篇路径 + writer(``total=1``)→ 失败时 ``finish(failed=[...])``
    把 KB 写成 ``failed`` + 一行失败摘要。

    ``KbIndexStatusWriter.fail_doc`` 的 total==1 分支会自动 ``finish()``
    写终态;writer 不需要 caller 再 finish。
    """
    import httpx
    from openai import APIConnectionError
    import storage.kb_repo as kb_repo
    _seed_kb_meta("test_writer_single_failed")

    boom = APIConnectionError(
        request=httpx.Request("POST", "https://api.siliconflow.cn/v1/embeddings"),
    )

    def _patched_batch(embed_model, texts):
        raise boom

    monkeypatch.setattr(
        "core.index_manager.embed_batch_with_retry", _patched_batch,
    )

    status_writer = KbIndexStatusWriter("test_writer_single_failed", total=1)
    status_writer.begin()

    docs = [Doc(doc_id="d1", text="触发失败的长文本" * 5, source_name="d1.txt")]
    KBIndexWriter("test_writer_single_failed").index_documents(
        docs, kb_status_writer=status_writer,
    )

    # KB 应该被写成 failed + 一行失败摘要
    kb = kb_repo.get("test_writer_single_failed")
    assert kb.index_status == "failed"
    assert "APIConnectionError" in (kb.index_current_doc or "")


# ── 7. rebuild_kb_index(2-phase 编排) ───────────────────────────────────


def test_rebuild_kb_index_with_cached_vectors_uses_fast_path(monkeypatch):
    """所有 doc 有 ``.npy`` 缓存 → 走 phase 1 快速路径(纯 CPU,无 embed 调用)。

    验证:``embed_batch_with_retry`` 不被调(因为 phase 1 直接用缓存向量)。
    """
    _seed_kb_meta("test_rebuild_fast")

    # 先索引一篇 doc,生成 .npy 缓存
    docs = [Doc(doc_id="d1", text="已经索引过的文档内容。" * 5, source_name="d1.txt")]
    KBIndexWriter("test_rebuild_fast").index_documents(docs)

    # 把 doc_id 写进 KB
    import storage.kb_repo as kb_repo
    kb = kb_repo.get("test_rebuild_fast")
    kb.document_ids = ["d1"]
    kb_repo.update(kb)

    called = []

    def _patched_batch(embed_model, texts):
        called.append(True)
        return [[0.0] * 1024 for _ in texts]

    monkeypatch.setattr(
        "core.index_manager.embed_batch_with_retry", _patched_batch,
    )

    KBIndexWriter("test_rebuild_fast").rebuild_kb_index()
    # phase 1 不调 embed_batch_with_retry
    assert len(called) == 0, (
        f"rebuild 走 phase 1 fast path 不应调 embed_batch_with_retry,实际 {len(called)} 次"
    )


def test_rebuild_kb_index_calls_finish_on_searchable(monkeypatch):
    """rebuild 成功 → KB ``index_status`` 写成 ``searchable``(内置契约)。

    Issue #149 / ADR-0002 §决策 2:rebuild 内置写回字段,无需 caller 再写。
    """
    _seed_kb_meta("test_rebuild_finish")

    docs = [Doc(doc_id="d1", text="用于 rebuild 测试的文档内容。" * 5, source_name="d1.txt")]
    KBIndexWriter("test_rebuild_finish").index_documents(docs)

    import storage.kb_repo as kb_repo
    kb = kb_repo.get("test_rebuild_finish")
    kb.document_ids = ["d1"]
    kb_repo.update(kb)

    KBIndexWriter("test_rebuild_finish").rebuild_kb_index()

    kb_after = kb_repo.get("test_rebuild_finish")
    assert kb_after.index_status == "searchable"


def test_rebuild_kb_index_progress_callback_called():
    """``rebuild_kb_index(progress_callback=...)`` 触发回调。

    进度回调形参 ``(current, total, doc_name)``,每个有 .npy 的 doc
    触发一次回调。
    """
    _seed_kb_meta("test_rebuild_progress")

    docs = [
        Doc(doc_id="d1", text="第一篇用于测试 rebuild 进度回调的文档。" * 5),
        Doc(doc_id="d2", text="第二篇用于测试 rebuild 进度回调的文档。" * 5),
    ]
    KBIndexWriter("test_rebuild_progress").index_documents(docs)

    import storage.kb_repo as kb_repo
    kb = kb_repo.get("test_rebuild_progress")
    kb.document_ids = ["d1", "d2"]
    kb_repo.update(kb)

    progress = []
    KBIndexWriter("test_rebuild_progress").rebuild_kb_index(
        progress_callback=lambda c, t, n: progress.append((c, t, n)),
    )
    assert len(progress) >= 2, f"应至少 2 个进度回调,实际 {len(progress)}"


# ── Doc / DocResult dataclass 形状 ───────────────────────────────────────


def test_doc_dataclass_optional_fields_default_to_none():
    """``Doc`` 的 by_page / by_layout 默认 None(单篇最简用法)。"""
    doc = Doc(doc_id="x", text="text")
    assert doc.by_page is None
    assert doc.by_layout is None
    assert doc.source_name == ""


def test_doc_result_status_values():
    """``DocResult.status`` 是 ``"done"`` / ``"failed"`` / ``"skipped"`` 之一。"""
    assert DocResult(doc_id="d", status="done").status == "done"
    assert DocResult(doc_id="d", status="failed", error="err").error == "err"
    assert DocResult(doc_id="d", status="skipped").error is None
