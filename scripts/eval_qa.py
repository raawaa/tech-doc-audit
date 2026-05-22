"""RAG 评估 — 检索质量 + 答案质量。

使用 LlamaIndex 内置评估器（Faithfulness / Relevancy），
配合 keyword-based 检索指标（HitRate / MRR）。

用法：
  uv run python scripts/eval_qa.py --kb-ids <ID1,ID2>
  uv run python scripts/eval_qa.py --kb-ids <ID1,ID2> --cases benchmark/test_cases.yaml
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 加载 .env（DeepSeek API key 等配置）
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import yaml
from typing import Any

from core.settings import get_llm
from llama_index.core.llms import ChatMessage, MessageRole


# ── 加载测试用例 ──────────────────────────────────────────────────────────────

def load_test_cases(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("test_cases", [])


# ── 检索指标（keyword-based）──────────────────────────────────────────────────

def _chunk_matches(content: str, expected: dict) -> bool:
    """判定 chunk 内容是否命中预期的关键词。"""
    keywords = expected.get("content_keywords", [])
    hits = sum(1 for kw in keywords if kw in content)
    if hits == len(keywords):
        return True
    if len(keywords) > 1 and hits >= max(1, int(len(keywords) * 0.7)):
        return True
    return False


def compute_retrieval_metrics(results: list[dict], expected_chunks: list[dict]) -> dict:
    """计算检索指标。"""
    # HitRate: 是否有至少一个结果命中了预期内容
    hit = any(
        any(_chunk_matches(r.get("content", ""), e) for e in expected_chunks)
        for r in results
    )
    # MRR: 第一个命中结果的排名倒数
    mrr = 0.0
    for i, r in enumerate(results, 1):
        if any(_chunk_matches(r.get("content", ""), e) for e in expected_chunks):
            mrr = 1.0 / i
            break
    # Recall: 预期 chunks 中被命中的比例
    found = sum(
        1 for e in expected_chunks
        if any(_chunk_matches(r.get("content", ""), e) for r in results)
    )
    recall = found / len(expected_chunks) if expected_chunks else 0
    # Precision@K: top-K 结果中命中的比例
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


# ── 答案质量评估（LlamaIndex evaluators）────────────────────────────────────

def evaluate_answer_quality(query: str, answer: str, context: str) -> dict:
    """用 LlamaIndex 评估器检查答案质量。"""
    from llama_index.core.evaluation import FaithfulnessEvaluator, RelevancyEvaluator

    llm = get_llm()

    faith_eval = FaithfulnessEvaluator(llm=llm)
    relev_eval = RelevancyEvaluator(llm=llm)

    # Faithfulness: 答案是否忠于提供的上下文（防幻觉）
    try:
        faith_result = faith_eval.evaluate(
            query=query, response=answer, contexts=[context]
        )
        faithful = faith_result.passing if hasattr(faith_result, 'passing') else faith_result.score > 0.5
    except Exception:
        faithful = None

    # Relevancy: 上下文是否与问题相关
    try:
        relev_result = relev_eval.evaluate(
            query=query, response=answer, contexts=[context]
        )
        relevant = relev_result.passing if hasattr(relev_result, 'passing') else relev_result.score > 0.5
    except Exception:
        relevant = None

    return {
        "faithfulness": bool(faithful),
        "relevancy": bool(relevant),
    }


# ── 主流程 ────────────────────────────────────────────────────────────────────

def run_eval(kb_ids: list[str], cases_path: str):
    from services.vector_search import search as vec_search
    from services.qa_service import ask as qa_ask

    cases = load_test_cases(cases_path)
    if not cases:
        print("没有找到测试用例")
        return

    print(f"\n{'='*70}")
    print(f"RAG 评估报告")
    print(f"知识库: {', '.join(kb_ids)}")
    print(f"测试用例: {len(cases)}")
    print(f"{'='*70}\n")

    all_retrieval = {"hit_rate": [], "mrr": [], "recall": [], "precision_at_k": []}
    all_generation = {"faithfulness": [], "relevancy": []}
    details = []

    for i, tc in enumerate(cases, 1):
        q = tc["query"]
        expected = tc.get("expected_chunks", [])

        print(f"[{i}/{len(cases)}] {tc.get('id', '?')}: {q}")

        # 1. 检索
        t0 = time.time()
        results = vec_search(kb_ids, q, max_results=5)
        t1 = time.time()

        # 2. 检索指标
        rm = compute_retrieval_metrics(results, expected)
        for k, v in rm.items():
            all_retrieval[k].append(v)

        # 3. Q&A
        qa_result = qa_ask(kb_ids, q, top_k=5)
        answer = qa_result.get("answer", "")
        sources_text = "\n".join(
            s.get("content_snippet", "") for s in qa_result.get("sources", [])
        )

        # 4. 答案质量评估
        eq = {"faithfulness": None, "relevancy": None, "error": None}
        if answer and sources_text:
            try:
                eq = evaluate_answer_quality(q, answer, sources_text)
                for k, v in eq.items():
                    if v is not None and k in all_generation:
                        all_generation[k].append(v)
            except Exception as e:
                eq["error"] = str(e)
                print(f"  评估失败: {e}")

        # 汇总
        status = f"H={rm['hit_rate']:.0f} R={rm['recall']:.2f}"
        if eq.get("faithfulness") is not None:
            status += f" F={'Y' if eq['faithfulness'] else 'N'} Rlv={'Y' if eq['relevancy'] else 'N'}"
        print(f"  ↳ {status} ({t1-t0:.1f}s)")

        details.append({
            "id": tc.get("id", ""),
            "query": q,
            "retrieval": rm,
            "generation": eq,
            "answer_preview": answer[:200] if answer else "",
            "latency": round(t1 - t0, 2),
        })

    # ── 汇总报告 ──────────────────────────────────────────────────────────────

    n = len(cases)
    print(f"\n{'='*70}")
    print(f"汇总")
    print(f"{'='*70}")

    if n > 0:
        print(f"\n■ 检索质量")
        print(f"  HitRate (avg):  {sum(all_retrieval['hit_rate'])/n:.3f}")
        print(f"  MRR (avg):      {sum(all_retrieval['mrr'])/n:.3f}")
        print(f"  Recall (avg):   {sum(all_retrieval['recall'])/n:.3f}")
        print(f"  Precision@K:    {sum(all_retrieval['precision_at_k'])/n:.3f}")

        gen_n = len([v for v in all_generation["faithfulness"] if v is not None])
        if gen_n > 0:
            print(f"\n■ 答案质量（LlamaIndex 评估）")
            f_val = sum(1 for v in all_generation["faithfulness"] if v) / gen_n
            r_val = sum(1 for v in all_generation["relevancy"] if v) / gen_n
            print(f"  Faithfulness:   {f_val:.0%} (答案是否忠于原文)")
            print(f"  Relevancy:      {r_val:.0%} (上下文是否相关)")
            print(f"  评估样本数:     {gen_n}/{n}")

    # 逐条详情
    print(f"\n■ 逐条详情")
    for d in details:
        rid = d["id"]
        rq = d["query"]
        print(f"  {rid}: {rq}")
        print(f"    H={d['retrieval']['hit_rate']:.0f} "
              f"MRR={d['retrieval']['mrr']:.3f} "
              f"R={d['retrieval']['recall']:.2f} "
              f"P={d['retrieval']['precision_at_k']:.2f}")
        if d["generation"].get("faithfulness") is not None:
            print(f"    Faithfulness={'✓' if d['generation']['faithfulness'] else '✗'} "
                  f"Relevancy={'✓' if d['generation']['relevancy'] else '✗'}")
        if d.get("error"):
            print(f"    Error: {d['error']}")
        if d.get("answer_preview"):
            print(f"    Answer: {d['answer_preview'][:100]}...")

    print(f"\n{'='*70}")
    print(f"评估完成")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RAG 评估")
    parser.add_argument("--kb-ids", required=True, help="知识库 ID（逗号分隔）")
    parser.add_argument("--cases", default="benchmark/test_cases.yaml", help="测试用例路径")
    args = parser.parse_args()

    kb_id_list = [k.strip() for k in args.kb_ids.split(",") if k.strip()]
    run_eval(kb_id_list, args.cases)
