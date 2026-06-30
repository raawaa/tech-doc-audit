"""ADR-0002 不变式单元测试：kb.index_status 字段为'是否可检索'的唯一真相。

不变量（per ADR-0002 §决策）：
1. get_kb_index_built() 返回值始终等于 kb.index_status=='searchable'
2. 删掉 default__vector_store.json 不应让上述判定反转（文件是缓存，非真相）
3. 字段为 searchable 但磁盘文件不存在 → 仍判定为已建（待重建/自愈目标）
4. 字段为 none/building/failed → 判定为未建

通过 fake_models 夹具，避免加载 bge-m3，并人工控制 kb 元数据与磁盘文件。
"""

import os
import shutil
from pathlib import Path

import pytest

import services.kb_service as kb_svc
import storage.kb_repo as kb_repo


@pytest.fixture(autouse=True)
def cleanup():
    yield
    data_dir = os.environ["AUDIT_DATA_DIR"]
    if os.path.exists(data_dir):
        shutil.rmtree(data_dir)


@pytest.fixture(autouse=True)
def _use_fake_models(fake_models):
    """避免 bge-m3 加载。"""
    yield


def _seed_kb_with_searchable(kb_id: str = "test_kb_src"):
    """构造一个 kb.index_status='searchable' 但无任何索引文件的 KB。"""
    kb = kb_svc.create_kb(name="单真相 KB", category="national")
    kb.index_status = "searchable"
    kb_repo.update(kb)
    return kb


# ── 单真相不变量 ──────────────────────────────────────────────────


def test_get_kb_index_built_follows_field_searchable():
    """字段=searchable → 函数返回 True（无需看磁盘）。"""
    from core.index_manager import get_kb_index_built, _vectors_dir

    kb = _seed_kb_with_searchable()

    # 删除可能不存在的索引目录（彻底无 FAISS 文件）
    vectors_dir = _vectors_dir(kb.id)
    if vectors_dir.exists():
        shutil.rmtree(vectors_dir)

    assert get_kb_index_built(kb.id) is True, (
        "字段说 searchable → 函数应返回 True，即使磁盘无任何索引文件"
    )


def test_get_kb_index_built_returns_false_when_field_none():
    """字段=none → 函数返回 False（即便磁盘上有人造了 FAISS 文件也不变）。"""
    from core.index_manager import get_kb_index_built, _vectors_dir

    kb = kb_svc.create_kb(name="空 KB", category="national")
    # 强行构造一个伪造的索引文件
    vectors_dir = _vectors_dir(kb.id)
    vectors_dir.mkdir(parents=True, exist_ok=True)
    (vectors_dir / "default__vector_store.json").write_text("{}")

    # 字段保持 none
    assert kb.index_status == "none"
    # 函数读字段，不被伪造文件影响
    assert get_kb_index_built(kb.id) is False, (
        "字段为 none → 函数应返回 False，即使磁盘存在 fake FAISS 文件"
    )


def test_get_kb_index_built_returns_false_when_field_building():
    from core.index_manager import get_kb_index_built, _vectors_dir

    kb = kb_svc.create_kb(name="building KB", category="national")
    kb.index_status = "building"
    kb_repo.update(kb)
    # 旧版误把 FAISS 文件视为'已建'，重建中状态会被错误返回 True

    assert kb.index_status == "building"
    # building 视为尚未完成 → False
    assert get_kb_index_built(kb.id) is False


def test_get_kb_index_built_returns_false_for_missing_kb():
    """不存在的 KB → False（与文件 fallback 行为一致）。"""
    from core.index_manager import get_kb_index_built

    assert get_kb_index_built("nonexistent_kb") is False


# ── 缓存可重生性 ──────────────────────────────────────────────


def test_delete_faiss_files_does_not_flip_truth():
    """验证 ADR-0002 §决策：'FAISS 文件降级为可从字段与文档重生的缓存'。

    即使运维误删 FAISS 缓存文件，只要 kb.index_status='searchable'，函数仍返 True
    ——故障不会让状态字段与能力脱钩。
    """
    from core.index_manager import get_kb_index_built, _vectors_dir

    kb = _seed_kb_with_searchable()
    vectors_dir = _vectors_dir(kb.id)
    vectors_dir.mkdir(parents=True, exist_ok=True)
    # 造一个空 FAISS 文件再删掉
    (vectors_dir / "default__vector_store.json").write_text("{}")
    (vectors_dir / "docstore.json").write_text("{}")
    (vectors_dir / "index_store.json").write_text("{}")
    assert get_kb_index_built(kb.id) is True

    # 删除所有索引文件
    for fname in ["default__vector_store.json", "docstore.json", "index_store.json"]:
        f = vectors_dir / fname
        if f.exists():
            f.unlink()

    # 字段没变 → 仍 True（ADR-0002 单真相）
    assert get_kb_index_built(kb.id) is True, (
        "删 FAISS 文件后状态仍应为已建；不应让'已建'变成'未建'"
    )
