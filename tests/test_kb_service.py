"""知识库服务单元测试"""

import shutil

import pytest

import services.kb_service as kb_svc


@pytest.fixture(autouse=True)
def cleanup():
    """每个测试后清理数据"""
    yield
    import storage.kb_repo as kb_repo
    if kb_repo.KBS_DIR.exists():
        shutil.rmtree(kb_repo.KBS_DIR)


def test_create_kb():
    """测试创建知识库"""
    kb = kb_svc.create_kb(
        name="测试知识库",
        description="测试描述",
        category="national",
    )
    assert kb.name == "测试知识库"
    assert kb.category == "national"
    assert kb.index_status == "none"


def test_get_kb():
    """测试获取知识库"""
    kb = kb_svc.create_kb(name="测试", category="national")
    retrieved = kb_svc.get_kb(kb.id)
    assert retrieved is not None
    assert retrieved.id == kb.id
    assert retrieved.name == kb.name


def test_get_nonexistent_kb():
    """测试获取不存在的知识库"""
    result = kb_svc.get_kb("nonexistent-id")
    assert result is None


def test_list_kbs():
    """测试列出知识库"""
    kb1 = kb_svc.create_kb(name="知识库1", category="national")
    kb2 = kb_svc.create_kb(name="知识库2", category="industry")

    all_kbs = kb_svc.list_kbs()
    assert len(all_kbs) == 2

    national_kbs = kb_svc.list_kbs(category="national")
    assert len(national_kbs) == 1
    assert national_kbs[0].id == kb1.id


def test_delete_kb():
    """测试删除知识库"""
    kb = kb_svc.create_kb(name="待删除", category="national")
    kb_id = kb.id

    success = kb_svc.delete_kb(kb_id)
    assert success is True

    retrieved = kb_svc.get_kb(kb_id)
    assert retrieved is None
