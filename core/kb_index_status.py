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
        # ``None`` = 未开始过；首次 ``begin()`` 把它设为 0.0，之后保持单调
        # 不减。批间切换由 caller 构造新 writer 或显式 ``clear_building``
        # 触发 —— 见 ``begin()`` docstring。
        self._progress: Optional[float] = None

    # ── 公开 callback ──────────────────────────────────────────────

    def begin(self) -> None:
        """批次开头写一次 ``building`` + 进度归零。

        仅在"未开始过（``_progress is None``）"或"KB 当前 ``status == 'none'``"
        时把 ``_progress`` 重置为 0.0；其它情况（mid-batch 重复调用，已有中间值）
        **不**重置，让 ``_write`` 的单调不减守卫自然兜住（issue #155 defense）。
        批间切换走两种路径之一：caller 构造新 writer（本模块与上游服务的
        现行约定）—— 让 ``_progress`` 回到 ``None``，下次 ``begin()`` 自然 reset；
        或先 ``clear_building()`` 把 KB 拍回 ``none``，下次 ``begin()`` 也能 reset。

        提前显式重置 ``_progress``：批终态 ``finish`` 会把单调守卫推到
        1.0，不重置就把"下一批的 0"截成 1.0，违反"进度归零"的契约。
        """
        with self._lock:
            kb = kb_repo.get(self._kb_id)
            should_reset = (
                self._progress is None
                or (kb is not None and kb.index_status == "none")
            )
            if should_reset:
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

    def fail_doc(self, name: str, err: str) -> None:
        """单篇失败的统一入口。

        ``total == 1``（自己造的 writer）→ ``finish(failed=[(name, err)])``
        把 KB 写成 ``failed`` + 一行摘要；``total > 1``（编排层注入的 writer）
        → 只把错误摘要写到 ``index_current_doc``，保留 ``status=building`` 不变，
        由编排层在批次末尾统一 ``finish()``。

        之所以把"该不该写终态"的判断收归 writer：调用方
        （``reparse_service._mark_failed``）只知道"我失败了"，不知道"我是单篇
        还是批量里的一篇"。把 ``_total`` 这种私有细节挪到 writer 自己后，
        调用方就只剩一行 ``kb_writer.fail_doc(name, err)`` —— issue #150 的
        "无脑调 writer API" 在这里真正落地。

        #149 已知字面小漂移：``total == 1`` 路径仍复用 ``_format_failure_summary``，
        因此单篇失败时 ``index_current_doc`` 会写成 ``"批量重新解析失败 1/1 篇
        （name: err）"`` —— "批量"字面与单篇语境不符，#147 §User Stories 第14条
        列入后续 ticket；下次若改 ``_format_failure_summary``，单篇路径会一起变。
        """
        if self._total == 1:
            self.finish(failed=[(name, err)])
        else:
            self.note_in_flight(f"reparse 错误: {err}")

    def clear_building(self) -> None:
        """把 KB 状态拉回 ``none``（崩溃自愈 / 运维解卡）。

        三个字段一并清除 —— 包括 ``index_progress=None``（即使原值非零也得
        清，否则下次重建时前端还会看到上次的 0.42）。``progress=None`` 走
        ``_write`` 的"显式置 None"分支（vs. 哨兵 ``_UNSET`` 的"跳过"分支），
        见 ``_write`` docstring。
        """
        self._write(status="none", progress=None, current_doc="")

    # ── 内部 ────────────────────────────────────────────────────────

    # 区分"未给出"与"明确置 None"：用哨兵对象，否则 ``clear_building`` 拿 ``_write(
    # progress=None)`` 表达"清空进度"会被当作"跳过"，index_progress 残留上次的非零
    # 值（#148 spec 7 / #153 把这个语义首次暴露给非默认值的 KB）。
    _UNSET: object = object()

    def _write(
        self,
        *,
        status: object = _UNSET,
        progress: object = _UNSET,
        current_doc: object = _UNSET,
    ) -> None:
        """读—改—写一次 KB 元数据；只覆盖显式给出的字段。

        锁住整个 read-modify-write：``kb_repo.update`` 落的是整个对象，
        没锁的话两个线程各自读到旧值再写回，后写的会把前一次的进度抹掉。
        KB 已删（``kb_repo.get`` 返 ``None``）→ 静默 ``return``。

        字段参数语义：
        - 哨兵 ``_UNSET``（默认）→ 该字段不动。
        - ``None`` → 把该字段显式清空（如 ``clear_building`` 的 ``progress``）。
        - 其它值 → 正常写入。
        """
        with self._lock:
            if progress is not self._UNSET and progress is not None:
                # 单调不减：并发下完成回调乱序也不许让进度倒退（#93）。
                # ``_progress`` 初始为 ``None``（writer 刚构造、还没 ``begin()``），
                # 此时直接收下入参，不与 ``None`` 比 max。
                if self._progress is not None:
                    progress = max(self._progress, progress)
                self._progress = progress
            kb = kb_repo.get(self._kb_id)
            if kb is None:
                return
            # 三字段同形态（"如果不是 _UNSET 就赋值"），收归一次循环；
            # 字段名是 KnowledgeBase 的 Literal 字段，setattr 走 type: ignore。
            for field, value in (
                ("index_status", status),
                ("index_progress", progress),
                ("index_current_doc", current_doc),
            ):
                if value is not self._UNSET:
                    setattr(kb, field, value)  # type: ignore[arg-type]
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
