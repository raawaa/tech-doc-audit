"""Benchmark 执行引擎。

通过 monkey-patch 修改 vector_search.py 的模块级全局常量，
使单次搜索使用指定的参数配置，然后计算结果指标。
"""

import time
import yaml
from pathlib import Path
from typing import Optional

from benchmark.models import TestCase, BenchmarkConfig, BenchmarkRun, SingleResult
from benchmark.metrics import compute_single, aggregate

import services.vector_search as vs


def load_test_cases(path: str) -> list[TestCase]:
    """从 YAML 加载测试用例。"""
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return [TestCase(**tc) for tc in data.get("test_cases", [])]


class _ConfigPatch:
    """将 BenchmarkConfig 应用到 vector_search 模块的上下文管理器。"""

    def __init__(self, config: BenchmarkConfig):
        self.config = config
        self._saved = {}

    def __enter__(self):
        for attr, val in [
            ("MAX_CHARS", self.config.max_chars),
            ("OVERLAP", self.config.overlap),
            ("_SEARCH_SIMILARITY_THRESHOLD", self.config.similarity_threshold),
        ]:
            self._saved[attr] = getattr(vs, attr, None)
            setattr(vs, attr, val)
        return self

    def __exit__(self, *args):
        for attr, val in self._saved.items():
            if val is not None:
                setattr(vs, attr, val)


def run_benchmark(
    test_cases: list[TestCase],
    kb_ids: list[str],
    config: BenchmarkConfig,
) -> BenchmarkRun:
    """用指定配置跑一次完整 benchmark。"""
    start = time.time()
    per_case = []

    with _ConfigPatch(config):
        for tc in test_cases:
            results = vs.vec_search(kb_ids, tc.query, top_k=config.top_k)
            sr = compute_single(results, tc.id, tc.query, tc.expected_chunks, config)
            per_case.append(sr)

    elapsed = round(time.time() - start, 3)
    agg = aggregate(per_case)

    return BenchmarkRun(
        config=config,
        per_case=per_case,
        aggregate=agg,
        duration=elapsed,
        kb_ids=kb_ids,
    )
