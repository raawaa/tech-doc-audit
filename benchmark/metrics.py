"""纯函数：匹配判定 + 指标计算。"""

from benchmark.models import ExpectedChunk, SingleResult, AggregateMetrics, BenchmarkConfig


def _chunk_matches(content: str, expected: ExpectedChunk) -> bool:
    """判定搜索结果内容是否命中预期 chunk。

    策略：
    - Level 1：所有 content_keywords 都出现在 content 中
    - Level 2：至少 70% 的 content_keywords 命中（当 Level 1 不满足时）
    """
    hits = sum(1 for kw in expected.content_keywords if kw in content)
    if hits == len(expected.content_keywords):
        return True
    if len(expected.content_keywords) > 1 and hits >= max(1, int(len(expected.content_keywords) * 0.7)):
        return True
    return False


def precision_at_k(results: list[dict], expected_chunks: list[ExpectedChunk], k: int) -> float:
    """Top-K 结果中有多少比例命中了至少一个预期 chunk。"""
    if not results or k == 0:
        return 0.0
    top_k = results[:k]
    hits = sum(1 for r in top_k if any(_chunk_matches(r.get("content", ""), e) for e in expected_chunks))
    return hits / len(top_k)


def recall(results: list[dict], expected_chunks: list[ExpectedChunk]) -> float:
    """预期 chunks 中有多少被至少一个结果命中。"""
    if not expected_chunks:
        return 0.0
    found = 0
    for e in expected_chunks:
        if any(_chunk_matches(r.get("content", ""), e) for r in results):
            found += 1
    return found / len(expected_chunks)


def reciprocal_rank(results: list[dict], expected_chunks: list[ExpectedChunk]) -> float:
    """第一个命中结果的倒数排名。0 = 没有命中。"""
    for i, r in enumerate(results, 1):
        if any(_chunk_matches(r.get("content", ""), e) for e in expected_chunks):
            return 1.0 / i
    return 0.0


def compute_single(results: list[dict], tc_id: str, query: str, expected_chunks: list[ExpectedChunk],
                   config: BenchmarkConfig) -> SingleResult:
    """计算单个测试用例的全部指标。"""
    p = precision_at_k(results, expected_chunks, config.top_k)
    r = recall(results, expected_chunks)
    rr = reciprocal_rank(results, expected_chunks)

    matched = sum(1 for e in expected_chunks
                  if any(_chunk_matches(res.get("content", ""), e) for res in results))
    missed = [e for e in expected_chunks
              if not any(_chunk_matches(res.get("content", ""), e) for res in results)]
    detail_parts = []
    if matched:
        detail_parts.append(f"{matched}/{len(expected_chunks)} 匹配")
    if missed:
        kws = [",".join(m.content_keywords) for m in missed]
        detail_parts.append(f"未命中: {'; '.join(kws)}")

    return SingleResult(
        test_id=tc_id,
        query=query,
        precision_at_k=round(p, 4),
        recall=round(r, 4),
        reciprocal_rank=round(rr, 4),
        num_results_returned=len(results),
        num_expected=len(expected_chunks),
        matched=matched,
        details="; ".join(detail_parts),
    )


def aggregate(results: list[SingleResult]) -> AggregateMetrics:
    """聚合全部测试用例的指标。"""
    if not results:
        return AggregateMetrics()
    total = len(results)
    avg_p = sum(r.precision_at_k for r in results) / total
    avg_r = sum(r.recall for r in results) / total
    avg_rr = sum(r.reciprocal_rank for r in results) / total
    with_match = sum(1 for r in results if r.reciprocal_rank > 0)

    return AggregateMetrics(
        mean_precision=round(avg_p, 4),
        mean_recall=round(avg_r, 4),
        mrr=round(avg_rr, 4),
        total_cases=total,
        cases_with_match=with_match,
    )
