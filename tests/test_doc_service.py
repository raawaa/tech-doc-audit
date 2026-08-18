"""文档服务单元测试"""

import time

import pytest

import services.kb_service as kb_svc
import services.doc_service as doc_svc


# ── 共享异步等待 helper（issue #136）──────────────────────────────────────────
#
# 旧实现给后台索引线程写死轮询预算（50/100 次 × 0.1s），预算本身是拍脑袋的，
# 且失败断言只报布尔 —— 超时拿到中间态 ``indexing`` 时无从排查。统一换成
# 有上限的条件等待：上限给 30s（stub 掉 parse 层后后台线程毫秒级完成，超时
# 只在真坏掉时发生），失败信息带上最后观测到的状态。

TERMINAL_STATES = ("embedded", "failed")


def _wait_docs_terminal(kb_id: str, doc_ids: list[str], *, timeout_s: float = 30.0) -> dict[str, str]:
    """轮询直到全部 doc 的 ``embedding_status`` 到终态，返回最后观测的 status 映射。

    注意磁盘元数据可能有瞬时竞态（写 truncate vs 读），捕获异常并重试。
    超时抛 ``TimeoutError``，信息里带上最后观测到的状态，不只报布尔。
    """
    import storage.doc_repo as doc_repo

    deadline = time.monotonic() + timeout_s
    last: dict[str, str] = {}
    while time.monotonic() < deadline:
        try:
            statuses = {}
            for doc_id in doc_ids:
                fresh = doc_repo.get_doc(kb_id, doc_id)
                statuses[doc_id] = fresh.embedding_status if fresh else "doc 不可读"
            last = statuses
        except Exception:
            time.sleep(0.1)
            continue
        if all(s in TERMINAL_STATES for s in statuses.values()):
            return statuses
        time.sleep(0.1)
    raise TimeoutError(
        f"文档 {timeout_s:.0f}s 内未全部到达终态 {TERMINAL_STATES}，"
        f"最后观测到 {last}"
    )


def _wait_kb_searchable(kb_id: str, *, timeout_s: float = 30.0) -> None:
    """轮询直到 KB 的 ``index_status`` 落到 searchable；失败带上最后观测状态。"""
    import storage.kb_repo as kb_repo

    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        try:
            kb = kb_repo.get(kb_id)
            last = kb.index_status if kb is not None else "KB 不可读"
        except Exception:
            time.sleep(0.1)
            continue
        if last == "searchable":
            return
        time.sleep(0.1)
    raise TimeoutError(
        f"KB {kb_id} {timeout_s:.0f}s 内未到达 searchable，最后 index_status={last!r}"
    )


@pytest.fixture
def stub_pdf_parse(monkeypatch):
    """把 ``core.parse_document.parse_document`` 替换为「空 ParseResult」桩。

    issue #136：fake/corrupt PDF 的真实解析路径会落到 PaddleOCR 分支（文字层
    检测失败 → OCR 路由），测试里等于连第三方 OCR 服务，成败还取决于 ``.env``
    是否配了凭证。异步导入测试验证的契约本不在解析内容 —— 桩掉解析入口后：
    不触发任何 PDF 路由 / OCR / embedding 模型（空 full_text 触发
    ``vector_search.index_document`` 的 20 字符 early-return），后台线程只走
    状态机推进，毫秒级完成。
    """
    from core.parse_document import PageText, ParseResult

    empty = ParseResult(by_page=[PageText(page=0, text="")], full_text="", layout=[])

    def _fake_parse(file_path: str, **kwargs) -> ParseResult:
        return empty

    monkeypatch.setattr("core.parse_document.parse_document", _fake_parse)


def test_import_document():
    """测试导入文档"""
    kb = kb_svc.create_kb(name="测试", category="national")

    # 创建一个简单的 PDF 文件
    content = b"%PDF-1.4 fake pdf content"
    doc = doc_svc.import_document(kb.id, "test.pdf", content)

    assert doc.name == "test.pdf"
    assert doc.file_type == "pdf"
    assert doc.kb_id == kb.id


