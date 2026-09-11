"""对整个 KB 触发批量重新解析 (Bulk Reparse) 的命令行入口（Wayfinder #86 / #89）。

**薄 wrapper**：领域逻辑（待重解析文档选取 / OCR 成本预检 / 拆分成本分类 /
受控并发编排 / 实测 OCR 计数与报告落盘）全部住在 ``services.bulk_reparse_service``，
与 HTTP API 共用同一实现（issue #108 / #110）。本脚本只负责四件事：
``.env`` 加载、argparse 契约、终端渲染、退出码。

不依赖 HTTP 服务在线 —— 离线运维、无前端环境仍可用。

退出码：0 = 全部成功 / 1 = 有失败（或 KB 不存在 / 参数非法）/ 2 = dry-run 或用户取消。

跑完的结构化报告（预估 vs 实测 OCR 页数、done/failed/skipped 明细）落在
``data/kbs/{kb_id}/bulk_reparse_report.json``，终端摘要只是它的一个视图。

用法：
  # 仅枚举目标 doc，不触发 reparse
  uv run python scripts/bulk_reparse.py --kb-id <kb_id> --dry-run

  # 实际跑（需要二次确认）
  uv run python scripts/bulk_reparse.py --kb-id <kb_id>

  # 跳过确认（CI / 已知环境用）
  uv run python scripts/bulk_reparse.py --kb-id <kb_id> --yes

  # 自定义并发
  uv run python scripts/bulk_reparse.py --kb-id <kb_id> --concurrency 8

  # 忽略三条选取规则，整库重建（换解析器后用）
  uv run python scripts/bulk_reparse.py --kb-id <kb_id> --force

  # 显式绕过拆分成本护栏（``--force`` 不能绕的成本阈值由这个开关出口）
  uv run python scripts/bulk_reparse.py --kb-id <kb_id> --ignore-cost-limit
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# 确保能找到项目模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 加载 .env（与 api/main.py:5-7 同源）— PaddleOCR 凭证必须在
# core.parse_document 调用前就位。wayfinder #93 提到的 layout=[] 假成功路径
# 已在 #99/05 修复（删除 _pdf_fallback），但 .env 加载仍是防回归的最简防线。
from dotenv import load_dotenv

_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    load_dotenv(_env_path)

os.environ.setdefault("AUDIT_DATA_DIR", "data")

import storage.kb_repo as kb_repo
from core.settings import BULK_REPARSE_SPLIT_COST_LIMIT_PAGES
from core.logger import get_logger
from services import bulk_reparse_service as bulk_svc

_logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 终端渲染
# ---------------------------------------------------------------------------

def _print_header(kb, kb_id: str, targets, cost, concurrency: int) -> None:
    """打印批量头信息（issue #181 / #182）。

    段落按"事实→决策辅助→拦截"顺序：先 OCR 估算，再走拆分解析路径的聚合
    段（issue #181 新口径，#182 改文案），最后才是被成本阈值挡住的清单。
    旧版"⚠️ 超 PAGE_LIMIT 的 doc 将被跳过"已无意义 —— 拆分路径下不再跳过。
    """
    print("=" * 70)
    print(f"知识库: {kb.name} ({kb_id})")
    print(f"目标 doc 数: {len(targets)} （其中 {cost.cached} 命中 OCR 缓存 / {cost.uncached} 需重 OCR）")
    print(f"预估 OCR 页数: {cost.pages_uncached} 页（缓存命中页 {cost.pages_cached} 不消耗）")
    if cost.will_split_docs:
        # 改文案：旧版"⚠️ 超 PADDLEOCR_PAGE_LIMIT=100 的 doc"是失败主义口吻，
        # 真实语义是"这篇要走拆分解析"，由 issue #182 翻成正面描述（spec #182 AC）。
        print(
            f"将拆 {len(cost.will_split_docs)} 篇 / "
            f"共 {cost.chunks_total} 块 / "
            f"总 {cost.ocr_pages_total} 页 OCR："
        )
        for plan in cost.will_split_docs:
            print(
                f"  - {plan.doc.id}: {plan.doc.original_name} "
                f"({plan.page_count} 页 → {plan.chunks_planned} 块)"
            )
    if cost.cost_exceeded_docs:
        # 改文案："超拆分成本阈值"→"超成本阈值"（issue #182 / spec §F），
        # 不动结构 —— 仍然列出被挡的每篇 doc。
        print(
            f"⚠️  超成本阈值将被跳过 "
            f"({len(cost.cost_exceeded_docs)} 篇，"
            f"阈值 {BULK_REPARSE_SPLIT_COST_LIMIT_PAGES} 页)："
        )
        for over in cost.cost_exceeded_docs:
            print(f"   - {over.doc.id} ({over.doc.original_name}) 约 {over.page_count} 页")
    print(f"并发: {concurrency}")
    print("=" * 70)


def _print_dry_run(targets) -> None:
    print("\n[DRY-RUN] 仅打印目标 doc 列表，不触发 reparse：")
    for target in targets:
        tag = "CACHED" if bulk_svc.is_cache_hit(target.doc) else "OCR"
        pages_tag = "PAGES" if target.has_pages_file else "NO-PAGES"
        split_tag = " [SPLIT]" if target.will_split else ""
        print(
            f"  [{tag:5s}] [{pages_tag:9s}] {target.doc.id}  "
            f"{target.doc.original_name}  ({target.estimated_page_count} 页){split_tag}"
        )


def _print_summary(result) -> None:
    print("\n" + "=" * 70)
    print(f"完成统计：")
    print(f"  done:    {len(result.done)}")
    print(f"  failed:  {len(result.failed)}")
    print(f"  skipped: {len(result.skipped)} （超拆分成本阈值）")

    # 预估 vs 实测并排 —— #91 那次"报 1694 页、实际 0 页"的指纹就在这两行的差值里。
    # 差异不拦截，只呈现（spec #102 story 26）。
    print(f"\nOCR 页数：预估 {result.estimate.pages_uncached} 页 / 实测 {result.usage.actual_ocr_pages} 页")
    if result.usage.pages_by_source:
        print("  实际解析来源分布：")
        for source, pages in sorted(result.usage.pages_by_source.items()):
            docs = result.usage.docs_by_source.get(source, 0)
            print(f"    {source:12s} {docs} 篇 / {pages} 页")

    if result.failed:
        print("\n失败列表：")
        for doc_id, reason in result.failed:
            print(f"  {doc_id}  ←  {reason}")
    if result.skipped:
        print("\n跳过列表（超拆分成本阈值）：")
        for skipped in result.skipped:
            print(f"  {skipped.doc.id}  （约 {skipped.page_count} 页）")
    if result.report_path:
        print(f"\n报告已写入: {result.report_path}")
    print("=" * 70)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def bulk_reparse(
    kb_id: str,
    *,
    dry_run: bool,
    concurrency: int,
    skip_confirm: bool,
    force: bool = False,
    ignore_cost_limit: bool = False,
) -> int:
    """批量重新解析主入口。返回退出码（0 = 全部 done；1 = 有 failed；2 = dry-run / 用户取消）。"""
    kb = kb_repo.get(kb_id)
    if not kb:
        print(f"知识库不存在: {kb_id}", file=sys.stderr)
        return 1

    targets = bulk_svc.list_target_docs(kb_id, force=force)
    if not targets:
        print(f"KB {kb.name} ({kb_id}) 无需 reparse：所有 doc 均已 embedded 且 pages 文件齐全。")
        return 0

    cost = bulk_svc.estimate_ocr_cost(targets)
    _print_header(kb, kb_id, targets, cost, concurrency)

    if dry_run:
        _print_dry_run(targets)
        return 2

    # 实际 run：拆分成本阈值拦截（dry-run 不拦，仅警告）。
    # ``ignore_cost_limit=True`` 时拆分器返回 ``(targets, [])`` → 全部进 runnable。
    runnable, cost_exceeded = bulk_svc.split_by_cost_limit(
        targets, ignore_cost_limit=ignore_cost_limit,
    )
    # ``force + cost-exceeded`` 的特殊路径（issue #182 / spec §F）：
    # 运维按了 ``--force`` 但被成本护栏挡住的 doc **仍然**会被跳过 —— 在 run
    # 起始打一条 ⚠️ banner + ``log WARNING``，**继续**跑，退出码不变。
    # 与"非 force 路径"的提示区分开：那是普通 confirm 前提示，本路径是强提示。
    if cost_exceeded and force and not ignore_cost_limit:
        warn = (
            f"⚠️  --force 模式下仍有 {len(cost_exceeded)} 篇 doc 超拆分成本阈值 "
            f"({BULK_REPARSE_SPLIT_COST_LIMIT_PAGES} 页) 会被跳过 —— "
            f"--force 不能绕过成本护栏；显式 --ignore-cost-limit 才可绕过"
        )
        print(f"\n{warn}")
        _logger.warning("bulk_reparse CLI: %s", warn)
    elif cost_exceeded and not skip_confirm:
        print(
            f"\n⚠️  检测到 {len(cost_exceeded)} 篇 doc 超拆分成本阈值 "
            f"({BULK_REPARSE_SPLIT_COST_LIMIT_PAGES} 页)，"
            f"run 将自动跳过这些 doc。"
        )
        print("   （--force 不能绕过；显式 --ignore-cost-limit 才可绕过，issue #181）")
    elif cost_exceeded:
        print(
            f"\n⚠️  跳过 {len(cost_exceeded)} 篇超拆分成本阈值的 doc "
            f"（详见 dry-run 输出）。"
        )

    if not skip_confirm:
        prompt = (
            f"\n将触发 {len(runnable)} 篇 reparse "
            f"（其中 {cost.uncached} 篇需 OCR 配额，预估 {cost.pages_uncached} 页）。\n"
            f"确认执行？[y/N] "
        )
        try:
            ans = input(prompt).strip().lower()
        except EOFError:
            ans = ""
        if ans != "y":
            print("已取消。")
            return 2

    print(f"\n开始 reparse {len(runnable)} 篇（已跳过 {len(cost_exceeded)} 篇超成本）...\n")

    def _on_doc_complete(completed: int, total: int, doc, outcome: str) -> None:
        label = "done" if outcome == "embedded" else (
            "failed" if outcome == "failed" else outcome
        )
        print(f"  [{completed}/{total}] [{label}] {doc.id} ({doc.original_name})")

    result = bulk_svc.run_bulk_reparse(
        kb_id, targets,
        concurrency=concurrency,
        forced=force,
        ignore_cost_limit=ignore_cost_limit,
        on_doc_complete=_on_doc_complete,
    )

    _print_summary(result)

    return 0 if not result.failed else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="对整个 KB 触发批量重新解析（Wayfinder #89）",
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
        default=bulk_svc.DEFAULT_CONCURRENCY,
        help=f"并发数（信号量限制，默认 {bulk_svc.DEFAULT_CONCURRENCY}；issue #87 决议 γ）",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="跳过二次确认 prompt（CI / 已知环境用）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="忽略三条选取规则，把整库全部 doc 当作目标（换解析器后的整库重建）",
    )
    parser.add_argument(
        "--ignore-cost-limit",
        action="store_true",
        help="显式绕过拆分成本护栏（--force 不能绕；issue #181）",
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
        force=args.force,
        ignore_cost_limit=args.ignore_cost_limit,
    )


if __name__ == "__main__":
    sys.exit(main())
