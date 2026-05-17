"""报告格式化：控制台 + JSON。"""

import json
from benchmark.models import BenchmarkRun


def format_console(run: BenchmarkRun) -> str:
    """生成人类可读的控制台报告。"""
    cfg = run.config
    lines = []
    lines.append("=" * 60)
    lines.append("  搜索基准测试结果")
    lines.append("=" * 60)
    lines.append(f"配置: max_chars={cfg.max_chars} overlap={cfg.overlap} "
                 f"threshold={cfg.similarity_threshold} top_k={cfg.top_k}")
    lines.append(f"运行时间: {run.run_at} (耗时 {run.duration}s)")
    lines.append("")

    a = run.aggregate
    lines.append("── 聚合指标 ──────────────────")
    lines.append(f"  Precision@{cfg.top_k}    {a.mean_precision:.4f}")
    lines.append(f"  Recall          {a.mean_recall:.4f}")
    lines.append(f"  MRR             {a.mrr:.4f}")
    lines.append(f"  命中用例        {a.cases_with_match} / {a.total_cases}")
    lines.append("")

    lines.append("── 逐用例详情 ──────────────────")
    for sr in run.per_case:
        icon = "✓" if sr.reciprocal_rank > 0 else "✗"
        lines.append(
            f"  {icon} {sr.test_id:<20} "
            f"P@{cfg.top_k}={sr.precision_at_k:.3f}  "
            f"Recall={sr.recall:.3f}  "
            f"RR={sr.reciprocal_rank:.3f}  "
            f"[{sr.details}]"
        )

    lines.append("")
    return "\n".join(lines)


def format_json(run: BenchmarkRun) -> str:
    """生成机器可读的 JSON 报告。"""
    return json.dumps(run.model_dump(mode="json"), ensure_ascii=False, indent=2)


def format_sweep_table(runs: list[BenchmarkRun], sort_by: str = "mrr") -> str:
    """生成参数扫描对比表（前 10 个配置）。"""
    key_fn = {
        "mrr": lambda r: r.aggregate.mrr,
        "precision": lambda r: r.aggregate.mean_precision,
        "recall": lambda r: r.aggregate.mean_recall,
    }.get(sort_by, lambda r: r.aggregate.mrr)

    sorted_runs = sorted(runs, key=key_fn, reverse=True)

    lines = []
    lines.append("=" * 80)
    lines.append(f"  参数扫描对比（按 {sort_by} 排序）")
    lines.append("=" * 80)
    lines.append(f" {'#':<4} {'chunk':>6} {'overlap':>7} {'thresh':>7} "
                 f"{'top_k':>6} {'P@K':<8} {'Recall':<8} {'MRR':<8}  {'t(s)':<6}")
    lines.append(" " + "-" * 64)
    for i, r in enumerate(sorted_runs[:10], 1):
        c = r.config
        lines.append(
            f" {i:<3} {c.max_chars:>6} {c.overlap:>7} {c.similarity_threshold:>7.1f} "
            f"{c.top_k:>6} {r.aggregate.mean_precision:<8.4f} "
            f"{r.aggregate.mean_recall:<8.4f} {r.aggregate.mrr:<8.4f}  {r.duration:<6.2f}"
        )
    lines.append("")
    return "\n".join(lines)
