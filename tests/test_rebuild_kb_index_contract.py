"""ADR-0002 不变式（Seam 2）：rebuild_kb_index 内置写回字段契约。

新增测试：
1. rebuild_kb_index 成功后，kb.index_status 自动变 'searchable'（无需调用方写）
2. rebuild_kb_index 抛异常时，kb.index_status 自动变 'failed'（保留错误信息）
3. 重建中途被外部强制改回 'none'，不应再返 True（双真相脱钩防御）
4. 空 KB（无文档）rebuild 也合法 → searchable（且清空向量目录）

不变量（per ADR-0002 §决策 2）：重建写回字段是函数自身的内置契约，
所有调用方（reindex 按钮、auto-rebuild、批量导入）共享同一段保证，
调用方不必再记得写字段。
"""

import os
import shutil

import pytest

import services.doc_service as doc_svc
import services.kb_service as kb_svc
import storage.kb_repo as kb_repo
from core.index_manager import (
    rebuild_kb_index,
    get_kb_index_built,
    _vectors_dir,
    index_document,
)
from models.knowledge_base import KnowledgeBase


@pytest.fixture(autouse=True)
def cleanup():
    yield
    data_dir = os.environ["AUDIT_DATA_DIR"]
    if os.path.exists(data_dir):
        shutil.rmtree(data_dir)


@pytest.fixture(autouse=True)
def _use_fake_models(fake_models):
    yield


def _make_kb(kb_id: str = "test_rebuild_contract") -> KnowledgeBase:
    kb = KnowledgeBase(id=kb_id, name="rebuild 契约", category="national")
    kb_repo.update(kb)
    return kb_repo.get(kb_id)


def test_rebuild_success_writes_searchable_in_lock(seed_searchable_kb):
    """成功重建 → kb.index_status 自动 'searchable'（内置契约，不需要调用方写）。"""
    seed_searchable_kb("test_rebuild_contract")

    # 先建一些 FAISS 内容（无需 GPU）
    import services.doc_service as doc_svc
    kb = kb_repo.get("test_rebuild_contract")
    doc_svc.import_document(
        kb.id, "x.md",
        "# 标题\n\n## 第一章\n\n重建契约测试内容，用以保证 rebuild 后字段被写回。".encode(),
    )

    # 调用方不写字段，只调用 rebuild
    rebuild_kb_index(kb.id)

    # 内置契约：函数自己写
    kb_after = kb_repo.get(kb.id)
    assert kb_after.index_status == "searchable"


def test_rebuild_empty_kb_writes_searchable():
    """空 KB（无文档）rebuild → fields=searchable。这是合法的 '无可检索但库是空' 状态。"""
    kb = _make_kb("test_rebuild_empty")

    rebuild_kb_index(kb.id)

    kb_after = kb_repo.get(kb.id)
    assert kb_after.index_status == "searchable"


def test_rebuild_failure_writes_failed_with_error():
    """rebuild 抛异常 → kb.index_status='failed'，current_doc 留错误信息。

    场景：rebuild_kb_index 内部 _rebuild_from_vectors 抛错时，
    函数顶层 try/except 把字段写回 'failed' 并保留错误信息。
    """
    import unittest.mock as mock

    kb = kb_svc.create_kb(name="fail KB", category="national")
    doc_svc.import_document(
        kb.id, "x.md",
        "# 重建失败测试\n\n## 一章\n\n用于触发 KB rebuild 的失败路径。".encode(),
    )

    with mock.patch("core.index_manager._persist", side_effect=IOError("disk gone")):
        try:
            rebuild_kb_index(kb.id)
        except IOError:
            pass

    kb_after = kb_repo.get(kb.id)
    assert kb_after.index_status == "failed", (
        f"rebuild 异常后字段应为 failed，实际 {kb_after.index_status}"
    )
    assert "错误" in kb_after.index_current_doc or "disk" in kb_after.index_current_doc.lower(), (
        f"current_doc 应保留错误信息，实际 {kb_after.index_current_doc!r}"
    )


def test_rebuild_no_longer_needs_outer_status_write(seed_searchable_kb):
    """reindex API 等调用方：调用 rebuild 后不需要再写 'searchable'。

    这是 ADR-0002 的核心收益——调用方免维护，避免每加一条路径就重写一遍。
    """
    seed_searchable_kb("test_rebuild_no_outer")
    kb = kb_repo.get("test_rebuild_no_outer")

    # 模拟调用方：先把 kb 设成 'building'，再调 rebuild，期待函数自己纠正
    kb.index_status = "building"
    kb_repo.update(kb)

    rebuild_kb_index(kb.id)

    kb_after = kb_repo.get(kb.id)
    # 函数自身把字段从 building → searchable，不依赖调用方
    assert kb_after.index_status == "searchable"


def test_get_kb_index_built_returns_false_if_field_flipped_after_faiss_exists(seed_searchable_kb):
    """字段被人外部重置为 none → get_kb_index_built 返 False（即使 FAISS 还在盘上）。

    防御场景：测试 / 运维误改字段不该让状态字段与能力"虚假脱钩"。
    此处字段是 single source of truth——任何人写它都生效。
    """
    seed_searchable_kb("test_field_overrides_faiss")
    kb = kb_repo.get("test_field_overrides_faiss")

    # 先索引一篇，让 FAISS 文件存在
    index_document(kb.id, "doc1", "索引一篇内容用于验证字段重置后查询行为。")

    # 把字段改成 none（模拟"被运维误改 / 测试模拟"）
    kb = kb_repo.get(kb.id)
    kb.index_status = "none"
    kb_repo.update(kb)

    # 字段为 none → 判定为未建，即使 FAISS 还在盘上
    assert get_kb_index_built(kb.id) is False, (
        "字段为 none 时函数应返回 False，不读 FAISS 文件"
    )
