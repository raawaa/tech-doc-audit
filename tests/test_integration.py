"""端到端集成测试"""

import os
import shutil
import tempfile

import pytest

# 设置测试数据目录
test_dir = tempfile.mkdtemp()
os.environ["AUDIT_DATA_DIR"] = test_dir


@pytest.fixture(autouse=True)
def cleanup():
    """每个测试后清理数据"""
    yield
    # 清理数据目录
    for item in os.listdir(test_dir):
        path = os.path.join(test_dir, item)
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.remove(path)


def test_full_workflow():
    """测试完整的工作流程"""
    # 1. 创建知识库
    import services.kb_service as kb_svc
    kb = kb_svc.create_kb(
        name="测试知识库",
        description="用于集成测试",
        category="national"
    )
    assert kb.id is not None
    assert kb.name == "测试知识库"
    print(f"✓ 知识库创建成功: {kb.id}")

    # 2. 导入文档到知识库（PDF）
    import services.doc_service as doc_svc
    sample_pdf = "sample_docs/sample_standard.pdf"
    if os.path.exists(sample_pdf):
        with open(sample_pdf, "rb") as f:
            content = f.read()
        kb_doc = doc_svc.import_document(kb.id, "标准文档.pdf", content)
        assert kb_doc.id is not None
        assert kb_doc.file_type == "pdf"
        print(f"✓ PDF 文档导入知识库成功: {kb_doc.id}")

    # 3. 导入 Markdown 文档到知识库
    sample_md = "sample_docs/sample.md"
    if os.path.exists(sample_md):
        with open(sample_md, "rb") as f:
            content = f.read()
        md_doc = doc_svc.import_document(kb.id, "技术标准.md", content)
        assert md_doc.id is not None
        assert md_doc.file_type == "md"
        assert md_doc.index_status == "ready"
        print(f"✓ MD 文档导入知识库成功: {md_doc.id}")

    # 4. 上传待审核文档
    import services.audit_doc_service as audit_doc_svc
    sample_audit_path = "sample_docs/sample_standard.pdf"
    if os.path.exists(sample_audit_path):
        with open(sample_audit_path, "rb") as f:
            content = f.read()
        audit_doc = audit_doc_svc.upload_document("待审核文档.pdf", content)
        assert audit_doc.id is not None
        print(f"✓ 待审核文档上传成功: {audit_doc.id}")

        # 5. 解析文档
        audit_doc = audit_doc_svc.parse_document(audit_doc.id)
        assert audit_doc.status == "parsed"
        assert audit_doc.page_count is not None
        print(f"✓ 文档解析成功: {audit_doc.page_count} 页")

        # 6. 创建审核任务
        import services.audit_task_service as task_svc
        task = task_svc.create_task(
            document_id=audit_doc.id,
            kb_ids=[kb.id]
        )
        assert task.id is not None
        print(f"✓ 审核任务创建成功: {task.id}")

        # 7. 列出任务
        tasks = task_svc.list_tasks(audit_doc.id)
        assert len(tasks) == 1
        print(f"✓ 任务列表查询成功")

    print("\n=== 端到端测试通过 ===")


def test_kb_crud():
    """测试知识库 CRUD"""
    import services.kb_service as kb_svc

    # 创建
    kb = kb_svc.create_kb(name="CRUD测试", category="industry")
    assert kb.id is not None

    # 读取
    retrieved = kb_svc.get_kb(kb.id)
    assert retrieved is not None
    assert retrieved.name == "CRUD测试"

    # 列表
    kbs = kb_svc.list_kbs()
    assert len(kbs) >= 1

    # 删除
    success = kb_svc.delete_kb(kb.id)
    assert success is True

    deleted = kb_svc.get_kb(kb.id)
    assert deleted is None
    print("✓ 知识库 CRUD 测试通过")


def test_audit_doc_crud():
    """测试待审核文档 CRUD"""
    import services.audit_doc_service as audit_doc_svc

    # 上传
    content = b"%PDF-1.4 test content"
    doc = audit_doc_svc.upload_document("test.pdf", content)
    assert doc.id is not None
    assert doc.status == "uploaded"

    # 读取
    retrieved = audit_doc_svc.get_document(doc.id)
    assert retrieved is not None

    # 列表
    docs = audit_doc_svc.list_documents()
    assert len(docs) >= 1

    # 删除
    success = audit_doc_svc.delete_document(doc.id)
    assert success is True

    deleted = audit_doc_svc.get_document(doc.id)
    assert deleted is None
    print("✓ 待审核文档 CRUD 测试通过")


def test_invalid_file_type():
    """测试不支持的文件格式"""
    import services.audit_doc_service as audit_doc_svc

    with pytest.raises(ValueError) as exc_info:
        audit_doc_svc.upload_document("test.exe", b"binary")

    assert "不支持的文件格式" in str(exc_info.value)
    print("✓ 不支持文件格式测试通过")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
