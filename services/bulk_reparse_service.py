"""批量重新解析 (Bulk Reparse) 服务 —— 领域逻辑归属地（spec #102 / issue #108）。

**批量重新解析**是对一个 KB 内全部**待重解析文档 (Reparse Target)** 触发的受控
批量流程，是单篇**重新解析 (Reparse)**（``services.reparse_service``）之上的编排层。
本模块承接四件事：

1. **待重解析文档选取** —— 三条规则（未向量化 / 缺按页文本 / 按页文本 layout 为空），
   外加 ``force`` 模式绕过（整库重建，如换解析器后）。
2. **OCR 成本预检** —— 按 ``(content_hash, model_version)`` 探测缓存条目，
   区分命中 / 未命中。**无副作用**。历史 ``source=fallback_pdfplumber`` 条目
   （#99/05 之前残留）现在算命中：``get_cached`` 已不再判废，清理是单独工单。
3. **页数上限分类** —— 超 ``PAGE_LIMIT`` 的文档会被解析器服务端截断（issue #87
   决议），预检里作为警告呈现、实际 run 里进 ``skipped``。
4. **受控并发编排** —— 线程池限流 + 单篇轮询超时，单篇失败不中断整批。
5. **KB 级检索状态** —— 整批期间把 KB 按在 ``building``，终态末尾写一次
   （issue #109，见 ``_KbIndexStatus``）。批量下单篇入口以
   ``caller_manages_kb_status=True`` 调用，不再各写各的。

为什么在 service 而不是 CLI（issue #108 的全部动机）：三条选取规则里的第三条
（``layout == []`` 兜底）是 #93 复盘一整轮才加上的，**只要它有第二份实现就必然分叉**。
CLI (``scripts/bulk_reparse.py``) 与后续的 HTTP API 共用本模块，唯一实现、唯一语义。

实测 OCR 计数与报告持久化（#110）、HTTP 端点（#111）不在这里，后续 ticket 在此基础上加。
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

import storage.doc_repo as doc_repo
import storage.kb_repo as kb_repo
from core import paddleocr_cache, pages_store
from core.logger import get_logger
from models.document import KBDocument
from services.reparse_service import reparse_document

_logger = get_logger(__name__)


# ── 常量 ───────────────────────────────────────────────────────────────────────

# 单文件页数上限：超过此值按 PaddleOCR 服务端约定会被截断（issue #87 决议）。
# 预检中仅警告；实际 run 中跳过（不静默丢内容）。
PAGE_LIMIT = 100

# 默认估算：未带 page_count 元数据的 doc，按此页数估算 OCR 成本。
# 虹桥公司制度 KB 实测均值 10.93 / 中位 9（research/ocr-cache-hit-estimate.md）。
DEFAULT_PAGES_ESTIMATE = 11

# 每篇 reparse 轮询超时（秒）。PaddleOCR 单页 ~3-5s + 索引写入 < 5s，
# 1694 页 ÷ N=4 并发 ÷ KB 锁串行化，最坏单 doc 30 分钟内应有结果。
PER_DOC_TIMEOUT_S = 1800

# 默认并发（issue #87 决议 γ）。
DEFAULT_CONCURRENCY = 4

# embedding 终态：成功 / 失败，轮询结束条件。
_TERMINAL_STATUSES = {"embedded", "failed", "none"}

# 轮询间隔（秒）。模块级常量便于测试压缩等待。
_POLL_INTERVAL_S = 2.0

# 选取原因：让调用方（CLI 表格 / 预检 API）能说明"这篇为什么在名单里"。
REASON_NOT_EMBEDDED = "not_embedded"
REASON_MISSING_PAGES = "missing_pages"
REASON_EMPTY_LAYOUT = "empty_layout"
REASON_FORCED = "forced"

# 跳过原因。
SKIP_REASON_PAGE_LIMIT = "page_limit"

# 失败摘要里最多点名几篇（``index_current_doc`` 是要给人看的一行字，不是日志）。
_MAX_SUMMARY_ITEMS = 3


# ── 值对象 ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ReparseTarget:
    """一篇**待重解析文档**：文档本身 + 它为什么进名单。"""

    doc: KBDocument
    has_pages_file: bool
    reason: str

    @property
    def estimated_page_count(self) -> int:
        """成本估算用页数：缺 ``page_count`` 元数据时回落到默认估值。"""
        return self.doc.page_count or DEFAULT_PAGES_ESTIMATE

    @property
    def over_page_limit(self) -> bool:
        return self.estimated_page_count > PAGE_LIMIT


@dataclass(frozen=True)
class SkippedDoc:
    """一篇被跳过的文档 + 跳过原因（跳过不许静默）。"""

    doc: KBDocument
    page_count: int
    reason: str


@dataclass(frozen=True)
class OcrCostEstimate:
    """预检产出：缓存命中 / 未命中的篇数与页数，以及超页数上限的清单。"""

    cached: int = 0
    uncached: int = 0
    pages_cached: int = 0
    pages_uncached: int = 0
    over_page_limit: list[SkippedDoc] = field(default_factory=list)


@dataclass(frozen=True)
class BulkReparseResult:
    """一次批量运行的终态统计。"""

    kb_id: str
    total: int
    done: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[SkippedDoc] = field(default_factory=list)


# ── 1) 待重解析文档选取 ────────────────────────────────────────────────────────


def list_target_docs(kb_id: str, *, force: bool = False) -> list[ReparseTarget]:
    """列出 KB 内的**待重解析文档**。保留 ``doc_repo.list_docs()`` 的原始顺序。

    三条规则，命中任一即入选：

    1. ``embedding_status != "embedded"`` —— 从没解析成功过。
    2. 缺 ``pages/{doc_id}.json`` —— 按页文本丢失。
    3. pages 文件存在但 ``layout == []`` —— #93 揭示的静默 fallback"假成功"。

    第三条的动机：``parse_document._pdf_fallback()`` 在 PaddleOCR 不可用时返回
    ``layout=[]`` 但 ``full_text`` 仍有内容 → ``reparse_service`` 的
    ``len(full_text) < 20`` 兜底校验通过 → ``embedding_status=embedded``。
    **仅看状态机无法识别这种假成功**，必须读 pages 文件的 ``layout`` 字段。

    ``force=True`` 绕过三条规则，整库皆为目标（换解析器后的整库重建入口）。
    """
    targets: list[ReparseTarget] = []
    for doc in doc_repo.list_docs(kb_id):
        has_pages = _pages_file_exists(kb_id, doc.id)
        if force:
            targets.append(ReparseTarget(doc=doc, has_pages_file=has_pages, reason=REASON_FORCED))
            continue
        reason = _selection_reason(doc, has_pages)
        if reason is not None:
            targets.append(ReparseTarget(doc=doc, has_pages_file=has_pages, reason=reason))
    return targets


def _selection_reason(doc: KBDocument, has_pages_file: bool) -> Optional[str]:
    """三条规则的判定；命中返回原因，都不命中返回 ``None``。

    多条同时命中时按"最根本"的先报：没 embedded > 没 pages 文件 > layout 空。
    """
    if doc.embedding_status != "embedded":
        return REASON_NOT_EMBEDDED
    if not has_pages_file:
        return REASON_MISSING_PAGES
    pages = pages_store.load_pages(doc.kb_id, doc.id)
    if pages is not None and not (pages.get("layout") or []):
        return REASON_EMPTY_LAYOUT
    return None


def _pages_file_exists(kb_id: str, doc_id: str) -> bool:
    """``data/kbs/{kb_id}/pages/{doc_id}.json`` 是否存在（只探测，不读内容）。"""
    return pages_store._pages_file(kb_id, doc_id).exists()


# ── 2) OCR 成本预检 ────────────────────────────────────────────────────────────


def estimate_ocr_cost(targets: Sequence[ReparseTarget]) -> OcrCostEstimate:
    """对一组待重解析文档估算 OCR 缓存命中与配额消耗。**无副作用**。

    命中判定整条委托给 ``core.paddleocr_cache.cache_state_by_hash`` —— 包括
    "无条目 / 条目损坏 / model_version 不符 → 未命中"。同一条规则只有一份实现，
    否则就会重演 #91 那次"报 0 页 OCR、实际整库重跑"的误报。

    注：#99/05 删了 V8 cache defense 之后，历史 ``source=fallback_pdfplumber``
    条目不再被判废；预检按"命中"计费。运维清理单独 ticket。
    """
    cached = uncached = pages_cached = pages_uncached = 0
    over_page_limit: list[SkippedDoc] = []

    for target in targets:
        if target.over_page_limit:
            over_page_limit.append(_as_skipped(target))
            # 超限 doc：服务端会截断，按 PAGE_LIMIT 计费更保守。
            billed_pages = PAGE_LIMIT
        else:
            billed_pages = target.estimated_page_count

        if cache_state(target.doc) == paddleocr_cache.CACHE_STATE_HIT:
            cached += 1
            pages_cached += billed_pages
        else:
            uncached += 1
            pages_uncached += billed_pages

    return OcrCostEstimate(
        cached=cached,
        uncached=uncached,
        pages_cached=pages_cached,
        pages_uncached=pages_uncached,
        over_page_limit=over_page_limit,
    )


def cache_state(doc: KBDocument) -> str:
    """该文档的 OCR 缓存状态：``cached`` / ``uncached``。

    见 ``core.paddleocr_cache.cache_state_by_hash``。``content_hash`` 缺失 → 未命中。
    """
    return paddleocr_cache.cache_state_by_hash(doc.content_hash or "")


def is_cache_hit(doc: KBDocument) -> bool:
    """这篇是否**真的**不消耗 OCR 配额。"""
    return cache_state(doc) == paddleocr_cache.CACHE_STATE_HIT


# ── 3) 页数上限分类 ────────────────────────────────────────────────────────────


def split_by_page_limit(
    targets: Sequence[ReparseTarget],
) -> tuple[list[ReparseTarget], list[SkippedDoc]]:
    """把目标清单切成"会跑"与"会跳过（超 ``PAGE_LIMIT``）"两半。

    dry-run 用它渲染"会被跳过"的警告；实际 run 用它决定谁进线程池。同一份规则。
    """
    runnable: list[ReparseTarget] = []
    skipped: list[SkippedDoc] = []
    for target in targets:
        if target.over_page_limit:
            skipped.append(_as_skipped(target))
        else:
            runnable.append(target)
    return runnable, skipped


def _as_skipped(target: ReparseTarget) -> SkippedDoc:
    """超页数上限的目标 → 一条带原因的跳过记录。"""
    return SkippedDoc(
        doc=target.doc,
        page_count=target.estimated_page_count,
        reason=SKIP_REASON_PAGE_LIMIT,
    )


# ── 4) 单篇触发 + 受控并发编排 ─────────────────────────────────────────────────


def reparse_one(
    kb_id: str,
    doc: KBDocument,
    *,
    timeout_s: float = PER_DOC_TIMEOUT_S,
    caller_manages_kb_status: bool = False,
) -> tuple[str, str]:
    """对一篇文档触发重新解析并等待终态。返回 ``(doc_id, outcome)``。

    ``outcome`` ∈ ``{"embedded", "failed", "none", "missing", "timeout", "raised:<err>"}``。

    ``caller_manages_kb_status`` 原样透传给 ``reparse_document``：批量编排下必须为
    ``True``，否则单篇一完成就把 KB 写回 ``searchable``（见 ``_KbIndexStatus``）。
    """
    try:
        reparse_document(doc.id, caller_manages_kb_status=caller_manages_kb_status)
    except Exception as e:
        return (doc.id, f"raised:{type(e).__name__}:{e}")

    return (doc.id, _wait_for_terminal(kb_id, doc.id, timeout_s))


def _wait_for_terminal(kb_id: str, doc_id: str, timeout_s: float) -> str:
    """轮询 ``embedding_status`` 直到终态。返回终态字符串；超时返回 ``"timeout"``。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        doc = doc_repo.get_doc(kb_id, doc_id)
        if doc is None:
            return "missing"
        if doc.embedding_status in _TERMINAL_STATUSES:
            return doc.embedding_status
        time.sleep(_POLL_INTERVAL_S)
    return "timeout"


