"""KbIndexStatusWriter 单测（issue #148 / #147）—— 8 条不变式。

writer 是 KB 检索状态字段（``index_status`` / ``index_progress`` /
``index_current_doc``）的**唯一写入者**。本文件用 stub ``kb_repo`` 隔离
存储层，验证 writer 的外部契约（写入序列与字段取值），不验证内部实现
（私有 ``_lock`` 等）。
"""
from __future__ import annotations

import threading
from typing import Optional

import pytest

from models.knowledge_base import KnowledgeBase


# ── 桩 ─────────────────────────────────────────────────────────────


class _StubKbRepo:
    """In-memory 替身，hold 一个 KB 镜像，记录所有 update 调用。

    ``set_kb_deleted()`` 把 ``get`` 切回 ``None``，模拟 KB 被删中途写入。
    """

    def __init__(self, kb_id: str = "stub_kb") -> None:
        self._kb_id = kb_id
        self._kb = KnowledgeBase(id=kb_id, name="stub", category="national")
        self.updates: list[KnowledgeBase] = []

    def get(self, kb_id: str) -> Optional[KnowledgeBase]:
        if kb_id != self._kb_id:
            return None
        return self._kb  # None when deleted

    def update(self, kb: KnowledgeBase) -> KnowledgeBase:
        self.updates.append(kb)
        self._kb = kb
        return kb

    def set_kb_deleted(self) -> None:
        self._kb = None  # type: ignore[assignment]


@pytest.fixture
def stub_and_writer(monkeypatch):
    """注入 stub kb_repo，构造 writer；返回 ``(stub, writer)``。

    writer 的 ``kb_repo`` 引用在 ``core.kb_index_status`` 模块级 —— 测试
    在 writer import 之前 monkeypatch 同名模块即可，不必传 stub 进去。
    """
    from core import kb_index_status

    stub = _StubKbRepo()
    monkeypatch.setattr(kb_index_status.kb_repo, "get", stub.get)
    monkeypatch.setattr(kb_index_status.kb_repo, "update", stub.update)
    writer = kb_index_status.KbIndexStatusWriter(kb_id=stub._kb_id, total=3)
    return stub, writer


def _latest_update(stub: _StubKbRepo) -> KnowledgeBase:
    """返回 stub 收到的最后一次 update 入参。"""
    assert stub.updates, "writer 还没写过任何 KB"
    return stub.updates[-1]


# ── 不变式 1: begin() ──────────────────────────────────────────────


def test_begin_writes_building_with_zero_progress(stub_and_writer):
    _stub, writer = stub_and_writer

    writer.begin()

    kb = _latest_update(_stub)
    assert kb.index_status == "building"
    assert kb.index_progress == 0
    assert kb.index_current_doc == ""


# ── 不变式 2: note_in_flight(name) ─────────────────────────────────


def test_note_in_flight_writes_current_doc_name(stub_and_writer):
    _stub, writer = stub_and_writer

    writer.note_in_flight("foo.pdf")

    kb = _latest_update(_stub)
    assert kb.index_current_doc == "foo.pdf"
    # 中途不改 state / progress
    assert kb.index_status == "none"
    assert kb.index_progress is None


# ── 不变式 3: advance(n) —— 单调非递减（并发乱序也成立）────────────


def test_advance_writes_progress_fraction_of_total(stub_and_writer):
    _stub, writer = stub_and_writer

    writer.advance(1)

    kb = _latest_update(_stub)
    assert kb.index_progress == pytest.approx(1 / 3)


def test_advance_is_monotonic_under_concurrent_out_of_order(stub_and_writer):
    """并发乱序调用 advance 时，最终落盘的 progress 一定 ≥ 任意一次传入。"""
    _stub, writer = stub_and_writer

    # 模拟"完成回调乱序"：线程 1 在主线程切到 _progress=2 之前先跑完
    # _write(``progress=1``)，但线程 2 把 _progress=2 抢在前面记下了。
    # 期望：最终至少是 2/3，不会倒退到 1/3。
    barrier = threading.Barrier(3)

    def _race(target: int) -> None:
        barrier.wait()
        writer.advance(target)

    threads = [threading.Thread(target=_race, args=(n,)) for n in (1, 2)]
    for t in threads:
        t.start()
    barrier.wait()  # 让主线程也撞一次 barrier，确保 3 个一起跑
    writer.advance(2)
    for t in threads:
        t.join()

    # 收集所有 update —— 整批写入里 progress 单调不减
    progresses = [u.index_progress for u in _stub.updates]
    assert progresses, "应至少写一次"
    # max 入参是 2/3 ⇒ 落盘 progress 必须 ≥ 2/3
    assert max(progresses) >= 2 / 3 - 1e-9
    # 序列单调不减
    for prev, curr in zip(progresses, progresses[1:]):
        assert curr >= prev - 1e-9, f"progress 倒退: {prev} -> {curr}"


# ── 不变式 4: finish(failed=[]) ────────────────────────────────────


def test_finish_with_empty_failed_writes_searchable(stub_and_writer):
    _stub, writer = stub_and_writer

    writer.finish(failed=[])

    kb = _latest_update(_stub)
    assert kb.index_status == "searchable"
    assert kb.index_progress == 1
    assert kb.index_current_doc == ""


# ── 不变式 5: finish(failed=[...]) ─────────────────────────────────


def test_finish_with_failures_writes_failed_and_summary(stub_and_writer):
    _stub, writer = stub_and_writer
    failed = [("a.pdf", "ocr crashed"), ("b.pdf", "timeout")]

    writer.finish(failed=failed)

    kb = _latest_update(_stub)
    assert kb.index_status == "failed"
    assert kb.index_progress == 1
    assert kb.index_current_doc == "批量重新解析失败 2/3 篇（a.pdf: ocr crashed；b.pdf: timeout）"


# ── 不变式 6: finish(interrupted="reason") ─────────────────────────


def test_finish_with_interrupted_writes_failed_and_interruption(stub_and_writer):
    _stub, writer = stub_and_writer

    writer.finish(failed=[], interrupted="Ctrl-C")

    kb = _latest_update(_stub)
    assert kb.index_status == "failed"
    assert kb.index_progress == 1
    assert kb.index_current_doc == "批量重新解析中断: Ctrl-C"


# ── 不变式 7: clear_building() ─────────────────────────────────────


def test_clear_building_writes_none_and_clears(stub_and_writer):
    _stub, writer = stub_and_writer

    writer.clear_building()

    kb = _latest_update(_stub)
    assert kb.index_status == "none"
    assert kb.index_progress is None
    assert kb.index_current_doc == ""


# ── 不变式 8: KB 已删 → 静默 return ────────────────────────────────


@pytest.mark.parametrize(
    "method, kwargs",
    [
        ("begin", {}),
        ("note_in_flight", {"doc_name": "x.pdf"}),
        ("advance", {"done": 1}),
        ("finish", {"failed": []}),
        ("clear_building", {}),
    ],
)
def test_all_calls_silently_return_when_kb_deleted(stub_and_writer, method, kwargs):
    """KB 已删 → 所有调用静默 return，不抛、不写。"""
    stub, writer = stub_and_writer
    stub.set_kb_deleted()
    before = len(stub.updates)

    getattr(writer, method)(**kwargs)  # 必须不抛

    assert len(stub.updates) == before, "KB 已删时不应触发 update"
