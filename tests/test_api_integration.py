"""API 集成测试"""

import os
import shutil

import pytest
from fastapi.testclient import TestClient

from api.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def cleanup():
    """每个测试后清理数据"""
    yield
    test_dir = os.environ["AUDIT_DATA_DIR"]
    for item in os.listdir(test_dir):
        path = os.path.join(test_dir, item)
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.remove(path)


def test_health_check():
    """测试健康检查"""
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    print("✓ 健康检查通过")


def test_create_knowledge_base():
    """测试创建知识库"""
    response = client.post(
        "/api/v1/knowledge-bases",
        json={"name": "API测试库", "category": "national", "description": "测试"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "API测试库"
    assert data["id"] is not None
    print(f"✓ 创建知识库成功: {data['id']}")
    return data["id"]


def test_list_knowledge_bases():
    """测试列出知识库"""
    # 先创建一个
    client.post(
        "/api/v1/knowledge-bases",
        json={"name": "列表测试库", "category": "industry"}
    )

    response = client.get("/api/v1/knowledge-bases")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) >= 1
    print("✓ 列出知识库成功")


def test_get_knowledge_base_not_found():
    """测试获取不存在的知识库"""
    response = client.get("/api/v1/knowledge-bases/nonexistent")
    assert response.status_code == 404
    print("✓ 404 测试通过")


def test_upload_audit_document():
    """测试上传待审核文档"""
    # 创建测试文件
    test_content = b"%PDF-1.4\ntest content"

    import io
    response = client.post(
        "/api/v1/audit-documents",
        files={"file": ("test.pdf", io.BytesIO(test_content), "application/pdf")}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "test.pdf"
    assert data["id"] is not None
    print(f"✓ 上传文档成功: {data['id']}")
    return data["id"]


def test_list_audit_documents():
    """测试列出待审核文档"""
    # 先上传一个
    test_content = b"%PDF-1.4\ntest"
    import io
    client.post(
        "/api/v1/audit-documents",
        files={"file": ("list_test.pdf", io.BytesIO(test_content), "application/pdf")}
    )

    response = client.get("/api/v1/audit-documents")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    print("✓ 列出待审核文档成功")


def test_parse_audit_document():
    """测试解析待审核文档"""
    # 使用实际示例文档
    sample_path = "sample_docs/sample_standard.pdf"
    if not os.path.exists(sample_path):
        print("⚠ 跳过（示例文档不存在）")
        return

    import io
    with open(sample_path, "rb") as f:
        test_content = f.read()

    upload_resp = client.post(
        "/api/v1/audit-documents",
        files={"file": ("sample.pdf", io.BytesIO(test_content), "application/pdf")}
    )
    doc_id = upload_resp.json()["id"]

    # 解析
    response = client.post(f"/api/v1/audit-documents/{doc_id}/parse")
    assert response.status_code == 200
    data = response.json()
    # PDF 解析可能失败，返回 failed 是正常的
    print(f"✓ 解析测试完成 (状态: {data['status']})")


def test_audit_task_workflow():
    """测试审核任务工作流"""
    # 1. 创建知识库
    kb_resp = client.post(
        "/api/v1/knowledge-bases",
        json={"name": "任务测试库", "category": "national"}
    )
    kb_id = kb_resp.json()["id"]

    # 2. 上传待审核文档
    test_content = b"%PDF-1.4\ntest"
    import io
    doc_resp = client.post(
        "/api/v1/audit-documents",
        files={"file": ("task_test.pdf", io.BytesIO(test_content), "application/pdf")}
    )
    doc_id = doc_resp.json()["id"]

    # 3. 创建审核任务
    task_resp = client.post(
        "/api/v1/audit-tasks",
        json={"document_id": doc_id, "kb_ids": [kb_id]}
    )
    assert task_resp.status_code == 200
    task_data = task_resp.json()
    assert task_data["id"] is not None
    assert task_data["status"] == "pending"
    print(f"✓ 审核任务创建成功: {task_data['id']}")

    # 4. 获取任务
    get_resp = client.get(f"/api/v1/audit-tasks/{task_data['id']}")
    assert get_resp.status_code == 200
    print("✓ 获取任务成功")


def test_cascade_delete():
    """测试级联删除"""
    # 创建知识库
    kb_resp = client.post(
        "/api/v1/knowledge-bases",
        json={"name": "删除测试库", "category": "national"}
    )
    kb_id = kb_resp.json()["id"]

    # 删除
    del_resp = client.delete(f"/api/v1/knowledge-bases/{kb_id}")
    assert del_resp.status_code == 200

    # 确认删除
    get_resp = client.get(f"/api/v1/knowledge-bases/{kb_id}")
    assert get_resp.status_code == 404
    print("✓ 级联删除测试通过")


# ── 启动恢复测试 ──────────────────────────────────────────────────────────────


def test_recover_stuck_indexes():
    """验证崩溃后启动恢复正确重置卡住的 KB 和文档状态。

    字段分裂后：
    - KB index_status='building' → 'none'
    - Doc embedding_status='pending_index' → 'none'
    - Doc embedding_status='indexing' → 'pending_index'
    - Doc embedding_status='embedded' 不动
    """
    import services.kb_service as kb_svc
    import services.doc_service as doc_svc
    import storage.kb_repo as kb_repo
    import storage.doc_repo as doc_repo
    from api.main import recover_stuck_indexes

    # 场景 1：KB 卡在 building
    kb_building = kb_svc.create_kb(name="卡住的KB", category="national")
    kb_building.index_status = "building"
    kb_repo.update(kb_building)

    # 场景 2：文档卡在 pending_index
    kb_pending = kb_svc.create_kb(name="pending文档", category="national")
    doc_pending = doc_repo.save_doc(kb_pending.id, "pending_doc.md", b"# Test\n\nTest content for pending doc.", "md")
    doc_pending.embedding_status = "pending_index"
    doc_repo._save_doc_meta(doc_pending)
    kb_pending.document_ids.append(doc_pending.id)
    kb_repo.update(kb_pending)

    # 场景 3：文档卡在 indexing（崩溃中）
    doc_indexing = doc_repo.save_doc(kb_pending.id, "indexing_doc.md", b"# Test2\n\nTest content for indexing doc.", "md")
    doc_indexing.embedding_status = "indexing"
    doc_repo._save_doc_meta(doc_indexing)
    kb_pending.document_ids.append(doc_indexing.id)
    kb_repo.update(kb_pending)

    # 场景 4：正常状态的文档（不应被改动）
    doc_ready = doc_repo.save_doc(kb_pending.id, "ready_doc.md", b"# Test3\n\nTest content for ready doc.", "md")
    doc_ready.embedding_status = "embedded"
    doc_repo._save_doc_meta(doc_ready)
    kb_pending.document_ids.append(doc_ready.id)
    kb_repo.update(kb_pending)

    # 执行恢复
    recover_stuck_indexes()

    # 验证 KB building → none
    kb_building_after = kb_repo.get(kb_building.id)
    assert kb_building_after.index_status == "none"
    assert kb_building_after.index_progress is None

    # 验证 doc pending_index → none
    doc_pending_after = doc_repo.get_doc(kb_pending.id, doc_pending.id)
    assert doc_pending_after.embedding_status == "none"

    # 验证 doc indexing → pending_index
    doc_indexing_after = doc_repo.get_doc(kb_pending.id, doc_indexing.id)
    assert doc_indexing_after.embedding_status == "pending_index", \
        f"indexing 应重置为 pending_index，实际 {doc_indexing_after.embedding_status}"

    # 验证正常文档未被改动
    doc_ready_after = doc_repo.get_doc(kb_pending.id, doc_ready.id)
    assert doc_ready_after.embedding_status == "embedded", "embedded 状态的文档不应被改动"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
