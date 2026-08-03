"""对整个 KB 触发批量 reparse（Wayfinder #86 / #89）。

直接复用 ``services.reparse_service.reparse_document``，不依赖 HTTP 服务在线。
设计上：
- 启动前打印目标 doc 数 + 预估 OCR 成本
- 二次确认 prompt（除非 ``--yes``）
- 每篇完成后打 ``[done/failed] doc_id (original_name)``
- 退出时输出最终统计

并发：信号量限制 N 个并发 reparse（默认 4）。KB 级 RLock 会自然序列化索引写入。

用法：
  # 仅枚举目标 doc，不触发 reparse
  uv run python scripts/bulk_reparse.py --kb-id <kb_id> --dry-run

  # 实际跑（需要二次确认）
  uv run python scripts/bulk_reparse.py --kb-id <kb_id>

  # 跳过确认（CI / 已知环境用）
  uv run python scripts/bulk_reparse.py --kb-id <kb_id> --yes

  # 自定义并发
  uv run python scripts/bulk_reparse.py --kb-id <kb_id> --concurrency 8
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# 确保能找到项目模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 加载 .env（与 api/main.py:5-7 同源）— PaddleOCR 凭证必须在
# core.parse_document._paddleocr_available() 调用前就位，否则子进程会因
# PaddleOCR 与 PyMuPDF 都不可用而抛 RuntimeError（issue #99/05 后无
# _pdf_fallback() 兜底）。wayfinder #93 提到的 layout=[] 假成功已不再可能。
# DEPRECATED: #99/05 起的修复 — 历史 cache 条目仍可能含 fallback_pdfplumber。
from dotenv import load_dotenv

_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    load_dotenv(_env_path)

os.environ.setdefault("AUDIT_DATA_DIR", "data")

import storage.doc_repo as doc_repo
import storage.kb_repo as kb_repo
from core.logger import get_logger
from core.paddleocr_cache import CACHE_DIR, _paddleocr_currently_available
from services.reparse_service import reparse_document

_logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 目标 doc 枚举
# ---------------------------------------------------------------------------

# 单文件页数上限：超过此值按 PaddleOCR 服务端约定会被截断（issue #87 决议）。
# 在实际 run 中触发警告并跳过；dry-run 中只警告不跳过。
PAGE_LIMIT = 100

# 默认估算：未带 page_count 元数据的 doc，按此页数估算 OCR 成本。
# 虹桥公司制度 KB 实测均值 10.93 / 中位 9（research/ocr-cache-hit-estimate.md）。
DEFAULT_PAGES_ESTIMATE = 11

# 每篇 reparse 轮询超时（秒）。PaddleOCR 单页 ~3-5s + 索引写入 < 5s，
# 1694 页 ÷ N=4 并发 ÷ KB 锁串行化，最坏单 doc 30 分钟内应有结果。
PER_DOC_TIMEOUT_S = 1800

# embedding 终态：成功 / 失败，轮询结束条件。
_TERMINAL_STATUSES = {"embedded", "failed", "none"}


def _pages_dir(kb_id: str) -> Path:
    """``data/kbs/{kb_id}/pages/``。与 core.pages_store 同源（仅 dry-run 探测用）。"""
    return Path(os.environ.get("AUDIT_DATA_DIR", "data")) / "kbs" / kb_id / "pages"


def _pages_file(kb_id: str, doc_id: str) -> Path:
    return _pages_dir(kb_id) / f"{doc_id}.json"


def list_target_docs(kb_id: str) -> list:
    """列出 KB 内需要 reparse 的 doc：embedding_status != embedded 或缺 pages 文件
    **或 pages 文件存在但 ``layout=[]``（wayfinder #93 揭示的历史污染兜底）**。

    兜底 layout 检查的动机：存量 KB 中可能残留 issue #99/05 之前的
    ``source=fallback_pdfplumber`` 污染条目，``embedding_status`` 显示 embedded
    但 ``layout=[]`` 不可用。仅看状态机无法识别这种"假成功"，必须读 pages 文件
    ``layout`` 字段。

    返回 ``[(doc, has_pages_file), ...]``；保留 doc_repo.list_docs() 的原始顺序。
    """
    from core.pages_store import load_pages as _load_pages
    docs = doc_repo.list_docs(kb_id)
    targets = []
    for doc in docs:
        has_pages = _pages_file(kb_id, doc.id).exists()
        # 三选一即视为需要 reparse：
        #   1. embedding_status != embedded
        #   2. 缺 pages 文件
        #   3. pages 文件存在但 layout 空（#93 假成功兜底）
        layout_empty = False
        if has_pages:
            pages = _load_pages(doc.kb_id, doc.id)
            if pages is not None and not (pages.get("layout") or []):
                layout_empty = True
        if doc.embedding_status != "embedded" or not has_pages or layout_empty:
            targets.append((doc, has_pages))
    return targets


# ---------------------------------------------------------------------------
# OCR 成本预估
# ---------------------------------------------------------------------------

def _cache_path_for_hash(content_hash: str, model_version: str = "PaddleOCR-VL-1.6") -> Path:
    """按 (content_hash, model_version) 构造 cache 文件路径。

    与 ``core.paddleocr_cache._cache_path`` 同源但不走 file_path → file_hash 重算（已存于 doc.content_hash）。
    """
    return CACHE_DIR / f"{content_hash}_{model_version}.json"


def estimate_ocr_cost(docs: list) -> dict:
    """对一组 (doc, has_pages) 列表估算 OCR 缓存命中与配额消耗。

    返回 dict：``cached`` / ``uncached`` / ``pages_cached`` / ``pages_uncached`` /
    ``warnings``（超过 PAGE_LIMIT 的 doc 列表）/
    ``polluted_cached``（issue #99/05 前被 V8 cache defense 判废的 fallback_pdfplumber
    条目数；现在仍按 source 字段识别以便估算，实际命中由 ``paddleocr_cache.get_cached`` 决定）。
    """
    cached = 0
    uncached = 0
    pages_cached = 0
    pages_uncached = 0
    warnings = []
    polluted_cached = 0

    paddleocr_available = _paddleocr_currently_available()

    for doc, _has_pages in docs:
        page_count = doc.page_count or DEFAULT_PAGES_ESTIMATE
        if page_count > PAGE_LIMIT:
            warnings.append((doc, page_count))
            # 超限 doc 的 OCR 估算：服务端会截断，按 PAGE_LIMIT 计费更保守。
            page_count_for_cost = PAGE_LIMIT
        else:
            page_count_for_cost = page_count

        cache_path = _cache_path_for_hash(doc.content_hash) if doc.content_hash else None
        if cache_path and cache_path.exists():
            # 读 source 字段：识别历史 fallback_pdfplumber 污染条目
            # DEPRECATED: #99/05 — V8 cache defense 已删除，core.paddleocr_cache
            # 不再按 source 判废；本函数仍识别该 source 以估算污染数，但实际
            # 命中仍由 core.paddleocr_cache.get_cached 决定。
            try:
                entry = json.loads(cache_path.read_text(encoding="utf-8"))
                source = entry.get("source", "")
            except (OSError, json.JSONDecodeError):
                source = ""
            if source == "fallback_pdfplumber" and paddleocr_available:
                # 历史污染条目（layout=[]，需重 OCR 补齐）— 仅用于估算
                uncached += 1
                pages_uncached += page_count_for_cost
                polluted_cached += 1
            else:
                cached += 1
                pages_cached += page_count_for_cost
        else:
            uncached += 1
            pages_uncached += page_count_for_cost

    return {
        "cached": cached,
        "uncached": uncached,
        "pages_cached": pages_cached,
        "pages_uncached": pages_uncached,
        "warnings": warnings,
        "polluted_cached": polluted_cached,
    }


# ---------------------------------------------------------------------------
# 单 doc reparse + 轮询
# ---------------------------------------------------------------------------

def _wait_for_terminal(kb_id: str, doc_id: str, timeout_s: float) -> str:
    """轮询 ``embedding_status`` 直到进入终态。返回终态字符串；超时返回 ``"timeout"``。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        doc = doc_repo.get_doc(kb_id, doc_id)
        if doc is None:
            return "missing"
        if doc.embedding_status in _TERMINAL_STATUSES:
            return doc.embedding_status
        time.sleep(2)
    return "timeout"


