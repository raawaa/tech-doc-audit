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
   （issue #109 / #147，由 ``core.kb_index_status.KbIndexStatusWriter`` 独占）。
   批量下单篇入口注入该 writer 取代旧的 ``caller_manages_kb_status=True`` 开关
   （#150），不再各写各的。
6. **实测 OCR 消耗计数与报告持久化**（issue #110）—— 跑前取缓存条目快照，
   跑完每篇按 ``paddleocr / cache_hit / 非 OCR 来源 / unknown`` 分桶，预检
   估算与实测值并列写进 ``data/kbs/{kb_id}/bulk_reparse_report.json``。

为什么在 service 而不是 CLI（issue #108 的全部动机）：三条选取规则里的第三条
（``layout == []`` 兜底）是 #93 复盘一整轮才加上的，**只要它有第二份实现就必然分叉**。
CLI (``scripts/bulk_reparse.py``) 与后续的 HTTP API 共用本模块，唯一实现、唯一语义。

HTTP 端点（#111）不在这里，后续 ticket 在此基础上加。
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Callable, Mapping, Optional, Sequence

import storage.doc_repo as doc_repo
import storage.kb_repo as kb_repo
from core import bulk_reparse_report_store, paddleocr_cache, pages_store
from core.kb_index_status import KbIndexStatusWriter
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

# 实测 OCR 消耗的两个**非解析器**分桶名（其余分桶名直接就是缓存条目的 ``source``：
# ``paddleocr`` / ``pymupdf`` / ``fallback_*``）。
#
# ``cache_hit`` —— 跑之前该 doc 就已有缓存条目，这一篇没烧配额。缓存条目的
# ``source`` 仍写着 ``paddleocr``，光看 source 会把缓存命中误报成真实 OCR，
# 所以区分二者的唯一依据是"跑之前有没有条目"（快照必须在 run 开始前取）。
SOURCE_CACHE_HIT = "cache_hit"
# ``unknown`` —— 跑完仍回读不到缓存条目（doc 无 ``content_hash``、条目损坏、
# 或解析根本没写缓存）。显式报"不知道"，**绝不**默认记成 OCR：
# 凭空补一个好看的页数正是 #90 报 1694 页而实际 0 页的错法。
SOURCE_UNKNOWN = "unknown"

# 报告 schema 版本。字段增删时递增，让 #111 的报告端点与旧文件能互相识别。
REPORT_SCHEMA_VERSION = 1


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
class DocParseUsage:
    """一篇跑过的文档**实际**是谁解析的、算几页。

    ``source`` 是分桶名：缓存条目的 ``source``（``paddleocr`` / ``pymupdf`` /
    ``fallback_*``）、``SOURCE_CACHE_HIT`` 或 ``SOURCE_UNKNOWN``。
    ``pages`` 取落盘的 ``pages/{doc_id}.json`` 的真实页数（读不到才回落到预检估值）。
    """

    doc_id: str
    original_name: str
    source: str
    pages: int
    succeeded: bool


@dataclass(frozen=True)
class ActualOcrUsage:
    """一次批量运行的**实测** OCR 消耗，按解析来源分桶。

    成功篇按 ``source`` 落入对应桶；失败篇入显式 ``"failed"`` 桶（页数归零）——
    不入这一桶就会让"预估 X、实际 X 篇 done 但 0 页 OCR"这种"claim 满但 burn
    零"的指纹（正是 #90 那类）凭空消失。失败原因仍单独记在报告的 failed 明细里。
    """

    pages_by_source: dict[str, int] = field(default_factory=dict)
    docs_by_source: dict[str, int] = field(default_factory=dict)

    @property
    def actual_ocr_pages(self) -> int:
        """真正烧掉 OCR 配额的页数 —— 只有 ``paddleocr`` 那一桶算数。"""
        return self.pages_by_source.get(paddleocr_cache.SOURCE_PADDLEOCR, 0)


