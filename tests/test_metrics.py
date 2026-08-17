"""``core.metrics`` thread-local token 计数器契约测试(ADR-0009)。

不依赖网络 / 数据库 / 真实 SF;只验模块级接口的语义:
1. ``record_embedding_tokens`` 累加正数 → ``get_embedding_tokens_total`` 返回累加值。
2. 多次累加按加法合并。
3. ``reset_embedding_tokens_total`` 清零。
4. 跨线程累加互相隔离(thread-local 语义)。
5. 负数与零值:实现按加法处理(零加 = 无变化;负数实现层接住但**不**抛,
   因为 ``usage`` 可能动态变化,稳健起见不当硬错误)。
"""
from __future__ import annotations

import threading

import pytest

from core import metrics


@pytest.fixture(autouse=True)
def _reset_metrics():
    """每条用例前清零,防止跨用例污染(thread-local 在测试间复用同一线程)。"""
    metrics.reset_embedding_tokens_total()
    yield
    metrics.reset_embedding_tokens_total()


def test_initial_total_is_zero():
    """首次读取时累加器为 0(从未累加过的语义)。"""
    assert metrics.get_embedding_tokens_total() == 0


def test_record_addes_in_value():
    """``record_embedding_tokens(123)`` 后读 = 123。"""
    metrics.record_embedding_tokens(123)
    assert metrics.get_embedding_tokens_total() == 123


def test_multiple_records_accumulate():
    """多次累加按加法合并(同线程)。"""
    metrics.record_embedding_tokens(10)
    metrics.record_embedding_tokens(20)
    metrics.record_embedding_tokens(5)
    assert metrics.get_embedding_tokens_total() == 35


def test_reset_clears_to_zero():
    """``reset_embedding_tokens_total`` 后读 = 0;之后再累加重新计数。"""
    metrics.record_embedding_tokens(50)
    assert metrics.get_embedding_tokens_total() == 50
    metrics.reset_embedding_tokens_total()
    assert metrics.get_embedding_tokens_total() == 0
    metrics.record_embedding_tokens(7)
    assert metrics.get_embedding_tokens_total() == 7


def test_thread_local_isolation():
    """两个线程各自累加,互不污染(thread-local 契约)。"""
    # 主线程累加
    metrics.record_embedding_tokens(100)

    other_thread_total = []

    def _worker():
        # 子线程:累加 7 后把值记下来
        metrics.record_embedding_tokens(7)
        other_thread_total.append(metrics.get_embedding_tokens_total())

    t = threading.Thread(target=_worker)
    t.start()
    t.join()

    # 子线程看到自己的 7,主线程仍为 100
    assert other_thread_total == [7]
    assert metrics.get_embedding_tokens_total() == 100


def test_record_zero_is_noop():
    """``record_embedding_tokens(0)`` 不抛,且不影响累加值。"""
    metrics.record_embedding_tokens(42)
    metrics.record_embedding_tokens(0)
    assert metrics.get_embedding_tokens_total() == 42