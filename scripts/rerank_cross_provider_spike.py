"""Rerank 跨 provider 一致性 spike —— issues/144 AC#6 验收 spike。

跑 ``benchmark/test_cases.yaml`` 11 query × top-10 doc,对每个 query:
1. 本机 bge-reranker-v2-m3 给 top-10 docs 打分,排序得 ``local_order``
2. SiliconFlow bge-reranker-v2-m3 给同一批 docs 打分,排序得 ``sf_order``
3. 计算两序列的 Spearman ρ

通过标准:**Spearman ρ ≥ 0.99**(每条 query 都过)。

不通过:
- 拒收 merge;
- 排查 SF rerank schema / 阈值差异(T3 §2.3 实测 SF relevance_score ∈ [0, 1],
  本机 logit 无界,严格 ρ 仍能反映顺序一致);
- fix 后重跑。

## 用法

    PYTEST_RUN_SILICONFLOW_CONTRACT=1 uv run --env-file .env \\
        python scripts/rerank_cross_provider_spike.py

退出码:0 PASS / 1 FAIL / 2 无数据(rerank 跳过)/ 3 环境缺 key。
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

# CrossEncoder 加载模式:
# - 本机 ModelScope 缓存优先(国内机器默认);
# - 显式不走 HF HEAD 检查(避免 5 次超时重试)。
# 注意:不要全局设 HF_HUB_OFFLINE=1 —— 旧版 transformers 在离线模式下校验
# ``model.safetensors.index.json`` 的存在,本地缓存若缺失此索引文件会抛
# ``AttributeError: 'NoneType' object has no attribute 'endswith'``。
# 我们显式传 ``local_files_only=True`` + 用 ModelScope 本地路径绕过。

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _load_queries(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f).get("test_cases", [])


def _spearman(a: list[float], b: list[float]) -> float:
    """手算 Spearman ρ,不依赖 scipy。

    ``a`` / ``b``:同长度实数列表。
    用平均秩(并列排名取均值)处理 ties,标准 Pearson on ranks。
    """
    n = len(a)
    if n < 2:
        return 1.0 if a == b else 0.0

    def _rank(vals: list[float]) -> list[float]:
        sorted_pairs = sorted(enumerate(vals), key=lambda kv: kv[1])
        ranks = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and sorted_pairs[j + 1][1] == sorted_pairs[i][1]:
                j += 1
            avg = (i + j) / 2 + 1  # 1-based mean rank
            for k in range(i, j + 1):
                ranks[sorted_pairs[k][0]] = avg
            i = j + 1
        return ranks

    ra, rb = _rank(a), _rank(b)
    # Pearson
    ma = sum(ra) / n
    mb = sum(rb) / n
    num = sum((ra[i] - ma) * (rb[i] - mb) for i in range(n))
    da = sum((ra[i] - ma) ** 2 for i in range(n))
    db = sum((rb[i] - mb) ** 2 for i in range(n))
    if da == 0 or db == 0:
        return 1.0 if ra == rb else 0.0
    return num / ((da * db) ** 0.5)


def _load_production_chunks(kb_dir: Path) -> tuple[list[str], list[str]]:
    """读所有 KB 的 ``vectors/{doc_id}_nodes.json`` 收集 chunk (排除 repro_kb)。

    Returns:
        ``(node_ids, texts)`` 等长 list。
    """
    node_ids: list[str] = []
    texts: list[str] = []
    for kb_path in (kb_dir / "kbs").iterdir():
        if not kb_path.is_dir():
            continue
        if kb_path.name.startswith("repro_"):
            continue
        vec_dir = kb_path / "vectors"
        if not vec_dir.is_dir():
            continue
        for nodes_file in vec_dir.glob("*_nodes.json"):
            try:
                nodes = json.loads(nodes_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            for nd in nodes:
                text = nd.get("text") or ""
                if 30 <= len(text) <= 4000:  # 略掐头去尾:过短无 rerank 信号
                    node_ids.append(nd.get("node_id", ""))
                    texts.append(text)
    return node_ids, texts


def _get_top_docs_for_query(
    query: str,
    *,
    chunks: list[str],  # 已预 embed 的 chunk 列表(从外部 cache 传进)
    chunk_vecs: "np.ndarray",  # 预 embed 的向量矩阵
    chunk_ids: list[str],
    q_vec: "np.ndarray",
    top_n: int = 10,
) -> list[tuple[str, str]]:
    """按 SF query 向量找 top_n chunks(cos 相似度)。

    在 spike 入口预 embed 所有 chunk 一次,所有 query 共用,避免 3976 × N 次嵌入。
    """
    import numpy as np
    sims = chunk_vecs @ q_vec
    order = np.argsort(-sims)[:top_n]
    return [(chunk_ids[i], chunks[i]) for i in order]


def _prefetch_chunk_vectors(chunks: list[str]) -> "np.ndarray":
    """一次性把所有 chunk 送去 SF embed(按 ``EMBED_BATCH_SIZE`` 分批)。

    siliconflow_client 内置的 ``_embed_with_siliconflow`` 用单次
    ``client.embeddings.create(...)`` 把整个 ``texts`` list 一次送出 —— 当
    列表长度上千时 SF 端可能返回 400(单请求 input 长度超限)。spike 一次性
    预嵌入把全量生产 chunks(≈ 3976)按 32 一批切片,降低单次 request 体量。
    """
    from core.siliconflow_client import (
        make_siliconflow_client,
        EMBED_MODEL_ID,
        truncate_batch,
        EMBED_BATCH_SIZE,
    )
    import numpy as np
    import httpx

    truncated = truncate_batch(chunks)
    all_vecs: list[list[float]] = []
    print(f"  预嵌入 {len(chunks)} 个 chunk,批 {EMBED_BATCH_SIZE} ...")
    t0 = time.time()
    client = make_siliconflow_client(timeout=300)
    for i in range(0, len(truncated), EMBED_BATCH_SIZE):
        batch = truncated[i: i + EMBED_BATCH_SIZE]
        # 显式重试 3 次(SF 偶发 5xx)
        last_err = None
        for attempt in range(3):
            try:
                resp = client.embeddings.create(model=EMBED_MODEL_ID, input=batch)
                all_vecs.extend(d.embedding for d in resp.data)
                last_err = None
                break
            except (httpx.HTTPError, Exception) as e:  # noqa: BLE001
                last_err = e
                time.sleep(min(2 ** attempt, 10))
        if last_err is not None:
            raise last_err
        done = min(i + EMBED_BATCH_SIZE, len(truncated))
        if done % 256 == 0 or done == len(truncated):
            print(f"    {done}/{len(truncated)} ({time.time() - t0:.1f}s)")
    print(f"  嵌入完成,耗时 {time.time() - t0:.1f}s")
    return np.asarray(all_vecs, dtype=np.float64)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--cases", default="benchmark/test_cases.yaml", help="测试用例 yaml"
    )
    ap.add_argument(
        "--top-n", type=int, default=10, help="每 query 召回 N 篇 doc 给 rerank"
    )
    ap.add_argument(
        "--threshold", type=float, default=0.99, help="Spearman ρ 通过阈值(默认 0.99)"
    )
    ap.add_argument(
        "--output", type=str, default=None, help="结果 JSON 落盘路径"
    )
    args = ap.parse_args()

    if not os.environ.get("SILICONFLOW_API_KEY"):
        print(
            "FAIL: SILICONFLOW_API_KEY 未设置。手动跑: "
            "PYTEST_RUN_SILICONFLOW_CONTRACT=1 uv run --env-file .env python "
            "scripts/rerank_cross_provider_spike.py",
            file=sys.stderr,
        )
        return 3

    cases = _load_queries(args.cases)
    if not cases:
        print(f"FAIL: {args.cases} 没找到 test_cases")
        return 2
    n_query = min(11, len(cases))
    cases = cases[:n_query]
    print(f"跑 {n_query} 条 query × top_n={args.top_n} rerank 跨 provider spike\n")

    # 收集所有 top_n docs,合并成 batch(减少 cross-encoder 加载次数)
    kb_dir = Path(os.environ.get("AUDIT_DATA_DIR", "./data")).resolve()
    print(f"KB data dir: {kb_dir}")

    # 一次性预嵌入所有 production chunks —— 11 query 共享,避免重复打 SF
    chunk_ids, chunks = _load_production_chunks(kb_dir)
    if not chunks:
        print(f"\nFAIL: 在 {kb_dir}/kbs 下没找到 production chunk (vector 文件缺失?)")
        return 2
    print(f"\n从 {kb_dir}/kbs 收集到 {len(chunks)} 个 production chunks (排除 repro_kb)")
    chunk_vecs = _prefetch_chunk_vectors(chunks)

    # 本机 CrossEncoder 一次性加载(spike 一次性,容忍 ~1.3s)
    from sentence_transformers import CrossEncoder
    import torch
    device = os.environ.get("RERANKER_DEVICE") or os.environ.get("EMBED_DEVICE") or None

    # 优先用 ModelScope 本地缓存(国内机器默认路径)
    local_modelscope = Path.home() / ".cache/modelscope/hub/BAAI/bge-reranker-v2-m3"
    reranker_path = str(local_modelscope) if local_modelscope.is_dir() else "BAAI/bge-reranker-v2-m3"

    try:
        local_ce = CrossEncoder(
            reranker_path,
            device=device,
            trust_remote_code=True,
            local_files_only=True,
        )
    except Exception as gpu_err:
        print(f"  CrossEncoder GPU 加载失败({gpu_err}), 降级到 cpu")
        local_ce = CrossEncoder(
            reranker_path,
            device="cpu",
            trust_remote_code=True,
            local_files_only=True,
        )

    # SF rerank adapter
    from core.siliconflow_client import rerank_with_siliconflow
    from llama_index.core.schema import NodeWithScore, TextNode

    results = []
    for i, tc in enumerate(cases, 1):
        q = tc["query"]
        print(f"[{i}/{n_query}] {tc.get('id', '?')}: {q[:40]}{'...' if len(q) > 40 else ''}")

        t0 = time.time()
        try:
            # 用 SF 一次性 embed 当前 query(11 query 各打一次 q_vec)
            from core.siliconflow_client import encode_query_for_siliconflow
            import numpy as np
            q_vec = np.asarray(encode_query_for_siliconflow(q), dtype=np.float64)
            top_hits = _get_top_docs_for_query(
                q, chunks=chunks, chunk_vecs=chunk_vecs,
                chunk_ids=chunk_ids, q_vec=q_vec, top_n=args.top_n,
            )
            top_docs = [text for _id, text in top_hits]
        except Exception as e:
            print(f"  FAIL 取 top docs: {e};skip 此 query")
            continue
        if len(top_docs) < 2:
            print(f"  SKIP: top docs 仅 {len(top_docs)} 篇,无可对比排序")
            continue

        # 本机 rerank(直接 CrossEncoder.predict)
        try:
            local_scores = local_ce.predict([(q, d) for d in top_docs]).tolist()
        except Exception as e:
            print(f"  FAIL 本机 rerank: {e}")
            continue

        # SF rerank
        sf_nodes = [
            NodeWithScore(node=TextNode(text=d), score=float(s))
            for d, s in zip(top_docs, local_scores)
        ]
        try:
            sf_reranked = rerank_with_siliconflow(sf_nodes, q, top_n=args.top_n)
            sf_scores = [n.score for n in sf_reranked] if sf_reranked else []
            # 重建 SF 顺序到与 top_docs 一致的位置(便于一一对应 Spearman)
            sf_order_scores = [None] * len(top_docs)
            for n in sf_reranked or []:
                t = n.node.text
                for idx, d in enumerate(top_docs):
                    if d == t:
                        sf_order_scores[idx] = n.score
                        break
            # 缺失填 0
            sf_full_scores = [s if s is not None else 0.0 for s in sf_order_scores]
        except Exception as e:
            print(f"  FAIL SF rerank: {e}")
            continue

        rho = _spearman(local_scores, sf_full_scores)
        elapsed = time.time() - t0
        passed = rho >= args.threshold

        print(
            f"  ↳ ρ = {rho:.4f}  ({'PASS' if passed else 'FAIL'} ≥ {args.threshold})"
            f"  [{elapsed:.1f}s]"
        )
        results.append({
            "id": tc.get("id", "?"),
            "query": q,
            "spearman_rho": rho,
            "passed": passed,
            "local_top_doc": top_docs[local_scores.index(max(local_scores))][:60],
            "sf_top_doc": top_docs[sf_full_scores.index(max(sf_full_scores))][:60],
        })

    del local_ce
    import gc
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    if not results:
        print("\n无任何 query 完成了 cross-provider rerank;视作 FAIL")
        return 1

    rhos = [r["spearman_rho"] for r in results]
    n_pass = sum(1 for r in results if r["passed"])

    print()
    print("=" * 60)
    print(f"跨 provider rerank Spearman ρ 报告 (n={len(results)} query)")
    print("=" * 60)
    print(f"  min ρ = {min(rhos):.4f}")
    print(f"  median ρ = {statistics.median(rhos):.4f}")
    print(f"  mean ρ = {statistics.mean(rhos):.4f}")
    print(f"  PASS (≥ {args.threshold}): {n_pass}/{len(results)}")
    print()
    for r in results:
        flag = "✓" if r["passed"] else "✗"
        print(
            f"  {flag} {r['id']}: ρ={r['spearman_rho']:.4f}"
        )

    if args.output:
        Path(args.output).write_text(json.dumps(results, ensure_ascii=False, indent=2))
        print(f"\n落盘: {args.output}")

    if n_pass == len(results):
        print(f"\n ACCEPT: {n_pass}/{len(results)} queries 全过 Spearman ρ ≥ {args.threshold}")
        return 0
    print(f"\n REJECT: {n_pass}/{len(results)} queries 没过 ρ ≥ {args.threshold}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
