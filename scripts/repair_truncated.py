"""对 KB 内的"服务端静默截短"文档触发重新解析的命令行入口（spec #173 / issue #180）。

薄 wrapper：领域逻辑（检测 + 修复动作）全在 service 层
（:func:`core.truncated_doc_detector.find_truncated_docs` 与
:func:`services.bulk_reparse_service.reparse_one`），本脚本只负责四件事：
``.env`` 加载、argparse 契约、终端渲染、退出码。

退出码（沿用 :mod:`scripts.bulk_reparse` 契约）：0 = 全部 repaired / 1 = 有 failed /
2 = dry-run。

跑完的结构化报告（found / repaired / failed 明细）落在
``data/kbs/{kb_id}/truncated_report.json``（``bulk_reparse_report.json`` 的兄弟，
同套心智模型：KB 磁盘产物、可读 JSON、只留最近一次）。``--dry-run`` 时报告是唯一
交付物；非 dry-run 时报告是修复回执。

不调用 ``parse_document`` 裸路径：必须走 ``reparse_one``，因为验收点
"``embedding_status == 'embedded'`` 且 ``len(by_page) == physical_pages``"
只有完整 **重新解析** 流程（解析 → 按页文本 → 重建索引 → 状态）能达成，
``reparse_one`` 同时给到 per-doc 隔离与终态字符串。

用法：
  # 默认扫描 data/kbs/* 全部 KB，仅打印截短 doc 列表 + 写报告，不触发 reparse
  uv run python scripts/repair_truncated.py --dry-run

  # 收窄到一个 KB，实际跑
  uv run python scripts/repair_truncated.py --kb-id <kb_id>

  # 收窄到一个 KB，仅打印
  uv run python scripts/repair_truncated.py --kb-id <kb_id> --dry-run
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# 确保能找到项目模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 加载 .env（与 api/main.py / scripts/bulk_reparse.py 同源）—— PaddleOCR 凭证必须
# 在 reparse_one 调用前就位。bulk_reparse.py 的 .env 加载是 #93 回归防线，
# repair_truncated.py 与它走同一段路径（#93 的"layout=[] 假成功"修复后，
# .env 加载仍是防回归的最简防线）。
from dotenv import load_dotenv

_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    load_dotenv(_env_path)

os.environ.setdefault("AUDIT_DATA_DIR", "data")


import storage.doc_repo as doc_repo
import storage.kb_repo as kb_repo
from core import truncated_report_store
from core.truncated_doc_detector import find_truncated_docs
from services.bulk_reparse_service import reparse_one


# 报告 schema 版本：字段增删时递增，让未来的报告端点与旧文件能互相识别。
# 报告文件名 ``truncated_report.json`` 由 :mod:`core.truncated_report_store`
# 统一管，CLI 不再自带（与 :mod:`core.bulk_reparse_report_store` 同源同构）。
REPORT_SCHEMA_VERSION = 1


# ── KB 枚举 ─────────────────────────────────────────────────────────────────────


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _list_all_kb_ids() -> list[str]:
    """``data/kbs/*`` 下全部 KB ID，按字典序。

    与 :func:`storage.kb_repo.list_all` 同源 —— 只列有 ``kb.json`` 的目录，
    无元数据的孤儿目录（如 ``data/kbs/foo/`` 缺 ``kb.json``）不当作 KB。
    """
    return [kb.id for kb in kb_repo.list_all()]


# ── 终端渲染 ────────────────────────────────────────────────────────────────────


def _print_header(kb_ids: list[str], found_by_kb: dict[str, int]) -> None:
    total = sum(found_by_kb.values())
    print("=" * 70)
    print(f"扫描 KB 数: {len(kb_ids)}  截短 doc 总数: {total}")
    for kb_id in kb_ids:
        n = found_by_kb.get(kb_id, 0)
        if n:
            print(f"  - {kb_id}: {n} 篇截短 doc")
    print("=" * 70)


def _print_dry_run(truncated) -> None:
    print("\n[DRY-RUN] 仅打印截短 doc 列表，不触发 reparse：")
    for t in truncated:
        print(
            f"  [TRUNCATED] {t.doc.id}  {t.doc.original_name}  "
            f"({t.parsed_pages}/{t.physical_pages} 页)"
        )


def _print_summary(repaired: list[str], failed: list[tuple[str, str]]) -> None:
    print("\n" + "=" * 70)
    print("完成统计：")
    print(f"  repaired: {len(repaired)}")
    print(f"  failed:   {len(failed)}")
    if failed:
        print("\n失败列表：")
        for doc_id, reason in failed:
            print(f"  {doc_id}  ←  {reason}")
    print("=" * 70)


# ── 单 KB 修复 ───────────────────────────────────────────────────────────────────


def _build_report(
    kb_id: str, kb_name: str, found, *, dry_run: bool, started_at: str,
) -> dict:
    """组装报告 dict（纯函数，不落盘）。"""
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "kb_id": kb_id,
        "kb_name": kb_name,
        "started_at": started_at,
        "finished_at": "",
        "dry_run": dry_run,
        "found": [
            {
                "doc_id": t.doc.id,
                "original_name": t.doc.original_name,
                "parsed_pages": t.parsed_pages,
                "physical_pages": t.physical_pages,
            }
            for t in found
        ],
        "repaired": [],
        "failed": [],
    }


def _repair_one_kb(kb_id: str, *, dry_run: bool) -> tuple[Optional[Path], int]:
    """对单个 KB 触发截短修复。返回 ``(report_path, exit_code_for_this_kb)``。

    报告无论 dry-run / 非 dry-run 都落盘 —— dry-run 用作"行动清单"，非 dry-run
    用作"修复回执"，同套心智模型（``bulk_reparse_report.json`` 同源）。
    """
    kb = kb_repo.get(kb_id)
    if kb is None:
        print(f"知识库不存在: {kb_id}", file=sys.stderr)
        return (None, 1)

    truncated = find_truncated_docs(kb_id)
    started_at = _utc_now()
    report = _build_report(
        kb_id, kb.name, truncated, dry_run=dry_run, started_at=started_at,
    )

    if not truncated:
        print(f"KB {kb.name} ({kb_id}) 无截短 doc。")
        return (None, 0)

    if dry_run:
        _print_dry_run(truncated)
        report["finished_at"] = _utc_now()
        path = truncated_report_store.save_report(kb_id, report)
        print(f"\n报告已写入: {path}")
        return (path, 2)

    # 非 dry-run：逐篇修复
    print(f"\n开始修复 {len(truncated)} 篇截短 doc ...\n")
    repaired: list[str] = []
    failed: list[tuple[str, str]] = []
    for i, t in enumerate(truncated, start=1):
        doc_id = t.doc.id
        doc_name = t.doc.original_name
        print(
            f"  [{i}/{len(truncated)}] {doc_id} ({doc_name}) "
            f"({t.parsed_pages}/{t.physical_pages} 页) → ",
            end="", flush=True,
        )
        # 崩溃可恢复点：先 mark_doc_embedding_truncated，再触发 reparse。
        # 后续若脚本被 Ctrl-C，下一次 bulk_reparse.py 的第一条选取规则
        # 会自然拾起（embedding_status != "embedded"），无需在 repair_truncated
        # 里写特殊恢复逻辑 —— 见 issue #180 AC5。
        doc_repo.mark_doc_embedding_truncated(kb_id, doc_id)
        _, outcome = reparse_one(kb_id, t.doc)
        print(outcome)
        if outcome == "embedded":
            repaired.append(doc_id)
        else:
            failed.append((doc_id, outcome))

    report["repaired"] = repaired
    report["failed"] = [{"doc_id": d, "reason": r} for d, r in failed]
    report["finished_at"] = _utc_now()
    path = truncated_report_store.save_report(kb_id, report)
    _print_summary(repaired, failed)
    print(f"\n报告已写入: {path}")
    return (path, 0 if not failed else 1)


# ── 主流程 ─────────────────────────────────────────────────────────────────────


def repair_truncated(*, kb_id: Optional[str] = None, dry_run: bool = False) -> int:
    """对 ``kb_id`` 指定的一个 KB（``kb_id=None`` 时为 ``data/kbs/*`` 全部）
    触发截短修复。返回退出码（0 = 全部 repaired / 1 = 有 failed / 2 = dry-run）。
    """
    if kb_id is None:
        kb_ids = _list_all_kb_ids()
        if not kb_ids:
            print("未找到任何 KB（data/kbs/* 下无 kb.json）。", file=sys.stderr)
            return 1
    else:
        kb_ids = [kb_id]

    # 头部总览：每个 KB 的截短 doc 数（提前枚举，避免重复 IO）
    found_by_kb: dict[str, int] = {}
    for kid in kb_ids:
        found_by_kb[kid] = len(find_truncated_docs(kid))
    total_found = sum(found_by_kb.values())

    if total_found == 0:
        print(f"扫描 {len(kb_ids)} 个 KB：无截短 doc。")
        return 0

    _print_header(kb_ids, found_by_kb)

    # dry-run：每个 KB 都写一份报告，但不发动作
    if dry_run:
        for kid in kb_ids:
            if found_by_kb[kid] == 0:
                continue
            _, code = _repair_one_kb(kid, dry_run=True)
            assert code == 2, f"dry-run 子入口退出码应为 2，实际 {code}"
        return 2

    # 非 dry-run：逐个 KB 跑
    any_failed = False
    for kid in kb_ids:
        if found_by_kb[kid] == 0:
            continue
        _, code = _repair_one_kb(kid, dry_run=False)
        if code == 1:
            any_failed = True

    return 1 if any_failed else 0


# ── CLI ─────────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "对 KB 内的服务端静默截短 doc 触发重新解析（spec #173 / issue #180）"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--kb-id",
        default=None,
        help="目标知识库 ID（ULID）；缺省扫描 data/kbs/* 全部 KB",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅打印截短 doc 列表与写 truncated_report.json，不触发 reparse",
    )
    args = parser.parse_args()

    return repair_truncated(kb_id=args.kb_id, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
