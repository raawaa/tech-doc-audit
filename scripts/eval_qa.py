"""RAG 评估 — LlamaIndex 最佳实践。

用法：
  uv run python scripts/eval_qa.py --kb-ids <ID1,ID2>
  uv run python scripts/eval_qa.py --kb-ids <ID1,ID2> --cases benchmark/test_cases.yaml

评估维度：
- 检索质量：HitRate / MRR / Recall / Precision@K（keyword-based）
- 答案质量：Faithfulness（是否忠于原文）+ Relevancy（上下文是否相关）
  使用 BatchEvalRunner 并行评估
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml
from typing import Any

from core.settings import get_embed_model, get_llm


# ── 加载测试用例 ──────────────────────────────────────────────────────────────

def load_test_cases(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("test_cases", [])


# ── 检索指标（keyword-based）──────────────────────────────────────────────────

def _chunk_matches(content: str, expected: dict) -> bool:
    keywords = expected.get("content_keywords", [])
    hits = sum(1 for kw in keywords if kw in content)
    if hits == len(keywords):
        return True
    if len(keywords) > 1 and hits >= max(1, int(len(keywords) * 0.7)):
        return True
    return False


def compute_retrieval_metrics(results: list[dict], expected_chunks: list[dict]) -> dict:
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


# ── 答案质量评估（BatchEvalRunner）────────────────────────────────────────────

def evaluate_batch(
    queries: list[str],
    responses: list[str],
    contexts_list: list[list[str]],
) -> dict[str, list[dict]]:
    """用 BatchEvalRunner 并行评估 Faithfulness + Relevancy。"""
    from llama_index.core.evaluation import (
        FaithfulnessEvaluator,
        RelevancyEvaluator,
        BatchEvalRunner,
    )

    llm = get_llm()
    runner = BatchEvalRunner(
        evaluators={
            "faithfulness": FaithfulnessEvaluator(llm=llm),
            "relevancy": RelevancyEvaluator(llm=llm),
        },
        workers=2,
        show_progress=True,
    )

    results = runner.evaluate_response_strs(
        queries=queries,
        response_strs=responses,
        contexts_list=contexts_list,
    )

    output = {}
    for eval_name, eval_results in results.items():
        output[eval_name] = [
            {
                "passing": r.passing,
                "score": r.score,
                "feedback": r.feedback,
            }
            for r in eval_results
        ]
    return output


# ── 主流程 ────────────────────────────────────────────────────────────────────

def run_eval(kb_ids: list[str], cases_path: str):
    # 确保 Embed 和 LLM 已初始化
    get_embed_model()
    get_llm()

    from services.vector_search import search as vec_search
    from services.qa_service import ask as qa_ask

    cases = load_test_cases(cases_path)
    if not cases:
        print("没有找到测试用例")
        return

    print(f"\n{'=' * 70}")
    print(f"RAG 评估报告")
    print(f"知识库: {', '.join(kb_ids)}")
    print(f"测试用例: {len(cases)}")
    print(f"{'=' * 70}\n")

    all_retrieval = {"hit_rate": [], "mrr": [], "recall": [], "precision_at_k": []}

    # 收集生成评估数据
    eval_queries = []
    eval_responses = []
    eval_contexts = []
    eval_details = []

    for i, tc in enumerate(cases, 1):
        q = tc["query"]
        expected = tc.get("expected_chunks", [])

        print(f"[{i}/{len(cases)}] {tc.get('id', '?')}: {q}")

        # 1. 检索 + 检索指标
        t0 = time.time()
        results = vec_search(kb_ids, q, max_results=5)
        t1 = time.time()
        rm = compute_retrieval_metrics(results, expected)
        for k, v in rm.items():
            all_retrieval[k].append(v)

        # 2. Q&A（使用 QueryEngine）
        qa_result = qa_ask(kb_ids, q, top_k=5)
        answer = qa_result.get("answer", "")
        sources = qa_result.get("sources", [])

        # 3. 收集评估数据（完整 context，不截断）
        if answer and sources:
            eval_queries.append(q)
            eval_responses.append(answer)
            # 传完整的 chunk 文本（最多 5000 字）
            full_contexts = [s.get("content_snippet", "") for s in sources]
            # 从 sources 没有完整文本，需要从检索结果取
            full_texts = []
            for s_result in results:
                full_texts.append(s_result.get("content", "")[:5000])
            eval_contexts.append(full_texts if full_texts else full_contexts)
            eval_details.append({
                "id": tc.get("id", ""),
                "query": q,
                "retrieval": rm,
                "answer_preview": answer[:200],
            })
        else:
            eval_details.append({
                "id": tc.get("id", ""),
                "query": q,
                "retrieval": rm,
                "answer_preview": answer[:200],
            })

        print(f"  ↳ H={rm['hit_rate']:.0f} R={rm['recall']:.2f} ({t1 - t0:.1f}s)")

    # 4. 生成评估
    gen_results = {}
    if eval_queries:
        print(f"\n  Running BatchEvalRunner ({len(eval_queries)} cases)...")
        try:
            gen_results = evaluate_batch(eval_queries, eval_responses, eval_contexts)
        except Exception as e:
            print(f"  生成评估失败: {e}")

    # ── 汇总报告 ────────────────────────────────────────────────────────────
    n = len(cases)
    print(f"\n{'=' * 70}")
    print(f"汇总")
    print(f"{'=' * 70}")

    if n > 0:
        print(f"\n■ 检索质量")
        print(f"  HitRate (avg):  {sum(all_retrieval['hit_rate']) / n:.3f}")
        print(f"  MRR (avg):      {sum(all_retrieval['mrr']) / n:.3f}")
        print(f"  Recall (avg):   {sum(all_retrieval['recall']) / n:.3f}")
        print(f"  Precision@K:    {sum(all_retrieval['precision_at_k']) / n:.3f}")

        if gen_results:
            print(f"\n■ 答案质量（BatchEvalRunner）")
            for eval_name in ("faithfulness", "relevancy"):
                scores = [r["passing"] for r in gen_results.get(eval_name, []) if r["passing"] is not None]
                if scores:
                    print(f"  {eval_name.capitalize()}:     {sum(scores) / len(scores):.0%}")
            eval_n = len(gen_results.get("faithfulness", []))
            print(f"  评估样本数:     {eval_n}/{n}")

    # 逐条详情
    print(f"\n■ 逐条详情")
    for d in eval_details:
        rid = d["id"]
        rq = d["query"]
        rm = d["retrieval"]
        print(f"  {rid}: {rq}")
        print(f"    H={rm['hit_rate']:.0f} MRR={rm['mrr']:.3f} R={rm['recall']:.2f} P={rm['precision_at_k']:.2f}")
        ans = d.get("answer_preview", "")
        if ans:
            print(f"    Answer: {ans[:100]}...")
        # 从 gen_results 查评分
        if gen_results:
            for eval_name in ("faithfulness", "relevancy"):
                idx = eval_details.index(d)
                if idx < len(gen_results.get(eval_name, [])):
                    r = gen_results[eval_name][idx]
                    if r["score"] is not None:
                        print(f"    {eval_name}: {'✓' if r['passing'] else '✗'} ({r['score']})")

    print(f"\n{'=' * 70}")
    print(f"评估完成")
    print(f"{'=' * 70}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RAG 评估")
    parser.add_argument("--kb-ids", required=True, help="知识库 ID（逗号分隔）")
    parser.add_argument("--cases", default="benchmark/test_cases.yaml", help="测试用例路径")
    args = parser.parse_args()

    kb_id_list = [k.strip() for k in args.kb_ids.split(",") if k.strip()]
    run_eval(kb_id_list, args.cases)
