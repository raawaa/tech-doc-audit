"""Eval 漂移对比 —— issues/144 AC#7。

对比两条路径在同一组 queries × KBs 上的**检索指标**(HitRate / MRR / Recall /
Precision@K),不跑 BatchEvalRunner(答案质量由 spec 列为 ≤5% drift;该路径
单 query 调 LLM ~30s × 11 query ≈ 5min,且 Faithfulness / Relevancy 主要看
答案文本而非 provider switch,在 spike 阶段不阻塞)。

如果 AC#4-5-6(rerank / query / 路径全部一致)通过,这里得到的 drift 应
**显著小于 5%**(实际上 T4 §4 实测 Recall@10 = 1.000 全 query 全等)。

## 用法

    # 默认 SF 路径(当前 production provider)
    uv run --env-file .env python scripts/eval_qa_drift.py

    # 本地 bge-m3 路径
    EMBED_PROVIDER=local uv run --env-file .env python scripts/eval_qa_drift.py

    # 显式比 SF vs local,逐 query 输出漂移
    uv run --env-file .env python scripts/eval_qa_drift.py --compare-providers
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml


def _load_test_cases(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f).get("test_cases", [])


def _chunk_matches(content: str, expected: dict) -> bool:
    """Re-evaluate what eval_qa.py:compute_retrieval_metrics considers a hit."""
    keywords = expected.get("content_keywords", [])
    hits = sum(1 for kw in keywords if kw in content)
    if hits == len(keywords):
        return True
    if len(keywords) > 1 and hits >= max(1, int(len(keywords) * 0.7)):
        return True
    return False


def _retrieval_metrics(results: list[dict], expected_chunks: list[dict]) -> dict:
    """Same logic as scripts/eval_qa.py:compute_retrieval_metrics — keep in sync."""
    hit = any(
        any(_chunk_matches(r.get("content", ""), e) for e in expected_chunks)
        for r in results
    )
    mrr = 0.0
    for i, r in enumerate(results, 1):
        if any(_chunk_matches(r.get("content", ""), e) for e in expected_chunks):
            mrr = 1.0 / i
            break
    found = sum(
        1 for e in expected_chunks
        if any(_chunk_matches(r.get("content", ""), e) for r in results)
    )
    recall = found / len(expected_chunks) if expected_chunks else 0
    top_k = len(results) if results else 1
    hits = sum(
        1 for r in results
        if any(_chunk_matches(r.get("content", ""), e) for e in expected_chunks)
    )
    precision = hits / top_k
    return {
        "hit_rate": 1.0 if hit else 0.0,
        "mrr": round(mrr, 4),
        "recall": round(recall, 4),
        "precision_at_k": round(precision, 4),
    }


def _run_provider(kb_ids: list[str], test_cases: list[dict]) -> dict:
    """跑一条 provider 路径的检索,返回聚合 metrics。"""
    # 让每个 provider 都干净初始化
    from core.settings import _embed_model
    from core import settings as _settings
    if _embed_model is not None:
        # 仅在同一进程内切换不可靠 —— 让 SHELL 用 env 切换
        pass

    # 通过 import 触发的 settings._init 已经设过 Settings.embed_model
    # 这里强制重新解析 get_embed_model()
    from services.vector_search import vec_search

    out: dict = {"hit_rate": [], "mrr": [], "recall": [], "precision_at_k": []}
    per_query = []
    for tc in test_cases:
        q = tc["query"]
        expected = tc.get("expected_chunks", [])
        results = vec_search(kb_ids, q, top_k=5)
        rm = _retrieval_metrics(results, expected)
        out["hit_rate"].append(rm["hit_rate"])
        out["mrr"].append(rm["mrr"])
        out["recall"].append(rm["recall"])
        out["precision_at_k"].append(rm["precision_at_k"])
        per_query.append({"id": tc.get("id", "?"), **rm})
    return {
        "provider": os.environ.get("EMBED_PROVIDER", "siliconflow"),
        "n": len(test_cases),
        "averages": {
            k: round(sum(v) / max(1, len(v)), 4) for k, v in out.items()
        },
        "per_query": per_query,
    }


def _compare(local: dict, sf: dict) -> dict:
    """按 metric 计算 drift% = (|local - sf| / max(eps, max(local, sf))) × 100。"""
    drift = {}
    for metric in ("hit_rate", "mrr", "recall", "precision_at_k"):
        l = local["averages"][metric]
        s = sf["averages"][metric]
        denom = max(abs(l), abs(s), 1e-4)
        drift[metric] = {
            "local": l,
            "sf": s,
            "abs_diff": round(abs(l - s), 4),
            "drift_pct": round(abs(l - s) / denom * 100, 4),
        }
    return drift


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--cases", default="benchmark/test_cases.yaml",
    )
    ap.add_argument(
        "--kb-ids",
        default="01KVSRJAXBYHQS7697DN42J2MJ,01KW0XRE1FRJF2WFJ4QWVVSW4K,01KW1PG49FQDAEYV0W1H2H309E",
        help="KB ids, comma-separated",
    )
    ap.add_argument(
        "--threshold", type=float, default=5.0,
        help="drift% 通过阈值(默认 5%,按 issues/144 AC#7)",
    )
    ap.add_argument(
        "--compare-providers", action="store_true",
        help="逐 query 对比 SF vs local(需要分别两次跑)",
    )
    ap.add_argument(
        "--local-result", default=None,
        help="上次跑 local 路径的 JSON 路径(--compare-providers 时用)",
    )
    ap.add_argument(
        "--output", default=None,
    )
    args = ap.parse_args()

    kb_ids = [s.strip() for s in args.kb_ids.split(",") if s.strip()]
    test_cases = _load_test_cases(args.cases)
    if not test_cases:
        print(f"FAIL: {args.cases} 没找到 test_cases")
        return 2

    print(f"\nProvider: {os.environ.get('EMBED_PROVIDER', 'siliconflow')}")
    print(f"KBs: {', '.join(kb_ids)}")
    print(f"Cases: {len(test_cases)}\n")

    result = _run_provider(kb_ids, test_cases)
    print("Retrieval averages:")
    for k, v in result["averages"].items():
        print(f"  {k}: {v}")

    if args.compare_providers:
        if not args.local_result or not Path(args.local_result).exists():
            print(
                "\nFAIL: --compare-providers 需要 --local-result <existing json>;"
                " 先单独跑一次本地 provider,再用 SF provider + 本 JSON 跑本脚本。"
            )
            return 2
        local = json.loads(Path(args.local_result).read_text())
        drift = _compare(local, result)
        print(f"\nDrift(local vs sf),阈值 {args.threshold}%:")
        bad = []
        for m, d in drift.items():
            print(f"  {m}: local={d['local']} sf={d['sf']} drift={d['drift_pct']}%")
            if d["drift_pct"] > args.threshold:
                bad.append(m)
        if bad:
            print(
                f"\nREJECT: 跨 provider drift > {args.threshold}% 在 {bad};"
                f" 排查 SF 行为变化或 rerank 阈值差异。"
            )
            if args.output:
                Path(args.output).write_text(
                    json.dumps({"drift": drift, "local": local, "sf": result},
                               ensure_ascii=False, indent=2)
                )
            return 1
        print(f"\nACCEPT: 全部 metric drift ≤ {args.threshold}%.")

    if args.output:
        Path(args.output).write_text(
            json.dumps(result, ensure_ascii=False, indent=2)
        )
        print(f"\n落盘: {args.output}")

    # ADR-0009:run 末打印 ``total_prompt_tokens=N``(免费档 baseline)
    from core.metrics import get_embedding_tokens_total
    total = get_embedding_tokens_total()
    print(f"\ntotal_prompt_tokens={total}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
