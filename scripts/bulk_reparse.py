"""对整个 KB 触发批量重新解析 (Bulk Reparse) 的命令行入口（Wayfinder #86 / #89）。

**薄 wrapper**：领域逻辑（待重解析文档选取 / OCR 成本预检 / 页数上限分类 /
受控并发编排）全部住在 ``services.bulk_reparse_service``，与 HTTP API 共用同一实现
（issue #108）。本脚本只负责四件事：``.env`` 加载、argparse 契约、终端渲染、退出码。

不依赖 HTTP 服务在线 —— 离线运维、无前端环境仍可用。

退出码：0 = 全部成功 / 1 = 有失败（或 KB 不存在 / 参数非法）/ 2 = dry-run 或用户取消。

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
from services import bulk_reparse_service as bulk_svc


# ---------------------------------------------------------------------------
# 终端渲染
# ---------------------------------------------------------------------------

def _print_header(kb, kb_id: str, targets, cost, concurrency: int) -> None:
    print("=" * 70)
    print(f"知识库: {kb.name} ({kb_id})")
    print(f"目标 doc 数: {len(targets)} （其中 {cost.cached} 命中 OCR 缓存 / {cost.uncached} 需重 OCR）")
    print(f"预估 OCR 页数: {cost.pages_uncached} 页（缓存命中页 {cost.pages_cached} 不消耗）")
    print(f"并发: {concurrency}")
    if cost.over_page_limit:
        print(f"⚠️  超 PAGE_LIMIT={bulk_svc.PAGE_LIMIT} 的 doc ({len(cost.over_page_limit)} 篇)：")
        for over in cost.over_page_limit:
            print(f"   - {over.doc.id} ({over.doc.original_name}) 约 {over.page_count} 页")
    print("=" * 70)


def _print_dry_run(targets) -> None:
    print("\n[DRY-RUN] 仅打印目标 doc 列表，不触发 reparse：")
    for target in targets:
        tag = "CACHED" if bulk_svc.is_cache_hit(target.doc) else "OCR"
        pages_tag = "PAGES" if target.has_pages_file else "NO-PAGES"
        print(
            f"  [{tag:5s}] [{pages_tag:9s}] {target.doc.id}  "
            f"{target.doc.original_name}  ({target.estimated_page_count} 页)"
        )


def _print_summary(result) -> None:
    print("\n" + "=" * 70)
    print(f"完成统计：")
    print(f"  done:    {len(result.done)}")
    print(f"  failed:  {len(result.failed)}")
    print(f"  skipped: {len(result.skipped)} （超 PAGE_LIMIT={bulk_svc.PAGE_LIMIT}）")
    if result.failed:
        print("\n失败列表：")
        for doc_id, reason in result.failed:
            print(f"  {doc_id}  ←  {reason}")
    if result.skipped:
        print("\n跳过列表（超 PAGE_LIMIT）：")
        for skipped in result.skipped:
            print(f"  {skipped.doc.id}  （约 {skipped.page_count} 页）")
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

    # 实际 run：超限 doc 拦截（dry-run 不拦，仅警告）
    runnable, over_limit = bulk_svc.split_by_page_limit(targets)
    if over_limit and not skip_confirm:
        print(
            f"\n⚠️  检测到 {len(over_limit)} 篇 doc 超过 {bulk_svc.PAGE_LIMIT} 页上限，"
            f"run 将自动跳过这些 doc。"
        )
        print("   （服务端会截断，避免静默丢内容；issue #87 决议）")
    elif over_limit:
        print(f"\n⚠️  跳过 {len(over_limit)} 篇超过 {bulk_svc.PAGE_LIMIT} 页的 doc（详见 dry-run 输出）。")

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

    print(f"\n开始 reparse {len(runnable)} 篇（已跳过 {len(over_limit)} 篇超限）...\n")

    def _on_doc_complete(completed: int, total: int, doc, outcome: str) -> None:
        label = "done" if outcome == "embedded" else (
            "failed" if outcome == "failed" else outcome
        )
        print(f"  [{completed}/{total}] [{label}] {doc.id} ({doc.original_name})")

    result = bulk_svc.run_bulk_reparse(
        kb_id, targets,
        concurrency=concurrency,
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
    )


if __name__ == "__main__":
    sys.exit(main())