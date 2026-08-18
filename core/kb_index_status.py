"""KB 检索状态字段（``index_status`` / ``index_progress`` /
``index_current_doc``）的**唯一写入者**（issue #147 / #148）。

背景：原本 KB 状态字段在 5+1 处不同代码路径下各自做 read-modify-write，
ADR-0002（"KB 检索状态字段 = 唯一真相"）形同虚设。本模块把那条契约的
实现收归一个 callback 化的状态机，旧的重复代码是机械的迁移目标。

被 ``core/``、``services/``、``api/`` 三层共用；只依赖 ``storage.kb_repo``，
不引入循环。
"""
from __future__ import annotations

import threading
from typing import Optional, Sequence

import storage.kb_repo as kb_repo


# 失败摘要里最多展开的单个文档条数（与旧的 _KbIndexStatus 保持一致）
_MAX_SUMMARY_ITEMS = 3


class KbIndexStatusWriter:
    """KB 检索状态字段的**唯一写入者**。

    接口是 callback 化的状态机：``begin`` / ``note_in_flight`` / ``advance`` /
    ``finish`` / ``clear_building`` 把外部事件翻译成对 ``kb_repo.update`` 的
    单次写入。同一 KB 同一时刻的所有调用经 ``_lock`` 串行化；不同 KB 互不
    阻塞（不同实例不同锁）。

    关键不变式：

    1. 整批期间 ``index_status`` 一律 ``building``，终态仅在 ``finish`` 时
       写一次 —— 前端 ``KnowledgeBaseDetail.tsx`` 以 ``building`` 为轮询续订
       唯一条件，途中闪 ``searchable`` 会触发 #93 的进度抖动。
    2. ``advance(n)`` 落盘的 ``index_progress`` 单调非递减 —— 并发乱序完成
       回调不会让进度倒退。
    3. ``index_current_doc`` 是多义的：批次里 = 当前在飞文档名；批次外
       （``finish`` / ``clear_building``）= 失败摘要 / 中断原因 / 空串。
       字符串格式收敛到本类的两个 helper，前端不再认两套前缀。
    """

    def __init__(self, kb_id: str, total: int = 1) -> None:
        self._kb_id = kb_id
        self._total = total
        self._lock = threading.Lock()
        self._progress = 0.0

    # ── 公开 callback ──────────────────────────────────────────────

    def begin(self) -> None:
        """批次开头写一次 ``building`` + 进度归零。

        提前显式重置 ``_progress``：批终态 ``finish`` 会把单调守卫推到
        1.0，不重置就把"下一批的 0"截成 1.0，违反"进度归零"的契约。
        """
        with self._lock:
            self._progress = 0.0
        self._write(status="building", progress=0.0, current_doc="")

    def note_in_flight(self, doc_name: str) -> None:
        """记录当前在飞文档名（writer 唯一接受的字段语义是字符串）。"""
        self._write(current_doc=doc_name)

    def advance(self, done: int) -> None:
        """推进进度；``done/total`` 中 ``done`` 通常是已完成计数。"""
        progress = done / self._total if self._total else 1.0
        self._write(progress=progress)

    def finish(
        self,
        failed: Optional[Sequence[tuple[str, str]]] = None,
        *,
        interrupted: Optional[str] = None,
    ) -> None:
        """写终态，整批只调用一次。

        - ``interrupted`` 非空 → ``failed`` + 中断说明（编排层自身出问题时用）。
        - ``failed`` 非空 → ``failed`` + 一行失败摘要。
        - 两者都为空 → ``searchable``。
        """
        if interrupted is not None:
            self._write(
                status="failed",
                progress=1.0,
                current_doc=self._format_interruption(interrupted),
            )
        elif failed:
            self._write(
                status="failed",
                progress=1.0,
                current_doc=self._format_failure_summary(list(failed), self._total),
            )
        else:
            self._write(status="searchable", progress=1.0, current_doc="")

    def clear_building(self) -> None:
        """把 KB 状态拉回 ``none``（崩溃自愈 / 运维解卡）。"""
        self._write(status="none", progress=None, current_doc="")

    # ── 内部 ────────────────────────────────────────────────────────

    def _write(
        self,
        *,
        status: Optional[str] = None,
        progress: Optional[float] = None,
        current_doc: Optional[str] = None,
    ) -> None:
        """读—改—写一次 KB 元数据；只覆盖显式给出的字段。

        锁住整个 read-modify-write：``kb_repo.update`` 落的是整个对象，
        没锁的话两个线程各自读到旧值再写回，后写的会把前一次的进度抹掉。
        KB 已删（``kb_repo.get`` 返 ``None``）→ 静默 ``return``。
        """
        with self._lock:
            if progress is not None:
                # 单调不减：并发下完成回调乱序也不许让进度倒退（#93）
                progress = max(self._progress, progress)
                self._progress = progress
            kb = kb_repo.get(self._kb_id)
            if kb is None:
                return
            if status is not None:
                kb.index_status = status
            if progress is not None:
                kb.index_progress = progress
            if current_doc is not None:
                kb.index_current_doc = current_doc
            kb_repo.update(kb)

    # ── 字符串 helper（人在读的一行）─────────────────────────────────

    @staticmethod
    def _format_failure_summary(
        failed: list[tuple[str, str]], total: int
    ) -> str:
        """把失败清单压成一行人读摘要，写进 ``index_current_doc``。

        格式：``批量重新解析失败 N/M 篇（doc1: reason1；doc2: reason2[ 等]）``。
        最多展开前 N 条；超出的用 `` 等`` 兜底。
        """
        shown = "；".join(
            f"{name}: {outcome}" for name, outcome in failed[:_MAX_SUMMARY_ITEMS]
        )
        more = " 等" if len(failed) > _MAX_SUMMARY_ITEMS else ""
        return f"批量重新解析失败 {len(failed)}/{total} 篇（{shown}{more}）"

    @staticmethod
    def _format_interruption(reason: str) -> str:
        """把中断原因写成一行人读摘要。"""
        return f"批量重新解析中断: {reason}"
