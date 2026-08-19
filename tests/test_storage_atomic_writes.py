"""storage repo 原子写测试 (issue #157)。

3 个 storage repo 的 save_* 函数历史上用 ``open("w")`` truncate-then-write：
daemon 线程写入期间读端会撞空文件 → JSONDecodeError。修法：tmp +
``os.replace`` 原子写（``storage/kb_repo.py`` 既有模板）。

POSIX 保证 ``rename`` 原子；read 端要么读到旧版要么读到完整新版，不会撞半截。

本测试覆盖三个 save_* 路径在并发读 / 写下的稳定性。减慢 ``json.dump``
把 race window 从微秒级撑到 50ms，让 race 在测试里可靠触发——
非原子写必爆，原子写稳过。
"""

from __future__ import annotations

import json
import threading
import time
from typing import Callable

import pytest

import storage.audit_doc_repo as audit_doc_repo
import storage.audit_task_repo as audit_task_repo
import storage.doc_repo as doc_repo
from models.audit_document import AuditDocument
from models.audit_task import AuditResult, AuditTask, ResultSummary
from models.document import KBDocument


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_big_audit_task() -> AuditTask:
    """撑宽 save 的 race window：较大 raw_analysis 让 json.dump 持续 ~50ms。"""
    task = AuditTask(
        document_id="doc1",
        document_name="payload.pdf",
        kb_ids=["kb1"],
    )
    task.result = AuditResult(
        task_id=task.id,
        document_id="doc1",
        document_name="payload.pdf",
        summary=ResultSummary(),
        issues=[],
        raw_analysis="x" * 50_000,
    )
    return task


def _make_big_audit_doc() -> AuditDocument:
    doc = AuditDocument(
        id="audit_doc1",
        name="payload.pdf",
        original_name="payload.pdf",
        file_type="pdf",
        file_path="/tmp/payload.pdf",
        status="parsed",
    )
    doc.parsed_content = "x" * 50_000
    return doc


def _slow_dump_factory(monkeypatch, module_name: str, delay_s: float = 0.05) -> None:
    """把 ``json.dump`` 注入 50ms 延迟，撑宽 race window。

    非原子写期间：reader 有充足时间撞 ``truncate`` 已发生 / ``dump`` 未完成的
    空文件 → JSONDecodeError。原子写期间：reader 只看得到旧版或完整新版。
    """
    import json as _json

    orig_dump = _json.dump

    def slow_dump(obj, fp, **kwargs):
        time.sleep(delay_s)
        return orig_dump(obj, fp, **kwargs)

    monkeypatch.setattr(f"{module_name}.json.dump", slow_dump)


def _run_concurrent_save_and_get(
    *,
    write_iterations: int,
    read_iterations: int,
    writer: Callable[[int], None],
    reader: Callable[[], object],
) -> list[Exception]:
    """起 daemon writer，主线程反复 read；返回读端抛的所有 JSONDecodeError。

    读端只把 ``json.JSONDecodeError`` 视为契约违反；其他异常（KeyError 等）
    说明测试本身有问题，向上冒不吞。
    """
    stop = threading.Event()
    errors: list[Exception] = []

    def writer_loop():
        for i in range(write_iterations):
            if stop.is_set():
                break
            try:
                writer(i)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

    t = threading.Thread(target=writer_loop, daemon=True)
    t.start()
    try:
        for _ in range(read_iterations):
            try:
                reader()
            except json.JSONDecodeError as e:
                errors.append(e)
    finally:
        stop.set()
        t.join()
    return errors


# ── audit_task_repo ───────────────────────────────────────────────────────────


def test_audit_task_repo_save_is_atomic_for_concurrent_readers(monkeypatch):
    """audit_task_repo.save_task + get_task 并发，read 端不应撞 JSONDecodeError。"""
    _slow_dump_factory(monkeypatch, "storage.audit_task_repo")
    task = _make_big_audit_task()
    audit_task_repo.save_task(task)

    write_total = 20

    def writer(i: int) -> None:
        task.progress = i / write_total
        audit_task_repo.save_task(task)

    def reader() -> object:
        return audit_task_repo.get_task(task.id)

    errors = _run_concurrent_save_and_get(
        write_iterations=write_total,
        read_iterations=200,
        writer=writer,
        reader=reader,
    )
    assert errors == [], f"save_task 期间 reader 撞了 {len(errors)} 次 JSONDecodeError：{errors[:3]}"


# ── doc_repo ───────────────────────────────────────────────────────────────────


def test_doc_repo_save_doc_is_atomic_for_concurrent_readers(monkeypatch, tmp_path):
    """doc_repo.save_doc + get_doc 并发，read 端不应撞 JSONDecodeError。

    save_doc 写两个文件：原文件 ``wb``（无关）与 ``meta.json`` ``w``+``json.dump``
    （issue #157 关心的 JSON 写）。
    """
    _slow_dump_factory(monkeypatch, "storage.doc_repo")
    kb_id = "kb_atomic"

    # 先 seed 一个 doc 给 writer 反复 update
    seed = doc_repo.save_doc(kb_id, "seed.pdf", b"seed content", "pdf")

    def writer(_i: int) -> None:
        # 反复触发 _save_doc_meta（与 update_doc 路径一致）
        seed.embedding_status = (
            "embedded" if seed.embedding_status != "embedded" else "indexing"
        )
        doc_repo._save_doc_meta(seed)

    def reader() -> object:
        return doc_repo.get_doc(kb_id, seed.id)

    errors = _run_concurrent_save_and_get(
        write_iterations=20,
        read_iterations=200,
        writer=writer,
        reader=reader,
    )
    assert errors == [], f"save_doc 期间 reader 撞了 {len(errors)} 次 JSONDecodeError：{errors[:3]}"


# ── audit_doc_repo ─────────────────────────────────────────────────────────────


def test_audit_doc_repo_save_is_atomic_for_concurrent_readers(monkeypatch):
    """audit_doc_repo.save_doc + get_doc 并发，read 端不应撞 JSONDecodeError。"""
    _slow_dump_factory(monkeypatch, "storage.audit_doc_repo")
    doc = _make_big_audit_doc()
    # save_doc 假设父目录已存在（生产路径总是先经由更上层 service 创建），
    # 这里同步建一份让测试独立可跑。
    audit_doc_repo._ensure_dir(audit_doc_repo.get_data_dir() / "audits" / doc.id)
    audit_doc_repo.save_doc(doc)

    def writer(_i: int) -> None:
        # 反复 save_doc（update_doc 路径 = save_doc）
        audit_doc_repo.save_doc(doc)

    def reader() -> object:
        return audit_doc_repo.get_doc(doc.id)

    errors = _run_concurrent_save_and_get(
        write_iterations=20,
        read_iterations=200,
        writer=writer,
        reader=reader,
    )
    assert errors == [], f"save_doc 期间 reader 撞了 {len(errors)} 次 JSONDecodeError：{errors[:3]}"
