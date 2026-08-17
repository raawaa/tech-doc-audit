"""线程本地用量计数器（issues/145 / ADR-0009）。

设计上镜像 ``core.degradation`` 的 thread-local 模式：每个线程独立累加
embedding 调用返回的 prompt_tokens；外部测试 / ``scripts/eval_qa_drift.py``
在 run 末调用 ``get_embedding_tokens_total()`` 读当前线程累加值，并按
``reset_embedding_tokens_total()`` 清零。

## 接口契约

```python
record_embedding_tokens(n: int) -> None
get_embedding_tokens_total() -> int
reset_embedding_tokens_total() -> None
```

调用点 ``_embed_with_siliconflow``：在 ``client.embeddings.create(...)`` 返回后
读 ``resp.usage.prompt_tokens``,调 ``record_embedding_tokens(n)``。失败 /
``usage`` 为 ``None`` 静默跳过(不影响主路径)。

为什么 thread-local：跨 KB / 跨线程的批量建库可能并行(SiliconFlow 主路径
本身串行,但 ``bulk_reparse_service`` 多线程并发调 ``reparse_document`` →
``index_document`` → ``_embed_with_siliconflow``),不同 worker 不该把 token
累加到一起。每个线程维护自己的累加,run 末由编排层自行汇总(如果需要)。
"""

from __future__ import annotations

import threading


_thread_local = threading.local()


def _ensure_counter():
    if not hasattr(_thread_local, "embedding_tokens"):
        _thread_local.embedding_tokens = 0


def record_embedding_tokens(n: int) -> None:
    """把 ``n`` 加到当前线程的 embedding prompt_tokens 累加器上。

    非负整数校验交给调用方;``n <= 0``(如服务端返回 ``usage=None`` 或 0)
    也允许调,实现按加法处理(零加 = 无变化)。
    """
    _ensure_counter()
    _thread_local.embedding_tokens += int(n)


def get_embedding_tokens_total() -> int:
    """读取当前线程 embedding prompt_tokens 累加值。

    返回 ``int``;从未累加过时返回 ``0``(保证幂等读)。
    """
    _ensure_counter()
    return int(_thread_local.embedding_tokens)


def reset_embedding_tokens_total() -> None:
    """清零当前线程 embedding prompt_tokens 累加器。

    主要给 ``scripts/eval_qa_drift.py`` 跑前 / 测试 setup 用 —— 避免跨用例
    / 跨 run 的累加值互相污染。
    """
    _ensure_counter()
    _thread_local.embedding_tokens = 0