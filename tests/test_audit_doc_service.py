"""待审核文档服务单元测试"""

import os
import shutil
import tempfile

import pytest

# 设置测试数据目录
os.environ["AUDIT_DATA_DIR"] = tempfile.mkdtemp()

import services.audit_doc_service as audit_doc_svc


@pytest.fixture(autouse=True)
def cleanup():
    """每个测试后清理数据"""
    yield
    import storage.audit_doc_repo as audit_doc_repo
    if audit_doc_repo.AUDIT_DOCS_DIR.exists():
        shutil.rmtree(audit_doc_repo.AUDIT_DOCS_DIR)


def test_upload_document():
    """测试上传文档"""
    content = b"%PDF-1.4 test content"
    doc = audit_doc_svc.upload_document("test.pdf", content)

    assert doc.name == "test.pdf"
    assert doc.file_type == "pdf"
    assert doc.status == "uploaded"
    assert doc.file_path


def test_get_document():
    """测试获取文档"""
    content = b"%PDF-1.4"
    doc = audit_doc_svc.upload_document("test.pdf", content)

    retrieved = audit_doc_svc.get_document(doc.id)
    assert retrieved is not None
    assert retrieved.id == doc.id


def test_list_documents():
    """测试列出文档"""
    content = b"%PDF-1.4"
    doc1 = audit_doc_svc.upload_document("test1.pdf", content)
    doc2 = audit_doc_svc.upload_document("test2.pdf", content)

    docs = audit_doc_svc.list_documents()
    assert len(docs) == 2


def test_delete_document():
    """测试删除文档"""
    content = b"%PDF-1.4"
    doc = audit_doc_svc.upload_document("test.pdf", content)
    doc_id = doc.id

    success = audit_doc_svc.delete_document(doc_id)
    assert success is True

    retrieved = audit_doc_svc.get_document(doc_id)
    assert retrieved is None


def test_parse_pdf_document():
    """测试解析 PDF 文档"""
    # 使用示例文档
    if not os.path.exists("sample_docs/sample_standard.pdf"):
        pytest.skip("示例文档不存在")

    with open("sample_docs/sample_standard.pdf", "rb") as f:
        content = f.read()

    doc = audit_doc_svc.upload_document("sample.pdf", content)
    doc = audit_doc_svc.parse_document(doc.id)

    assert doc.status == "parsed"
    assert doc.page_count is not None
    assert doc.parsed_content is not None
    assert len(doc.parsed_content) > 0


def test_upload_unsupported_format():
    """测试上传不支持的格式"""
    content = b"binary content"
    with pytest.raises(ValueError) as exc_info:
        audit_doc_svc.upload_document("test.exe", content)

    assert "不支持的文件格式" in str(exc_info.value)