@dataclass(frozen=True)
class BulkReparseResult:
    """一次批量运行的终态统计。

    ``done`` / ``failed`` / ``skipped`` 是 CLI 退出码与终端渲染的既有契约（#108）；
    ``estimate`` / ``usage`` / 时间戳 / ``report_path`` 是 #110 补上的实测侧。
    """

    kb_id: str
    total: int
    done: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[SkippedDoc] = field(default_factory=list)
    estimate: OcrCostEstimate = field(default_factory=lambda: OcrCostEstimate())
    usage: ActualOcrUsage = field(default_factory=lambda: ActualOcrUsage())
    doc_usages: list[DocParseUsage] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0
    report_path: Optional[str] = None


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
    kb_writer: Optional[KbIndexStatusWriter] = None,
) -> tuple[str, str]:
    """对一篇文档触发重新解析并等待终态。返回 ``(doc_id, outcome)``。

    ``outcome`` ∈ ``{"embedded", "failed", "none", "missing", "timeout", "raised:<err>"}``；
    失败时 ``failed`` 会被精化为 ``"failed: <reason>"``（从 ``kb.index_current_doc``
    读 —— ``reparse_service._mark_failed`` 写在那里），否则批量报告的 failed
    明细只剩光秃秃的 "failed" 串，根本看不出是 parse 挂了、layout 空了、还是
    索引写失败。同步 ``reparse_document()`` 抛异常仍走 ``"raised:<err>"`` 路径。

    ``kb_writer`` 直接透传给 ``reparse_document``：批量编排下注入它跨文档共享的
    ``KbIndexStatusWriter``，让 KB 级状态字段由编排层独占管理（详见 ``KbIndexStatusWriter``
    docstring + #147）；默认 ``None`` —— 单篇入口走函数自构造 ``total=1`` 的 writer。
    """
    try:
        reparse_document(doc.id, kb_writer=kb_writer)
    except Exception as e:
        return (doc.id, f"raised:{type(e).__name__}:{e}")

    return (doc.id, _wait_for_terminal(kb_id, doc.id, timeout_s))


