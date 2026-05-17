"""参数扫描。

参数分两组：
- Index 参数（chunk_size, overlap）→ 变化需要重建向量索引，结果缓存到 benchmark/_cache/
- Search 参数（threshold, top_k）→ 仅影响搜索行为，不重建索引
"""

import itertools
import shutil
import json
import time
from pathlib import Path
from typing import Optional

from benchmark.models import BenchmarkConfig, BenchmarkRun
from benchmark.runner import run_benchmark, load_test_cases
import services.vector_search as vs

CACHE_DIR = Path(__file__).parent / "_cache"
DATA_DIR = Path(__file__).parent.parent / "data"


def _ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)


def _vec_dir(kb_id: str) -> Path:
    return DATA_DIR / "kbs" / kb_id / "vectors"


def _cache_key(kb_id: str, chunk_size: int, overlap: int) -> Path:
    return CACHE_DIR / kb_id / f"{chunk_size}_{overlap}"


def _save_vectors_to_cache(kb_id: str, chunk_size: int, overlap: int):
    """将当前向量索引保存到缓存。"""
    src = _vec_dir(kb_id)
    if not src.exists():
        return
    dst = _cache_key(kb_id, chunk_size, overlap)
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def _restore_vectors_from_cache(kb_id: str, chunk_size: int, overlap: int) -> bool:
    """从缓存恢复向量索引。"""
    dst = _vec_dir(kb_id)
    src = _cache_key(kb_id, chunk_size, overlap)
    if not src.exists():
        return False
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    return True


def _backup_original(kb_id: str):
    """备份当前向量索引。"""
    src = _vec_dir(kb_id)
    if not src.exists():
        return
    dst = CACHE_DIR / kb_id / "_original"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def _restore_original(kb_id: str):
    """恢复原始向量索引。"""
    dst = _vec_dir(kb_id)
    src = CACHE_DIR / kb_id / "_original"
    if not src.exists():
        return
    if dst.exists():
        shutil.rmtree(dst)
    if src.exists():
        shutil.copytree(src, dst)


def sweep(
    kb_ids: list[str],
    test_cases_path: str,
    chunk_sizes: list[int] = None,
    overlaps: list[int] = None,
    thresholds: list[float] = None,
    top_ks: list[int] = None,
    acceptance_thresholds: list[float] = None,
) -> list[BenchmarkRun]:
    """参数扫描入口。"""
    if chunk_sizes is None:
        chunk_sizes = [256, 512, 768]
    if overlaps is None:
        overlaps = [64, 128, 192]
    if thresholds is None:
        thresholds = [0.1, 0.2, 0.3]
    if top_ks is None:
        top_ks = [3, 5]
    if acceptance_thresholds is None:
        acceptance_thresholds = [0.35]
    _ensure_dir(CACHE_DIR)

    test_cases = load_test_cases(test_cases_path)
    index_params = list(itertools.product(chunk_sizes, overlaps))
    search_params = list(itertools.product(thresholds, top_ks, acceptance_thresholds))

    # 备份原始索引
    for kb_id in kb_ids:
        _backup_original(kb_id)

    runs = []
    start = time.time()
    for chunk_size, overlap in index_params:
        # 重建或恢复索引
        for kb_id in kb_ids:
            if not _restore_vectors_from_cache(kb_id, chunk_size, overlap):
                print(f"  [index] 重建 {kb_id} chunk={chunk_size} overlap={overlap} ...")
                vs.MAX_CHARS = chunk_size
                vs.OVERLAP = overlap
                vs.rebuild_kb_index(kb_id)
                _save_vectors_to_cache(kb_id, chunk_size, overlap)

        # 对该索引下的所有 search 参数组合跑 benchmark
        for threshold, top_k, accept_thresh in search_params:
            config = BenchmarkConfig(
                max_chars=chunk_size,
                overlap=overlap,
                similarity_threshold=threshold,
                top_k=top_k,
                acceptance_threshold=accept_thresh,
            )
            run = run_benchmark(test_cases, kb_ids, config)
            runs.append(run)
            print(f"    {chunk_size:>4}/{overlap:<4} "
                  f"thr={threshold:.1f} k={top_k} "
                  f"MRR={run.aggregate.mrr:.4f} "
                  f"P={run.aggregate.mean_precision:.4f} "
                  f"R={run.aggregate.mean_recall:.4f}")

    # 恢复原始索引
    for kb_id in kb_ids:
        _restore_original(kb_id)

    elapsed = time.time() - start
    print(f"\n扫描完成: {len(runs)} 个配置, 耗时 {elapsed:.1f}s")
    return runs
