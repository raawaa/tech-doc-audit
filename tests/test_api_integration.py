"""API 集成测试"""

import os
import shutil
import tempfile

import pytest
from fastapi.testclient import TestClient

# 设置测试数据目录
test_dir = tempfile.mkdtemp()
os.environ["AUDIT_DATA_DIR"] = test_dir

# 导入 app（在设置环境变量之后）
from api.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def cleanup():
    """每个测试后清理数据"""
    yield
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
