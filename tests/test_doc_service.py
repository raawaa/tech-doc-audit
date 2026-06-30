"""文档服务单元测试"""

import shutil
import time

import pytest

import services.kb_service as kb_svc
import services.doc_service as doc_svc


@pytest.fixture(autouse=True)
def cleanup():
    """每个测试后清理数据"""
    yield
    import storage.kb_repo as kb_repo
    if kb_repo.KBS_DIR.exists():
        shutil.rmtree(kb_repo.KBS_DIR)


def test_import_document():
    """测试导入文档"""
    kb = kb_svc.create_kb(name="测试", category="national")

    # 创建一个简单的 PDF 文件
    content = b"%PDF-1.4 fake pdf content"
    doc = doc_svc.import_document(kb.id, "test.pdf", content)

    assert doc.name == "test.pdf"
    assert doc.file_type == "pdf"
    assert doc.kb_id == kb.id


def test_import_document_async():
    """测试异步导入文档（async_index=True 生产路径）。"""
    import storage.kb_repo as kb_repo

    kb = kb_svc.create_kb(name="测试异步", category="national")

    content = b"%PDF-1.4 fake pdf content"
    doc = doc_svc.import_document(kb.id, "test.pdf", content, async_index=True)

    # async 导入返回时可能仍在 pending_index，也可能已被后台线程标记为 indexing
    assert doc.embedding_status in ("pending_index", "indexing")

    # 等待后台线程完成（fake PDF 空文本 → 快速返回）
    import storage.doc_repo as _dr
    for _ in range(50):
        try:
            doc = _dr.get_doc(kb.id, doc.id)
        except Exception:
            time.sleep(0.1)
            continue
        if doc and doc.embedding_status in ("embedded", "failed"):
            break
        time.sleep(0.1)

    assert doc.embedding_status in ("embedded", "failed"), (
        f"expected embedded/failed, got {doc.embedding_status}"
    )

    # 验证 document_ids 包含该文档
    kb = kb_repo.get(kb.id)
    assert kb is not None
    assert doc.id in kb.document_ids

    # 验证 KB 状态恢复 searchable
    assert kb.index_status == "searchable"


def test_import_document_async_multiple():
    """测试多次异步导入（防 document_ids 被覆盖）。"""
    import storage.kb_repo as kb_repo

    kb = kb_svc.create_kb(name="测试并发异步", category="national")

    docs = []
    for i in range(5):
        content = "fake pdf content {}".format(i).encode()
        doc = doc_svc.import_document(kb.id, f"test_{i}.pdf", content, async_index=True)
        docs.append(doc)

    # 等待所有后台线程完成（状态变为 embedded 或 failed）
    for _ in range(100):
        not_done = [d for d in docs if d.embedding_status not in ("embedded", "failed")]
        if not not_done:
            break
        time.sleep(0.1)

    # 验证所有文档 id 都在 kb.document_ids 中
    kb = kb_repo.get(kb.id)
    assert kb is not None
    for doc in docs:
        assert doc.id in kb.document_ids, f"doc {doc.id} not in document_ids"

    # 异步路径在每篇 doc 索引完成时都会把 KB 写 searchable，但并发调度下
    # 最后一个线程可能把自己的状态写完后整体可见——轮询直到 searchable
    for _ in range(100):
        kb = kb_repo.get(kb.id)
        if kb.index_status == "searchable":
            break
        time.sleep(0.1)
    assert kb.index_status == "searchable"


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
