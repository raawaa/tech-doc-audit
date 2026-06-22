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

    assert doc.index_status == "pending_index"

    # 等待后台线程完成（fake PDF 空文本 → 快速返回）
    for _ in range(50):
        if doc.index_status != "pending_index":
            break
        time.sleep(0.1)

    assert doc.index_status in ("ready", "failed"), f"expected ready/failed, got {doc.index_status}"

    # 验证 document_ids 包含该文档
    kb = kb_repo.get(kb.id)
    assert kb is not None
    assert doc.id in kb.document_ids

    # 验证 KB 状态恢复 ready
    assert kb.index_status == "ready"


def test_import_document_async_multiple():
    """测试多次异步导入（防 document_ids 被覆盖）。"""
    import storage.kb_repo as kb_repo

    kb = kb_svc.create_kb(name="测试并发异步", category="national")

    docs = []
    for i in range(5):
        content = "fake pdf content {}".format(i).encode()
        doc = doc_svc.import_document(kb.id, f"test_{i}.pdf", content, async_index=True)
        docs.append(doc)

    # 等待所有后台线程完成
    for _ in range(100):
        pending = [d for d in docs if d.index_status == "pending_index"]
        if not pending:
            break
        time.sleep(0.1)

    # 验证所有文档 id 都在 kb.document_ids 中
    kb = kb_repo.get(kb.id)
    assert kb is not None
    for doc in docs:
        assert doc.id in kb.document_ids, f"doc {doc.id} not in document_ids"

    assert kb.index_status == "ready"


def test_batch_import_documents_async():
    """测试批量异步导入。"""
    import storage.kb_repo as kb_repo

    kb = kb_svc.create_kb(name="测试批量异步", category="national")

    files = [
        ("doc_1.pdf", b"%PDF-1.4 content 1"),
        ("doc_2.pdf", b"%PDF-1.4 content 2"),
        ("doc_3.pdf", b"%PDF-1.4 content 3"),
    ]

    docs = doc_svc.batch_import_documents(kb.id, files, async_index=True)
    assert len(docs) == 3

    # 等待后台线程完成
    for _ in range(100):
        if all(d.index_status != "pending_index" for d in docs):
            break
        time.sleep(0.1)

    # 验证所有文档都在 kb.document_ids 中
    kb = kb_repo.get(kb.id)
    assert kb is not None
    for doc in docs:
        assert doc.id in kb.document_ids, f"doc {doc.id} not in document_ids"

    assert kb.index_status == "ready"


# ── Markdown 文档导入 ────────────────────────────────────────────────────────


def test_import_markdown_document():
    """测试导入 .md 文件（同步索引，含 ## 标题触发 MarkdownNodeParser）。"""
    kb = kb_svc.create_kb(name="测试MD导入", category="national")

    content = "# 设计说明\n\n## 第一章 总则\n\n这是总则内容。\n\n## 第二章 要求\n\n这是具体要求内容。".encode()
    doc = doc_svc.import_document(kb.id, "设计说明.md", content)

    assert doc.name == "设计说明.md"
    assert doc.file_type == "md"
    assert doc.kb_id == kb.id
    assert doc.index_status == "ready"


def test_import_markdown_document_async():
    """测试异步导入 .md 文件。"""
    import storage.kb_repo as kb_repo

    kb = kb_svc.create_kb(name="测试MD异步", category="national")

    content = "# 施工规范\n\n## 第一章 总则\n\n施工规范测试内容。\n\n## 第二章 要求\n\n具体要求内容。".encode()
    doc = doc_svc.import_document(kb.id, "施工规范.md", content, async_index=True)

    assert doc.index_status == "pending_index"

    # 等待后台线程完成（MD 提取快速返回）
    for _ in range(50):
        if doc.index_status != "pending_index":
            break
        time.sleep(0.1)

    assert doc.index_status in ("ready", "failed"), f"expected ready/failed, got {doc.index_status}"

    # 验证 KB 状态恢复 ready
    kb = kb_repo.get(kb.id)
    assert kb is not None
    assert doc.id in kb.document_ids
    assert kb.index_status == "ready"


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
    assert doc.index_status == "ready"
    assert kb.index_status == "ready"


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