def reparse_one(kb_id: str, doc) -> tuple[str, str]:
    """对一篇 doc 触发 reparse 并等待完成。返回 ``(doc_id, outcome)``。

    outcome ∈ ``{"done", "failed", "timeout", "raised:<err>"}``。
    """
    try:
        reparse_document(doc.id)  # 立即返回；后台线程执行
    except Exception as e:
        return (doc.id, f"raised:{type(e).__name__}:{e}")

    final = _wait_for_terminal(kb_id, doc.id, PER_DOC_TIMEOUT_S)
    return (doc.id, final)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def bulk_reparse(kb_id: str, *, dry_run: bool, concurrency: int, skip_confirm: bool) -> int:
    """批量 reparse 主入口。返回退出码（0 = 全部 done；1 = 有 failed；2 = 仅 dry-run / 用户取消）。"""
    kb = kb_repo.get(kb_id)
    if not kb:
        print(f"知识库不存在: {kb_id}", file=sys.stderr)
        return 1

    targets = list_target_docs(kb_id)
    if not targets:
        print(f"KB {kb.name} ({kb_id}) 无需 reparse：所有 doc 均已 embedded 且 pages 文件齐全。")
        return 0

    cost = estimate_ocr_cost(targets)

    print("=" * 70)
    print(f"知识库: {kb.name} ({kb_id})")
    print(f"目标 doc 数: {len(targets)} （其中 {cost['cached']} 命中 OCR 缓存 / {cost['uncached']} 需重 OCR）")
    print(f"预估 OCR 页数: {cost['pages_uncached']} 页（缓存命中页 {cost['pages_cached']} 不消耗）")
    print(f"并发: {concurrency}")
    if cost["warnings"]:
        print(f"⚠️  超 PAGE_LIMIT={PAGE_LIMIT} 的 doc ({len(cost['warnings'])} 篇)：")
        for doc, pc in cost["warnings"]:
            print(f"   - {doc.id} ({doc.original_name}) 约 {pc} 页")
    print("=" * 70)

    if dry_run:
        print("\n[DRY-RUN] 仅打印目标 doc 列表，不触发 reparse：")
        for doc, has_pages in targets:
            pages = doc.page_count or DEFAULT_PAGES_ESTIMATE
            cache_hit = bool(doc.content_hash and _cache_path_for_hash(doc.content_hash).exists())
            tag = "CACHED" if cache_hit else "OCR"
            pages_tag = "PAGES" if has_pages else "NO-PAGES"
            print(f"  [{tag:5s}] [{pages_tag:9s}] {doc.id}  {doc.original_name}  ({pages} 页)")
        return 2

    # 实际 run：超限 doc 拦截（dry-run 不拦，仅警告）
    over_limit = [d for d, _ in targets if (d.page_count or 0) > PAGE_LIMIT]
    if over_limit and not skip_confirm:
        print(f"\n⚠️  检测到 {len(over_limit)} 篇 doc 超过 {PAGE_LIMIT} 页上限，run 将自动跳过这些 doc。")
        print("   （PaddleOCR 服务端会截断，避免静默丢内容；issue #87 决议）")
    elif over_limit:
        print(f"\n⚠️  跳过 {len(over_limit)} 篇超过 {PAGE_LIMIT} 页的 doc（详见 dry-run 输出）。")

    if not skip_confirm:
        prompt = (
            f"\n将触发 {len(targets) - len(over_limit)} 篇 reparse "
            f"（其中 {cost['uncached']} 篇需 OCR 配额，预估 {cost['pages_uncached']} 页）。\n"
            f"确认执行？[y/N] "
        )
        try:
            ans = input(prompt).strip().lower()
        except EOFError:
            ans = ""
        if ans != "y":
            print("已取消。")
            return 2

    # 执行：用 ThreadPoolExecutor 控制并发（信号量等价物）。
    # 注：KB 级 RLock 会自然序列化索引写入；executor 限制的是后台线程数。
    done: list[str] = []
    failed: list[tuple[str, str]] = []
    skipped_over_limit: list[tuple[str, int]] = [(d.id, d.page_count or 0) for d in over_limit]

    todo = [d for d, _ in targets if (d.page_count or 0) <= PAGE_LIMIT]

    total = len(todo)
    print(f"\n开始 reparse {total} 篇（已跳过 {len(skipped_over_limit)} 篇超限）...\n")

    completed = 0
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(reparse_one, kb_id, doc): doc for doc in todo}
        for fut in futures:
            doc = futures[fut]
            try:
                _doc_id, outcome = fut.result()
            except Exception as e:
                outcome = f"raised:{type(e).__name__}:{e}"

            completed += 1
            label = "done" if outcome == "embedded" else (
                "failed" if outcome == "failed" else outcome
            )
            print(f"  [{completed}/{total}] [{label}] {doc.id} ({doc.original_name})")

            if outcome == "embedded":
                done.append(doc.id)
            else:
                failed.append((doc.id, outcome))

    print("\n" + "=" * 70)
    print(f"完成统计：")
    print(f"  done:    {len(done)}")
    print(f"  failed:  {len(failed)}")
    print(f"  skipped: {len(skipped_over_limit)} （超 PAGE_LIMIT={PAGE_LIMIT}）")
    if failed:
        print("\n失败列表：")
        for doc_id, reason in failed:
            print(f"  {doc_id}  ←  {reason}")
    if skipped_over_limit:
        print("\n跳过列表（超 PAGE_LIMIT）：")
        for doc_id, pc in skipped_over_limit:
            print(f"  {doc_id}  （约 {pc} 页）")
    print("=" * 70)

    return 0 if not failed else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="对整个 KB 触发批量 reparse（Wayfinder #89）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--kb-id", required=True, help="目标知识库 ID（ULID）")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅打印目标 doc 列表与 OCR 成本估算，不触发 reparse",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="并发数（信号量限制，默认 4；issue #87 决议 γ）",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="跳过二次确认 prompt（CI / 已知环境用）",
    )
    args = parser.parse_args()

    if args.concurrency < 1:
        print("--concurrency 必须 >= 1", file=sys.stderr)
        return 1

    return bulk_reparse(
        args.kb_id,
        dry_run=args.dry_run,
        concurrency=args.concurrency,
        skip_confirm=args.yes,
    )


if __name__ == "__main__":
    sys.exit(main())