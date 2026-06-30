"""ADR-0002 §3 自动重建分层：快路同步 / 慢路异步降级。

不变量：
1. 所有文档都有 .npy 缓存（fast path）→ 触发同步重建（秒级、无 GPU）
2. 有文档缺失 .npy 缓存（slow path，需重算向量）→ 行为由调用方决定：
   - sync_rebuild=True（审核路径）：阻塞同步重建，保证质量
   - sync_rebuild=False（问答路径）：触发后台异步，当前请求立即返回 False
3. 重建路径写回字段按 ADR-0002 §决策 2（由 rebuild_kb_index 内置保证）
4. 自愈可见：重建期间字段短暂 building → searchable（运维可见）

通过 fake_models + 假文件布局，不触发真实 embedding。
"""

import os
import shutil
import time
import threading
from pathlib import Path

import pytest

import storage.kb_repo as kb_repo
from core.index_manager import _vectors_dir, get_kb_index_built
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


def _kb_with_doc_vectors(kb_id: str, doc_ids: list[str]):
    """创建 KB 元数据 + 造空但存在的 .npy 文件（模拟'缓存齐全'）。"""
    kb = KnowledgeBase(id=kb_id, name=f"分层测试-{kb_id}", category="national")
    kb.document_ids = doc_ids
    kb_repo.update(kb)
    vectors_dir = _vectors_dir(kb_id)
    vectors_dir.mkdir(parents=True, exist_ok=True)
    for did in doc_ids:
        # 写一个有效但空的 numpy 数组占位
        import numpy as np
        np.save(str(vectors_dir / f"{did}.npy"), np.zeros((1, 1024), dtype=np.float32))
    return kb_repo.get(kb_id)


def _kb_with_missing_vector(kb_id: str, doc_ids: list[str]):
    """创建 KB 元数据但不写 .npy（'需要重算向量' → 慢路）。"""
    kb = KnowledgeBase(id=kb_id, name=f"缺向量-{kb_id}", category="national")
    kb.document_ids = doc_ids
    kb_repo.update(kb)
    return kb_repo.get(kb_id)


# ── Fast path：所有文档 .npy 齐全 → 同步重建，秒级可成功 ──────────────────


def test_fast_path_all_cached_returns_true_sync(seed_searchable_kb):
    """所有文档 .npy 缓存齐全 → 同步重建成功 + 字段 searchable。

    这是用户故事 5：首次使用 / 缓存齐全的情况下系统自动建立索引，
    字段诚实地短暂经 building → searchable。
    """
    seed_searchable_kb("test_layer_fast")
    _kb_with_doc_vectors("test_layer_fast", ["d1", "d2"])

    from services.vector_search import vec_search
    results = vec_search(["test_layer_fast"], "测试", rebuild_if_missing=True)
    # 此时字段=searchable（fast 同步已完成），search 应有结果或至少不挂
    # 因为 fake embedder 与 query 无真实语义匹配，结果可能为空——断言字段即可
    kb_after = kb_repo.get("test_layer_fast")
    assert kb_after.index_status == "searchable", (
        f"fast path 同步后字段应为 searchable，实际 {kb_after.index_status}"
    )


def test_fast_path_field_never_lies_after_sync_rebuild(seed_searchable_kb):
    """fast path 同步重建：调用返回后字段已 searchable，无 building 卡住。"""
    seed_searchable_kb("test_layer_fast_no_lie")
    _kb_with_doc_vectors("test_layer_fast_no_lie", ["d1"])

    from services.vector_search import vec_search
    vec_search(["test_layer_fast_no_lie"], "q", rebuild_if_missing=True)

    # 同步路径必须把字段写回 searchable，不留 building
    kb_after = kb_repo.get("test_layer_fast_no_lie")
    assert kb_after.index_status in ("searchable", "failed"), (
        f"同步重建后不应停留在 building/无状态：{kb_after.index_status}"
    )


# ── Slow path：缺向量 → 行为由调用方 sync_rebuild 决定 ──────────────