def test_import_document_async(stub_pdf_parse):
    """测试异步导入文档（async_index=True 生产路径）。

    issue #136：stub 掉 parse 层（见 ``stub_pdf_parse``），后台线程只推进
    索引状态机，毫秒级完成 —— 不再有写死 5 秒预算撞上中间态 ``indexing`` 的
    flake，结果也不依赖 ``.env`` 是否配了 OCR 凭证。
    """
    import storage.kb_repo as kb_repo

    kb = kb_svc.create_kb(name="测试异步", category="national")

    content = b"%PDF-1.4 fake pdf content"
    doc = doc_svc.import_document(kb.id, "test.pdf", content, async_index=True)

    # async 导入返回时可能仍在 pending_index，也可能已被后台线程标记为 indexing
    assert doc.embedding_status in ("pending_index", "indexing")

    # 等待后台线程完成（stub 空文本 → early-return → embedded/failed）
    _wait_docs_terminal(kb.id, [doc.id])

    # 验证 document_ids 包含该文档
    kb = kb_repo.get(kb.id)
    assert kb is not None
    assert doc.id in kb.document_ids

    # 验证 KB 状态恢复 searchable
    _wait_kb_searchable(kb.id)


def test_import_document_async_multiple(stub_pdf_parse):
    """测试多次异步导入（防 document_ids 被覆盖）。

    issue #136：stub 掉 parse 层后 5 个后台线程只走状态机推进，毫秒级完成；
    等待用共享的有界轮询 helper，超时报出最后观测到的状态。
    """
    import storage.kb_repo as kb_repo

    kb = kb_svc.create_kb(name="测试并发异步", category="national")

    docs = []
    for i in range(5):
        content = "fake pdf content {}".format(i).encode()
        doc = doc_svc.import_document(kb.id, f"test_{i}.pdf", content, async_index=True)
        docs.append(doc)

    # 等待所有后台线程完成（从 repo 重读，状态变为 embedded 或 failed）
    _wait_docs_terminal(kb.id, [d.id for d in docs])

    # 验证所有文档 id 都在 kb.document_ids 中
    kb = kb_repo.get(kb.id)
    assert kb is not None
    for doc in docs:
        assert doc.id in kb.document_ids, f"doc {doc.id} not in document_ids"

    # 异步路径在每篇 doc 索引完成时都会把 KB 写 searchable，但并发调度下
    # 最后一个线程可能把自己的状态写完后整体可见——轮询直到 searchable
    _wait_kb_searchable(kb.id)


def test_batch_import_documents_async():
    """测试批量异步导入。"""
    import storage.kb_repo as kb_repo

    kb = kb_svc.create_kb(name="测试批量异步", category="national")

    files = [
        ("doc_1.md", b"# Document 1\n\nThis is the content of document 1 for testing."),
        ("doc_2.md", b"# Document 2\n\nThis is the content of document 2 for testing."),
        ("doc_3.md", b"# Document 3\n\nThis is the content of document 3 for testing."),
    ]

    docs = doc_svc.batch_import_documents(kb.id, files, async_index=True)
    assert len(docs) == 3

    # 等待后台线程完成（从 repo 重新读取最新状态）
    # 注意：磁盘元数据可能有瞬时竞态（写 truncate vs 读），捕获 JSON 错误并重试
    import storage.doc_repo as doc_repo
    for _ in range(600):
        try:
            fresh_docs = [doc_repo.get_doc(kb.id, d.id) for d in docs]
        except Exception:
            time.sleep(0.1)
            continue
        if fresh_docs and all(
            d and d.embedding_status not in ("pending_index", "indexing")
            for d in fresh_docs
        ):
            break
        time.sleep(0.5)

    # 验证所有文档都在 kb.document_ids 中
    kb = kb_repo.get(kb.id)
    assert kb is not None
    for doc in docs:
        assert doc.id in kb.document_ids, f"doc {doc.id} not in document_ids"

    assert kb.index_status == "searchable"


# ── Markdown 文档导入 ────────────────────────────────────────────────────────


def test_import_markdown_document():
    """测试导入 .md 文件（同步索引，含 ## 标题触发 MarkdownNodeParser）。

    同步路径：embedding_status → embedded（不再用已废弃的 ready）。
    """
    kb = kb_svc.create_kb(name="测试MD导入", category="national")

    content = "# 设计说明\n\n## 第一章 总则\n\n这是总则内容。\n\n## 第二章 要求\n\n这是具体要求内容。".encode()
    doc = doc_svc.import_document(kb.id, "设计说明.md", content)

    assert doc.name == "设计说明.md"
    assert doc.file_type == "md"
    assert doc.kb_id == kb.id
    assert doc.embedding_status == "embedded"