# ── 5) KB 级检索状态：批次期间的唯一写入者 ─────────────────────────────────────


class _KbIndexStatus:
    """一次批量运行期间 KB 级检索状态的**唯一写入者**（issue #109）。

    契约三句话：

    1. 批次开头写一次 ``building`` + ``index_progress=0``；
    2. 期间只推进 ``index_progress``（``done/total``，单调不减）与
       ``index_current_doc``（在飞文档名），``index_status`` 恒为 ``building``；
    3. 批次末尾写一次终态 —— 全部完成 → ``searchable``；有任何一篇没完成 →
       ``failed`` + 一行人能读懂的失败摘要。

    为什么中途一次 ``searchable`` 都不许有：按 ``CONTEXT.md`` 的定义，
    ``searchable`` 表示"该库**此刻**可被向量检索"，批量重解析进行到一半时
    这句话不成立。而且前端 ``KnowledgeBaseDetail.tsx`` 以
    ``index_status === "building"`` 为轮询续订的唯一条件，抖动会让它反复
    停轮询又重启，进度条彻底不可信（#93 实测 154 篇抖动上百次）。

    因此单篇入口在批量下必须以 ``caller_manages_kb_status=True`` 调用。
    ``index_current_doc`` 在并发 > 1 时是"最近开跑的那一篇"（后写覆盖前写），
    不试图表达"同时在飞的 N 篇"—— 这个字段只有一个槽位。
    """

    def __init__(self, kb_id: str, total: int) -> None:
        self._kb_id = kb_id
        self._total = total
        self._lock = threading.Lock()
        self._progress = 0.0

    def begin(self) -> None:
        self._write(status="building", progress=0.0, current_doc="")

    def note_in_flight(self, doc: KBDocument) -> None:
        self._write(current_doc=doc.original_name)

    def advance(self, completed: int) -> None:
        self._write(progress=completed / self._total if self._total else 1.0)

    def finish(
        self, failed: Sequence[tuple[str, str]], *, interrupted: Optional[str] = None
    ) -> None:
        """写终态，整批只调用一次。``failed`` 是 ``(文档名, outcome)`` 列表。

        ``interrupted`` 是编排层自身出事时的错误串（线程池崩溃 / Ctrl-C）：
        批次没跑完，但 KB 更不能永远卡在 ``building`` —— 前端会一直轮询一个
        永不落地的批次。落 ``failed`` 并说明中断原因。
        """
        if interrupted is not None:
            self._write(
                status="failed", progress=1.0,
                current_doc=f"批量重新解析中断: {interrupted}",
            )
        elif failed:
            self._write(
                status="failed", progress=1.0,
                current_doc=_failure_summary(failed, self._total),
            )
        else:
            self._write(status="searchable", progress=1.0, current_doc="")

    def _write(
        self,
        *,
        status: Optional[str] = None,
        progress: Optional[float] = None,
        current_doc: Optional[str] = None,
    ) -> None:
        """读—改—写一次 KB 元数据；只覆盖显式给出的字段。

        锁住整个读—改—写：``kb_repo.update`` 落的是整个对象，没有锁的话
        两个线程各自读到旧值再写回，后写的会把前一次的进度抹掉。
        """
        with self._lock:
            if progress is not None:
                # 单调不减：并发下完成回调乱序也不许让进度倒退
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