def _wait_for_terminal(kb_id: str, doc_id: str, timeout_s: float) -> str:
    """轮询 ``embedding_status`` 直到终态。返回终态字符串；超时返回 ``"timeout"``。

    终态 ``failed`` 时从 ``kb.index_current_doc`` 抓出 ``_mark_failed`` 写下的
    错误消息（契约见 ``services/reparse_service`` 顶部 docstring），让批量报告的
    failed 明细能直接复盘是哪一步炸的，而不是只看到 "failed" 一个词。
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        doc = doc_repo.get_doc(kb_id, doc_id)
        if doc is None:
            return "missing"
        if doc.embedding_status in _TERMINAL_STATUSES:
            if doc.embedding_status != "failed":
                return doc.embedding_status
            # 失败：从 KB 侧抓真正的错误消息
            kb = kb_repo.get(kb_id)
            err = (kb.index_current_doc if kb is not None else "") or ""
            if err.startswith("reparse 错误: "):
                err = err[len("reparse 错误: "):]
            return f"failed: {err}" if err else "failed"
        time.sleep(_POLL_INTERVAL_S)
    return "timeout"


# ── 5) KB 级检索状态：批次期间的唯一写入者 ─────────────────────────────────────
#
# Issue #147 / #148：KB 级检索状态字段（``index_status`` / ``index_progress`` /
# ``index_current_doc``）的唯一写入者是 ``core.kb_index_status.KbIndexStatusWriter``。
# 本模块原本的 ``_KbIndexStatus`` 是它的二次克隆（issue #109 引入），#150 后
# 直接用 ``KbIndexStatusWriter`` 即可：批次开头 ``begin()`` + 期间 ``advance()``
# + 末尾 ``finish()`` 三句契约不变；单篇入口注入本 writer 取代旧的
# ``caller_manages_kb_status=True`` 开关。
#
# 为什么中途一次 ``searchable`` 都不许有：按 ``CONTEXT.md`` 的定义，
# ``searchable`` 表示"该库**此刻**可被向量检索"，批量重解析进行到一半时
# 这句话不成立。而且前端 ``KnowledgeBaseDetail.tsx`` 以
# ``index_status === "building"`` 为轮询续订的唯一条件，抖动会让它反复
# 停轮询又重启，进度条彻底不可信（#93 实测 154 篇抖动上百次）。
# ``index_current_doc`` 在并发 > 1 时是"最近开跑的那一篇"（后写覆盖前写），
# 不试图表达"同时在飞的 N 篇"—— 这个字段只有一个槽位。


def run_bulk_reparse(
    kb_id: str,
    targets: Sequence[ReparseTarget],
    *,
    concurrency: int = DEFAULT_CONCURRENCY,
    timeout_s: float = PER_DOC_TIMEOUT_S,
    forced: bool = False,
    on_doc_complete: Optional[Callable[[int, int, KBDocument, str], None]] = None,
) -> BulkReparseResult:
    """受控并发跑完一批待重解析文档，返回终态统计并落盘一份**批量重新解析报告**。

    - 超 ``PAGE_LIMIT`` 的文档不触发，直接进 ``skipped``（带原因）。
    - 单篇失败 / 超时 / 抛异常都只记账，不中断整批。
    - 每篇跑完立刻回读它的解析来源，进 run log 也进实测分桶（#110）。
    - 整批期间 KB 被按在 ``building``，终态在末尾写一次（由
      ``KbIndexStatusWriter`` 独占，issue #147 / #148 / #154）。
      没有任何可跑的目标时一个字都不写 —— 什么都没发生，不该改写 KB 状态。
    - ``forced`` 只是报告里的一个字段（这批是不是 ``--force`` 跑的），不影响执行。

    并发用线程池限流（信号量等价物）；KB 级 RLock 会自然序列化索引写入。

    **缓存快照必须在触发任何解析之前取**：区分"缓存命中"与"真实 OCR"的唯一依据
    就是跑之前有没有条目 —— 跑完再问就全是命中了。
    """
    runnable, skipped = split_by_page_limit(targets)
    total = len(runnable)

    cached_before = {t.doc.id: cache_state(t.doc) for t in targets}
    estimate = estimate_ocr_cost(targets)

    started_at = _utc_now()
    started_mono = time.monotonic()

    done: list[str] = []
    failed: list[tuple[str, str]] = []
    failed_labels: list[tuple[str, str]] = []
    doc_usages: list[DocParseUsage] = []

    if total:
        status = KbIndexStatusWriter(kb_id, total=total)
        status.begin()

        def _run_one(doc: KBDocument) -> tuple[str, str]:
            status.note_in_flight(doc.original_name)
            return reparse_one(
                kb_id, doc, timeout_s=timeout_s, kb_writer=status
            )

        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            futures = {pool.submit(_run_one, t.doc): t for t in runnable}
            try:
                for completed, (fut, target) in enumerate(futures.items(), start=1):
                    doc = target.doc
                    try:
                        _doc_id, outcome = fut.result()
                    except Exception as e:  # 线程池自身异常：仍然记账，不炸整批
                        outcome = f"raised:{type(e).__name__}:{e}"

                    succeeded = outcome == "embedded"
                    if succeeded:
                        done.append(doc.id)
                    else:
                        failed.append((doc.id, outcome))
                        failed_labels.append((doc.original_name, outcome))

                    usage = _measure_doc(
                        kb_id, target,
                        state_before=cached_before.get(doc.id, paddleocr_cache.CACHE_STATE_MISS),
                        succeeded=succeeded,
                    )
                    doc_usages.append(usage)

                    _logger.info(
                        "bulk_reparse: [%d/%d] %s (%s) → %s [source=%s, pages=%d]",
                        completed, total, doc.id, doc.original_name, outcome,
                        usage.source, usage.pages,
                    )
                    status.advance(completed)
                    if on_doc_complete is not None:
                        on_doc_complete(completed, total, doc, outcome)
            except BaseException as e:
                # 编排层自身出事（回调抛错 / Ctrl-C）：终态照落，绝不留 building。
                status.finish(failed_labels, interrupted=f"{type(e).__name__}: {e}")
                raise

        status.finish(failed_labels)

    result = BulkReparseResult(
        kb_id=kb_id,
        total=total,
        done=done,
        failed=failed,
        skipped=skipped,
        estimate=estimate,
        usage=_aggregate_usage(doc_usages),
        doc_usages=doc_usages,
        started_at=started_at,
        finished_at=_utc_now(),
        duration_seconds=round(time.monotonic() - started_mono, 3),
    )

    return _persist_report(
        result, targets,
        concurrency=concurrency, forced=forced,
        cache_state_at_start=cached_before,
    )


# ── 6) 实测 OCR 消耗计数（issue #110）─────────────────────────────────────────


def _measure_doc(
    kb_id: str, target: ReparseTarget, *, state_before: str, succeeded: bool
) -> DocParseUsage:
    """回读一篇跑过的文档**实际**由谁解析、算几页。

    地面真相是**缓存写入侧的 ``source`` 字段** —— #93 事后正是靠数
    ``source=paddleocr`` 的条目才证明"154 篇真的跑了 OCR"。这里把那次考古变成
    流程产物：跑完一篇就问一次，静默降级（本该 OCR 却走了别的路）当场可见。

    ``state_before`` 是 run **触发前**的快照（CONTEXT.md："缓存快照必须在触发
    任何解析之前取"）—— 跑前已命中就一定进 ``cache_hit`` 桶，无论条目 ``source``
    怎么写；跑前未命中才回读 ``source`` 字段区分真 OCR / 其它解析器 / 不可考。
    """
    doc = target.doc
    if state_before == paddleocr_cache.CACHE_STATE_HIT:
        # 跑之前就有条目 → 这一篇没烧配额，无论条目的 source 写着什么。
        source = SOURCE_CACHE_HIT
    else:
        source = paddleocr_cache.cache_source_by_hash(doc.content_hash or "") or SOURCE_UNKNOWN

    if source == SOURCE_UNKNOWN and succeeded:
        _logger.warning(
            "bulk_reparse: no cache entry for %s (%s) after a successful reparse — "
            "解析来源不可考，实测计入 unknown 而非 OCR",
            doc.id, doc.original_name,
        )

    return DocParseUsage(
        doc_id=doc.id,
        original_name=doc.original_name,
        source=source,
        pages=_parsed_page_count(kb_id, doc.id, target.estimated_page_count),
        succeeded=succeeded,
    )


def _parsed_page_count(kb_id: str, doc_id: str, fallback: int) -> int:
    """落盘的 ``pages/{doc_id}.json`` 的**真实**页数；读不到才回落到预检估值。

    实测的意义就在于不复用估值：``doc.page_count`` 可能过时或缺失
    （``DEFAULT_PAGES_ESTIMATE`` 是个中位数猜测），而 ``by_page`` 是解析器
    刚刚吐出来的物理页列表。
    """
    pages = pages_store.load_pages(kb_id, doc_id)
    by_page = (pages or {}).get("by_page") or []
    return len(by_page) or fallback


def _aggregate_usage(doc_usages: Sequence[DocParseUsage]) -> ActualOcrUsage:
    """把逐篇来源汇总成分桶计数。

    成功篇按 ``source`` 落入对应桶；失败篇入显式 ``"failed"`` 桶（页数归零）——
    不入这一桶就会让"预估 X、实际 X 篇 done 但 0 页 OCR"这种"claim 满但 burn
    零"的指纹（正是 #90 那类）凭空消失。失败原因仍单独记在报告的 failed 明细里。
    """
    pages_by_source: dict[str, int] = {}
    docs_by_source: dict[str, int] = {}
    for usage in doc_usages:
        if usage.succeeded:
            pages_by_source[usage.source] = pages_by_source.get(usage.source, 0) + usage.pages
            docs_by_source[usage.source] = docs_by_source.get(usage.source, 0) + 1
        else:
            docs_by_source["failed"] = docs_by_source.get("failed", 0) + 1
    return ActualOcrUsage(pages_by_source=pages_by_source, docs_by_source=docs_by_source)


# ── 7) 批量重新解析报告（issue #110）──────────────────────────────────────────


def build_report(
    result: BulkReparseResult,
    targets: Sequence[ReparseTarget],
    *,
    concurrency: int,
    forced: bool,
    cache_state_at_start: Optional[Mapping[str, str]] = None,
) -> dict:
    """把一次运行组装成**批量重新解析报告** dict（纯函数，不落盘）。

    形状上刻意让 ``estimated_ocr_pages`` 与 ``actual_ocr_pages`` **顶层并列** ——
    #91 那次"预检说 1694 页、实际 0 页"的指纹，就该在报告开头一眼看见。
    两者背离**不做自动拦截**（spec #102 story 26：差异要被看见，不要被自动处置）。

    done / failed / skipped 三类明细各自带 doc id、原始文件名与原因串：
    - done 的 ``reason`` 是**入选**原因（这篇当初为什么在名单里）
    - failed 的 ``reason`` 是**失败**原因（终态串 / 异常类型与消息）
    - skipped 的 ``reason`` 是**跳过**原因（当前只有超页数上限一种）

    ``cache_state_at_start`` 必须是 run **触发前**的快照（CONTEXT.md 写明的
    "缓存快照必须在触发任何解析之前取"）—— 跑完再回读会让"先 uncached 后 cached"
    的 doc 在预检块与实测块互相打脸，正是 #91 那类误报的反向翻版。
    调用方没传则按"重新查"（与 ``cache_state`` 同口径），仅作退化路径。
    """
    by_id = {t.doc.id: t for t in targets}
    usage_by_id = {u.doc_id: u for u in result.doc_usages}
    failure_reasons = dict(result.failed)
    state_at_start = dict(cache_state_at_start or {})

    def _cache_state_of(doc_id: str) -> str:
        if doc_id in state_at_start:
            return state_at_start[doc_id]
        target = by_id.get(doc_id)
        return cache_state(target.doc) if target is not None else paddleocr_cache.CACHE_STATE_MISS

    def _target_view(doc_id: str) -> tuple[str, str, str, int]:
        """``(original_name, selection_reason, source, pages)`` —— 失败篇 source/pages 退化。"""
        target = by_id.get(doc_id)
        usage = usage_by_id.get(doc_id)
        return (
            target.doc.original_name if target else "",
            target.reason if target else "",
            usage.source if usage else SOURCE_UNKNOWN,
            usage.pages if usage else 0,
        )

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "kb_id": result.kb_id,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "duration_seconds": result.duration_seconds,
        "forced": forced,
        "concurrency": concurrency,
        "target_count": len(targets),
        # ── 预检估算 vs 实测：并列呈现，差异是信号不是拦截 ──
        "estimated_ocr_pages": result.estimate.pages_uncached,
        "actual_ocr_pages": result.usage.actual_ocr_pages,
        "actual_pages_by_source": dict(result.usage.pages_by_source),
        "actual_docs_by_source": dict(result.usage.docs_by_source),
        "preflight": {
            "cached_docs": result.estimate.cached,
            "uncached_docs": result.estimate.uncached,
            "estimated_cached_pages": result.estimate.pages_cached,
            "targets": [
                {
                    "doc_id": t.doc.id,
                    "original_name": t.doc.original_name,
                    "page_count": t.estimated_page_count,
                    "reason": t.reason,
                    "cache_state": _cache_state_of(t.doc.id),
                }
                for t in targets
            ],
        },
        "counts": {
            "done": len(result.done),
            "failed": len(result.failed),
            "skipped": len(result.skipped),
        },
        "done": [
            {
                "doc_id": doc_id,
                "original_name": view[0],
                "reason": view[1],
                "source": view[2],
                "pages": view[3],
            }
            for doc_id in result.done
            for view in [_target_view(doc_id)]
        ],
        "failed": [
            {
                "doc_id": doc_id,
                "original_name": view[0],
                "reason": failure_reasons.get(doc_id, ""),
                "source": view[2],
            }
            for doc_id, _reason in result.failed
            for view in [_target_view(doc_id)]
        ],
        "skipped": [
            {
                "doc_id": s.doc.id,
                "original_name": s.doc.original_name,
                "reason": s.reason,
                "page_count": s.page_count,
            }
            for s in result.skipped
        ],
    }


def _persist_report(
    result: BulkReparseResult,
    targets: Sequence[ReparseTarget],
    *,
    concurrency: int,
    forced: bool,
    cache_state_at_start: Mapping[str, str],
) -> BulkReparseResult:
    """落盘报告并把路径挂回 result；落盘失败只 warning，不让整批白跑。"""
    report = build_report(
        result, targets,
        concurrency=concurrency, forced=forced,
        cache_state_at_start=cache_state_at_start,
    )
    try:
        path = bulk_reparse_report_store.save_report(result.kb_id, report)
    except OSError as e:
        _logger.warning("bulk_reparse: failed to persist report for %s: %s", result.kb_id, e)
        return result

    _logger.info(
        "bulk_reparse: report written to %s (预估 %d 页 OCR / 实测 %d 页，来源 %s)",
        path, report["estimated_ocr_pages"], report["actual_ocr_pages"],
        report["actual_pages_by_source"] or "{}",
    )
    return replace(result, report_path=str(path))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