def test_import_markdown_document_async():
    """测试异步导入 .md 文件。"""
    import storage.kb_repo as kb_repo

    kb = kb_svc.create_kb(name="测试MD异步", category="national")

    content = "# 施工规范\n\n## 第一章 总则\n\n施工规范测试内容。\n\n## 第二章 要求\n\n具体要求内容。".encode()
    doc = doc_svc.import_document(kb.id, "施工规范.md", content, async_index=True)

    assert doc.embedding_status in ("pending_index", "indexing")

    # 等待后台线程完成（MD 提取快速返回）
    for _ in range(50):
        if doc.embedding_status not in ("pending_index", "indexing"):
            break
        time.sleep(0.1)

    assert doc.embedding_status in ("embedded", "failed"), (
        f"expected embedded/failed, got {doc.embedding_status}"
    )

    # 验证 KB 状态恢复 searchable
    kb = kb_repo.get(kb.id)
    assert kb is not None
    assert doc.id in kb.document_ids
    assert kb.index_status == "searchable"


def test_batch_import_markdown_documents():
    """测试批量导入 .md 文件（同步索引，避免后台线程竞态）。"""
    import storage.kb_repo as kb_repo

    kb = kb_svc.create_kb(name="测试MD批量", category="national")

    files = [
        ("设计说明.md", "# 设计说明\n\n## 第一章\n\n内容一。\n\n## 第二章\n\n内容二。".encode()),
        ("施工规范.md", "# 施工规范\n\n## 第一章\n\n内容三。\n\n## 第二章\n\n内容四。".encode()),
    ]

    docs = doc_svc.batch_import_documents(kb.id, files, async_index=False)
    assert len(docs) == 2

    # 同步索引完成后验证
    kb = kb_repo.get(kb.id)
    assert kb is not None
    assert len(kb.document_ids) == 2
    for doc in docs:
        assert doc.id in kb.document_ids, f"doc {doc.id} not in document_ids"
    assert doc.embedding_status == "embedded"
    assert kb.index_status == "searchable"


# ── 删除 / 异常 ──────────────────────────────────────────────────────────────


def test_delete_document():
    """测试删除文档"""
    kb = kb_svc.create_kb(name="测试", category="national")

    content = b"%PDF-1.4"
    doc = doc_svc.import_document(kb.id, "test.pdf", content)
    doc_id = doc.id

    success = doc_svc.delete_document(kb.id, doc_id)
    assert success is True

    import storage.doc_repo as doc_repo
    retrieved = doc_repo.get_doc(kb.id, doc_id)
    assert retrieved is None


def test_import_unsupported_format():
    """测试导入不支持的文件格式"""
    kb = kb_svc.create_kb(name="测试", category="national")

    with pytest.raises(ValueError) as exc_info:
        doc_svc.import_document(kb.id, "test.exe", b"binary")

    assert "不支持的文件格式" in str(exc_info.value)


# ── 并发 / TOCTOU 回归 ──────────────────────────────────────────────────────────


def test_append_doc_ids_atomic():
    """_append_doc_ids_atomic：去重追加、KB 不存在时静默返回。"""
    import storage.kb_repo as kb_repo
    from services.doc_service import _append_doc_ids_atomic

    kb = kb_svc.create_kb(name="原子追加", category="national")
    _append_doc_ids_atomic(kb.id, ["d1", "d2", "d1"])  # d1 重复
    kb = kb_repo.get(kb.id)
    assert kb.document_ids == ["d1", "d2"]

    # KB 不存在 → 静默返回，不抛异常
    _append_doc_ids_atomic("nonexistent-kb-id", ["d3"])