def _failure_summary(failed: Sequence[tuple[str, str]], total: int) -> str:
    """把失败清单压成一行给人看的摘要，写进 ``index_current_doc``。"""
    shown = "；".join(
        f"{name}: {outcome}" for name, outcome in failed[:_MAX_SUMMARY_ITEMS]
    )
    more = " 等" if len(failed) > _MAX_SUMMARY_ITEMS else ""
    return f"批量重新解析失败 {len(failed)}/{total} 篇（{shown}{more}）"


def run_bulk_reparse(
    kb_id: str,
    targets: Sequence[ReparseTarget],
    *,
    concurrency: int = DEFAULT_CONCURRENCY,
    timeout_s: float = PER_DOC_TIMEOUT_S,
    on_doc_complete: Optional[Callable[[int, int, KBDocument, str], None]] = None,
) -> BulkReparseResult:
    """受控并发跑完一批待重解析文档，返回终态统计。

    - 超 ``PAGE_LIMIT`` 的文档不触发，直接进 ``skipped``（带原因）。
    - 单篇失败 / 超时 / 抛异常都只记账，不中断整批。
    - ``on_doc_complete(completed, total, doc, outcome)`` 逐篇回调，供调用方渲染进度。
    - 整批期间 KB 被按在 ``building``，终态在末尾写一次（见 ``_KbIndexStatus``）。
      没有任何可跑的目标时一个字都不写 —— 什么都没发生，不该改写 KB 状态。

    并发用线程池限流（信号量等价物）；KB 级 RLock 会自然序列化索引写入。
    """
    runnable, skipped = split_by_page_limit(targets)
    total = len(runnable)

    done: list[str] = []
    failed: list[tuple[str, str]] = []
    failed_labels: list[tuple[str, str]] = []

    if total:
        status = _KbIndexStatus(kb_id, total)
        status.begin()

        def _run_one(doc: KBDocument) -> tuple[str, str]:
            status.note_in_flight(doc)
            return reparse_one(
                kb_id, doc, timeout_s=timeout_s, caller_manages_kb_status=True
            )

        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            futures = {pool.submit(_run_one, t.doc): t.doc for t in runnable}
            try:
                for completed, (fut, doc) in enumerate(futures.items(), start=1):
                    try:
                        _doc_id, outcome = fut.result()
                    except Exception as e:  # 线程池自身异常：仍然记账，不炸整批
                        outcome = f"raised:{type(e).__name__}:{e}"

                    if outcome == "embedded":
                        done.append(doc.id)
                    else:
                        failed.append((doc.id, outcome))
                        failed_labels.append((doc.original_name, outcome))

                    _logger.info(
                        "bulk_reparse: [%d/%d] %s (%s) → %s",
                        completed, total, doc.id, doc.original_name, outcome,
                    )
                    status.advance(completed)
                    if on_doc_complete is not None:
                        on_doc_complete(completed, total, doc, outcome)
            except BaseException as e:
                # 编排层自身出事（回调抛错 / Ctrl-C）：终态照落，绝不留 building。
                status.finish(failed_labels, interrupted=f"{type(e).__name__}: {e}")
                raise

        status.finish(failed_labels)

    return BulkReparseResult(
        kb_id=kb_id, total=total, done=done, failed=failed, skipped=skipped
    )
