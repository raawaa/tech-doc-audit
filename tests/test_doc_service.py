"""文档服务单元测试"""

import os
import shutil
import tempfile

import pytest

# 设置测试数据目录
os.environ["AUDIT_DATA_DIR"] = tempfile.mkdtemp()

import services.kb_service as kb_svc
import services.doc_service as doc_svc


@pytest.fixture(autouse=True)
def cleanup():
    """每个测试后清理数据"""
    yield
    import storage.kb_repo as kb_repo
    import storage.doc_repo as doc_repo
    if kb_repo.KB_META_DIR.exists():
        shutil.rmtree(kb_repo.KB_META_DIR)
    if doc_repo.KB_DOCS_DIR.exists():
        shutil.rmtree(doc_repo.KB_DOCS_DIR)


def test_import_document():
    """测试导入文档"""
    kb = kb_svc.create_kb(name="测试", category="national")

    # 创建一个简单的 PDF 文件
    content = b"%PDF-1.4 fake pdf content"
    doc = doc_svc.import_document(kb.id, "test.pdf", content)

    assert doc.name == "test.pdf"
    assert doc.file_type == "pdf"
    assert doc.kb_id == kb.id


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