def test_concurrent_batch_imports_no_orphans(monkeypatch):
    """并发批量导入同一 KB → 所有 doc_id 都保留，无陈旧覆盖丢失。

    回归 review_report.md #2 的 TOCTOU：batch_import_documents 此前在锁外用
    陈旧 kb 对象追加 document_ids 再写回，并发批量会互相覆盖丢失 id。改用
    _append_doc_ids_atomic（锁内 read-modify-write）后，4 批 × 3 篇全部保留。

    mock 掉真实索引（避免触发 embedding 模型加载），只验证 document_ids 一致性。
    """
    import threading
    import storage.kb_repo as kb_repo

    monkeypatch.setattr("core.index_manager.index_documents_batch", lambda *a, **k: None)

    kb = kb_svc.create_kb(name="并发批量", category="national")

    def batch_one(i):
        files = [(f"doc_{i}_{j}.md", f"# Doc {i}_{j}\n\nTest content for document {i}_{j}.".encode()) for j in range(3)]
        doc_svc.batch_import_documents(kb.id, files, async_index=False)

    threads = [threading.Thread(target=batch_one, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    kb_final = kb_repo.get(kb.id)
    assert kb_final is not None
    assert len(kb_final.document_ids) == 12  # 4 批 × 3 篇，全部保留无丢失


# ── 内容去重测试 ──────────────────────────────────────────────────────────────


def test_import_document_dedup():
    """导入相同内容的文档两次，第二次返回已有文档（跳过重复导入）。"""
    import storage.doc_repo as doc_repo

    kb = kb_svc.create_kb(name="去重测试", category="national")

    content = "# 测试文档\n\n这是一份测试文档的内容，用于验证去重功能是否正常。\n\n## 第二章\n\n更多测试内容。".encode()
    doc1 = doc_svc.import_document(kb.id, "test.md", content)

    # 再次导入相同内容，应返回同一个文档
    doc2 = doc_svc.import_document(kb.id, "test_copy.md", content)

    assert doc2.id == doc1.id, f"去重失败：第二次导入应返回已有文档 {doc1.id}，实际返回 {doc2.id}"

    # 确认 content_hash 已设置
    assert doc1.content_hash is not None
    assert len(doc1.content_hash) == 64  # SHA-256

    # 确认只创建了一篇文档
    all_docs = doc_repo.list_docs(kb.id)
    assert len(all_docs) == 1


def test_batch_import_documents_dedup():
    """批量导入混合新/重复文档，只导入新文档。"""
    import storage.doc_repo as doc_repo

    kb = kb_svc.create_kb(name="批量去重", category="national")

    content_a = "# 文档A\n\n文档A的测试内容。\n\n## 第一节\n\n具体内容。".encode()
    content_b = "# 文档B\n\n文档B的测试内容。\n\n## 第一节\n\n其他内容。".encode()

    # 第一次导入 2 个文档
    files1 = [("doc_a.md", content_a), ("doc_b.md", content_b)]
    docs1 = doc_svc.batch_import_documents(kb.id, files1, async_index=False)
    assert len(docs1) == 2

    # 第二次导入：A 重复、B 重复、C 新
    content_c = "# 文档C\n\n文档C的测试内容。\n\n## 第一节\n\n新内容。".encode()
    files2 = [("doc_a_v2.md", content_a), ("doc_b_v2.md", content_b), ("doc_c.md", content_c)]
    docs2 = doc_svc.batch_import_documents(kb.id, files2, async_index=False)

    # 只应有 1 个新文档（C）
    assert len(docs2) == 1, f"预期导入 1 篇新文档，实际 {len(docs2)} 篇"
    assert docs2[0].original_name == "doc_c.md"

    # KB 中总共 3 篇文档
    all_docs = doc_repo.list_docs(kb.id)
    assert len(all_docs) == 3


# ── KbIndexStatusWriter 集成（issue #152）────────────────────────────────────────
#
# #152 把 `_index_single_doc_async` / `_batch_index_docs` 内三处 KB 状态字段的
# 直接写收集归 `KbIndexStatusWriter`。验证手段：spy writer 的 callback 调用
# 序列 —— 函数必须经过 writer 这一间接层（而不是直接 `kb.index_status = ...`）。
# `_get_lock(kb_id)` 仍是文档生命周期锁，**不被 writer 替换**，验证锁外还能
# 跑非 KB-状态的工作（``doc_repo._save_doc_meta`` 等）。


def test_index_single_doc_async_uses_writer(monkeypatch, stub_pdf_parse):
    """`_index_single_doc_async` 不再直接 ``kb.index_status = ...``，改走 writer。

    同步 ``_index_vec`` 桩为成功 → 函数末尾 ``kb_writer.finish()`` 被调一次。
    """
    from unittest.mock import MagicMock, patch
    from core.kb_index_status import KbIndexStatusWriter

    # spy on KbIndexStatusWriter 的 callback 序列
    callback_calls: list[tuple[str, tuple]] = []

    real_begin = KbIndexStatusWriter.begin
    real_finish = KbIndexStatusWriter.finish
    real_note_in_flight = KbIndexStatusWriter.note_in_flight

    def spy_begin(self):
        callback_calls.append(("begin", ()))
        real_begin(self)

    def spy_finish(self, failed=None, *, interrupted=None):
        callback_calls.append(("finish", (failed, interrupted)))
        real_finish(self, failed, interrupted=interrupted)

    def spy_note(self, doc_name):
        callback_calls.append(("note_in_flight", (doc_name,)))
        real_note_in_flight(self, doc_name)

    # 桩掉 _index_vec 走成功路径
    monkeypatch.setattr("services.vector_search.index_document", lambda *a, **k: None)

    with patch.object(KbIndexStatusWriter, "begin", spy_begin), \
         patch.object(KbIndexStatusWriter, "finish", spy_finish), \
         patch.object(KbIndexStatusWriter, "note_in_flight", spy_note):
        kb = kb_svc.create_kb(name="writer_async_single", category="national")
        content = b"%PDF-1.4 fake pdf content"
        doc = doc_svc.import_document(kb.id, "test.pdf", content, async_index=True)

        _wait_docs_terminal(kb.id, [doc.id])
        _wait_kb_searchable(kb.id)

    methods_called = [name for name, _ in callback_calls]
    assert "finish" in methods_called, (
        f"_index_single_doc_async 必须调 writer.finish()；实际 callback 序列 {methods_called}"
    )
    # finish 必须以 failed=[]|None + interrupted=None 收尾（成功路径）
    finish_call = next(args for name, args in callback_calls if name == "finish")
    failed, interrupted = finish_call
    assert failed in (None, []), (
        f"成功路径 finish 必须 failed=[]|None；实际 {failed!r}"
    )
    assert interrupted is None, (
        f"成功路径 finish 必须 interrupted=None；实际 {interrupted!r}"
    )


def test_index_single_doc_async_failure_keeps_searchable(monkeypatch, stub_pdf_parse):
    """`_index_single_doc_async` 失败仍写 searchable（保留旧契约）。

    issue #152 AC：单文档异步入口"末尾 `searchable` 写入"。doc.embedding_status
    写 ``failed`` 是 doc 层面的事，KB 检索状态仍写 ``searchable``（KB 视角"已
    处理过这篇"，失败摘要留给 doc 级 `embedding_status`，KB 字段不再分裂语义）。
    """
    from unittest.mock import patch
    from core.kb_index_status import KbIndexStatusWriter

    finish_calls: list[tuple] = []

    real_finish = KbIndexStatusWriter.finish

    def spy_finish(self, failed=None, *, interrupted=None):
        finish_calls.append((failed, interrupted))
        real_finish(self, failed, interrupted=interrupted)

    def _explode(*a, **k):
        raise RuntimeError("simulated index_vec outage")

    monkeypatch.setattr("services.vector_search.index_document", _explode)

    with patch.object(KbIndexStatusWriter, "finish", spy_finish):
        kb = kb_svc.create_kb(name="writer_async_single_fail", category="national")
        content = b"%PDF-1.4 fake pdf content"
        doc = doc_svc.import_document(kb.id, "test.pdf", content, async_index=True)

        _wait_docs_terminal(kb.id, [doc.id])

    assert finish_calls, (
        "_index_single_doc_async 失败仍必须调 writer.finish()（保留 searchable 契约）；实际 0 次"
    )
    failed, interrupted = finish_calls[0]
    assert failed is None or failed == [], (
        f"单文档异步入口 finish 不应传 failed；实际 {failed!r}"
    )
    assert interrupted is None, (
        f"单文档异步入口 finish 不应传 interrupted；实际 {interrupted!r}"
    )


def test_batch_index_docs_uses_writer_for_three_spots(monkeypatch):
    """`_batch_index_docs` 三处状态写入全部走 writer。

    开头 building 占位 → ``begin()`` + ``note_in_flight(first_doc)``；
    末尾 searchable 终态 → ``finish(failed=[])``；
    失败路径走 ``finish(interrupted=...)`` 而不是直接 ``kb.index_current_doc = f"错误: ..."`。

    同步路径（``async_index=False``）让函数在主线程跑完，无须轮询。
    """
    from unittest.mock import MagicMock, patch
    from core.kb_index_status import KbIndexStatusWriter

    callback_calls: list[tuple[str, tuple]] = []

    real_begin = KbIndexStatusWriter.begin
    real_finish = KbIndexStatusWriter.finish
    real_note_in_flight = KbIndexStatusWriter.note_in_flight
    real_advance = KbIndexStatusWriter.advance

    def spy_begin(self):
        callback_calls.append(("begin", ()))
        real_begin(self)

    def spy_finish(self, failed=None, *, interrupted=None):
        callback_calls.append(("finish", (failed, interrupted)))
        real_finish(self, failed, interrupted=interrupted)

    def spy_note(self, doc_name):
        callback_calls.append(("note_in_flight", (doc_name,)))
        real_note_in_flight(self, doc_name)

    def spy_advance(self, done):
        callback_calls.append(("advance", (done,)))
        real_advance(self, done)

    with patch.object(KbIndexStatusWriter, "begin", spy_begin), \
         patch.object(KbIndexStatusWriter, "finish", spy_finish), \
         patch.object(KbIndexStatusWriter, "note_in_flight", spy_note), \
         patch.object(KbIndexStatusWriter, "advance", spy_advance):
        kb = kb_svc.create_kb(name="writer_batch_sync", category="national")
        files = [
            ("a.md", "# A\n\n内容一的内容一的内容一的内容一的内容一的内容一。".encode("utf-8")),
            ("b.md", "# B\n\n内容二的内容二的内容二的内容二的内容二的内容二。".encode("utf-8")),
        ]
        doc_svc.batch_import_documents(kb.id, files, async_index=False)

    methods_called = [name for name, _ in callback_calls]
    # 开头 building 占位 → begin + note_in_flight(first doc)
    assert "begin" in methods_called, (
        f"_batch_index_docs 必须调 writer.begin()；实际 {methods_called}"
    )
    # 末尾 searchable 终态 → finish(failed=[])
    assert "finish" in methods_called, (
        f"_batch_index_docs 末尾必须调 writer.finish()；实际 {methods_called}"
    )
    finish_calls = [args for name, args in callback_calls if name == "finish"]
    assert any(
        (fc == ([], None) or fc == (None, None))
        for fc in finish_calls
    ), (
        f"成功路径 finish 必须 failed=[]|None + interrupted=None；实际 finish calls={finish_calls}"
    )
    # 开头 note_in_flight 至少调一次（"a.md" 是 texts[0]）
    note_calls = [args for name, args in callback_calls if name == "note_in_flight"]
    assert any(nc == ("a.md",) for nc in note_calls), (
        f"开头 note_in_flight 应传入第一篇 doc 名 'a.md'；实际 {note_calls}"
    )


def test_batch_index_docs_failure_path_uses_writer_format_helper(monkeypatch):
    """`_batch_index_docs` 失败路径 → ``finish(interrupted=str(e))``。

    验证 writer 的 ``_format_interruption`` 是失败消息唯一来源 —— ``index_current_doc``
    必须以 ``"批量重新解析中断: "`` 前缀开头（而非旧的 ``"错误: "``）。
    """
    from core.kb_index_status import KbIndexStatusWriter

    # 触发 index_documents_batch 抛错 → 走 except 分支 → finish(interrupted=str(e))
    monkeypatch.setattr(
        "core.index_manager.index_documents_batch",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("simulated batch outage")),
    )

    kb = kb_svc.create_kb(name="writer_batch_fail", category="national")
    files = [
        ("x.md", "# X\n\n内容 X 的内容 X 的内容 X 的内容 X 的内容 X 的内容 X。".encode("utf-8")),
    ]
    doc_svc.batch_import_documents(kb.id, files, async_index=False)

    import storage.kb_repo as kb_repo
    final_kb = kb_repo.get(kb.id)
    assert final_kb is not None
    assert final_kb.index_status == "failed", (
        f"失败路径 KB 终态应为 failed；实际 {final_kb.index_status}"
    )
    # 失败消息走 writer 的 _format_interruption（统一前缀）
    assert final_kb.index_current_doc.startswith("批量重新解析中断: "), (
        f"失败消息必须以 '批量重新解析中断: ' 前缀开头（writer 统一格式）；"
        f"实际 {final_kb.index_current_doc!r}"
    )
    assert "simulated batch outage" in final_kb.index_current_doc, (
        f"失败消息应包含原始错误；实际 {final_kb.index_current_doc!r}"
    )


def test_doc_service_lock_still_held_around_doc_meta_writes(monkeypatch):
    """`_get_lock(kb_id)` 仍是文档生命周期锁，与 writer 的 per-instance lock 并存。

    验证：``_append_doc_ids_atomic`` 内的 ``_get_lock`` 调用顺序不变 —— 锁住
    doc_repo 读—改—写。writer 自己另起一把锁串行化 KB 状态字段。两条资源互不
    替代（issue #152 AC）。
    """
    from unittest.mock import MagicMock
    import services.doc_service as doc_svc_mod
    from core.kb_index_status import KbIndexStatusWriter

    # spy 锁的获取：验证 _append_doc_ids_atomic 仍然 _get_lock 内做 read-modify-write
    lock_acquired: list[str] = []

    original_get_lock = doc_svc_mod._get_lock

    def spy_get_lock(kb_id):
        lock_acquired.append(kb_id)
        return original_get_lock(kb_id)

    monkeypatch.setattr(doc_svc_mod, "_get_lock", spy_get_lock)

    # 同时 stub writer 让它真的不发 kb_repo.update（隔离 doc lifecycle 路径）
    noop_writer = MagicMock(spec=KbIndexStatusWriter)
    monkeypatch.setattr(
        "core.kb_index_status.KbIndexStatusWriter",
        noop_writer,
    )

    kb = kb_svc.create_kb(name="lock_test", category="national")
    doc_svc_mod._append_doc_ids_atomic(kb.id, ["d1", "d2"])

    assert lock_acquired, (
        f"_append_doc_ids_atomic 必须调 _get_lock({kb.id})；实际 0 次"
    )
    assert lock_acquired[0] == kb.id, (
        f"_get_lock 应传入 kb_id={kb.id}；实际 {lock_acquired}"
    )


def test_index_single_doc_async_begin_holds_get_lock(monkeypatch, stub_pdf_parse):
    """`_index_single_doc_async` BEGIN ``kb_writer.begin()`` 仍在 ``_get_lock`` 内。

    issue #152 AC3 守卫回归：把 ``kb_writer.begin()`` 从 ``_get_lock`` 内挪出去
    会让 ``document_ids`` 与 KB 状态字段交错（issue #136 TOCTOU 残留）——
    writer 的 per-instance 锁只保护自己的 read-modify-write，不替代文档
    生命周期锁。验证 BEGIN 段 ``_get_lock`` 仍被持有。
    """
    import threading
    import services.doc_service as doc_svc_mod
    from core.kb_index_status import KbIndexStatusWriter

    # 用追踪 depth 的锁替换 _get_lock 返回的 lock（threading.Lock 自身不可改写）
    depth = 0
    depth_lock = threading.Lock()

    class _TrackedLock:
        def __init__(self, inner: threading.Lock):
            self._inner = inner

        def __enter__(self):
            nonlocal depth
            with depth_lock:
                depth += 1
            return self._inner.__enter__()

        def __exit__(self, exc_type, exc, tb):
            result = self._inner.__exit__(exc_type, exc, tb)
            nonlocal depth
            with depth_lock:
                depth -= 1
            return result

    real_get_lock = doc_svc_mod._get_lock

    def spy_get_lock(kb_id):
        return _TrackedLock(real_get_lock(kb_id))

    # spy writer.begin()：记录调用时 _get_lock 是否已持有
    lock_held_during_begin: list[bool] = []
    lock_depth_at_begin: list[int] = []
    real_begin = KbIndexStatusWriter.begin

    def spy_begin(self):
        with depth_lock:
            lock_held_during_begin.append(depth > 0)
            lock_depth_at_begin.append(depth)
        return real_begin(self)

    monkeypatch.setattr(doc_svc_mod, "_get_lock", spy_get_lock)
    monkeypatch.setattr(KbIndexStatusWriter, "begin", spy_begin)

    # stub _index_vec 走快速成功路径
    monkeypatch.setattr("services.vector_search.index_document", lambda *a, **k: None)

    kb = kb_svc.create_kb(name="lock_at_begin", category="national")
    content = b"%PDF-1.4 fake pdf content"
    doc = doc_svc.import_document(kb.id, "test.pdf", content, async_index=True)

    _wait_docs_terminal(kb.id, [doc.id])
    _wait_kb_searchable(kb.id)

    assert lock_held_during_begin, (
        "_index_single_doc_async 必须至少调一次 writer.begin()；实际 0 次"
    )
    assert all(lock_held_during_begin), (
        f"writer.begin() 调用时 _get_lock 必须已持有；"
        f"实际 lock_depth_at_begin = {lock_depth_at_begin}"
    )