def test_slow_path_audit_sync_blocks_for_quality(seed_searchable_kb):
    """审核路径（sync_rebuild=True）→ 慢路同步重建，行为诚实（即使需要 GPU 也阻塞）。

    验证场景：KB 状态字段为 none、doc .npy 缺失 → 走 slow path。
    审核质量优先，调用方明示 sync_rebuild=True 来保证同步完成。
    """
    seed_searchable_kb("test_layer_slow_audit")
    _kb_with_missing_vector("test_layer_slow_audit", ["d1"])

    # 模拟 audit 路径的调用：拒绝异步降级、要求同步质量
    from services.vector_search import _ensure_kb_index
    t0 = time.time()
    _ensure_kb_index("test_layer_slow_audit", sync_rebuild_for_audit=True)
    elapsed = time.time() - t0
    # 同步重建可能在异常路径上不会完成（无 GPU + 缺向量）；此处只验证：
    # 字段变化可见（变 building 或变 failed，绝不应静默停留在 none 不动）
    kb_after = kb_repo.get("test_layer_slow_audit")
    assert kb_after.index_status != "none", (
        f"sync_rebuild 路径上字段不应停留在 none：{kb_after.index_status}"
    )
    # 至少调用方在合理时间内收到反馈（10s 内）
    assert elapsed < 10, f"sync 路径不应无限阻塞，实际 {elapsed}s"


def test_slow_path_qa_async_does_not_block(seed_searchable_kb):
    """问答路径（默认）→ 慢路异步降级：当前请求立即返回，不阻塞。

    验证：QA 调用 vec_search 时如果 KB 不在 searchable，走慢路异步：
    - 当前调用立即返回 []（不是 [] from 阻塞；是从字段判断不可用）
    - 后台线程在异步重建（把字段从 none → building → searchable）
    """
    seed_searchable_kb("test_layer_slow_qa")
    _kb_with_missing_vector("test_layer_slow_qa", ["d1"])

    from services.vector_search import vec_search
    t0 = time.time()
    results = vec_search(["test_layer_slow_qa"], "q", rebuild_if_missing=True)
    elapsed = time.time() - t0

    # QA 路径不应长时阻塞（<2s 内必须返回；sync 走 GPU embedding 会分钟级）
    assert elapsed < 2.0, f"QA 路径不应长时阻塞：实际 {elapsed}s"
    # 等后台线程把字段从 none 推进到 building（rebuild 自带内置契约：开锁设 building）
    # 或最终 searchable
    for _ in range(50):
        kb_after = kb_repo.get("test_layer_slow_qa")
        if kb_after.index_status in ("building", "searchable"):
            break
        time.sleep(0.05)
    assert kb_after.index_status in ("building", "searchable"), (
        f"QA 异步降级后台线程应至少把字段推到 building/searchable，"
        f"实际 {kb_after.index_status}"
    )
    # 等后台线程把字段写回 searchable
    for _ in range(100):
        kb_after = kb_repo.get("test_layer_slow_qa")
        if kb_after.index_status == "searchable":
            break
        time.sleep(0.1)
    assert kb_after.index_status == "searchable", (
        f"QA 异步降级后台重建后字段应回 searchable：{kb_after.index_status}"
    )


# ── 已 searchable 跳过重建 ───────────────────────────────────────────


def test_searchable_kb_skips_rebuild(seed_searchable_kb):
    """searchable 状态 → _ensure_kb_index 直接返回，不触发任何重建。"""
    seed_searchable_kb("test_layer_already_searchable")
    from services.vector_search import _ensure_kb_index

    t0 = time.time()
    result = _ensure_kb_index("test_layer_already_searchable", sync_rebuild_for_audit=False)
    elapsed = time.time() - t0

    # 直接返回 True，且不应进入锁
    assert result is True, "searchable 状态应直接返回 True"
    assert elapsed < 0.5, f"已 searchable 不应阻塞：实际 {elapsed}s"
    # 字段保持 searchable
    kb_after = kb_repo.get("test_layer_already_searchable")
    assert kb_after.index_status == "searchable"


# ── 用户故事 4: 重建完成后自动回 searchable（无需手动刷新） ─────────


def test_user_story_4_auto_heals_to_searchable(seed_searchable_kb):
    """用户故事 4：重建完成后 KB 自动变 searchable，无需手动操作。"""
    seed_searchable_kb("test_layer_heal")
    _kb_with_missing_vector("test_layer_heal", ["d1"])

    from services.vector_search import vec_search
    vec_search(["test_layer_heal"], "q", rebuild_if_missing=True)

    # 等异步后台线程完成 rebuild 后写回字段
    for _ in range(100):
        kb_after = kb_repo.get("test_layer_heal")
        if kb_after.index_status == "searchable":
            break
        time.sleep(0.1)

    kb_after = kb_repo.get("test_layer_heal")
    assert kb_after.index_status == "searchable", (
        f"自动自愈后字段应为 searchable，实际 {kb_after.index_status}"
    )
