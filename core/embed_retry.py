"""批量 embedding 的**连接层**重试(ADR-0007 §2 的唯一 owner)。

协议(每类瞬态失败只有一个重试 owner,不得另起一层):
- HTTP 层错误（429/408/409/5xx）由 OpenAI SDK 内置重试（``max_retries=2``，
  尊重 ``retry-after`` + jitter）在 ``core.siliconflow_client.make_siliconflow_client``
  里负责 —— 撞墙 → SDK 重试 → SDK 实在扛不住才上抛。
- 连接层错误（``APIConnectionError`` / ``APITimeoutError``）由本模块 tenacity
  负责，仅批量路径：3 次，2s→30s 指数退避（``stop_after_attempt(3)`` +
  ``wait_exponential(multiplier=2, min=2, max=30)``）。
- 查询路径零附加重试：``_embed_with_siliconflow`` 无 tenacity 包裹。
- 不可重试错误（模型缺失 / ValueError / RuntimeError 等）**不**进入本层
  重试 —— ``retry_if_exception_type((APIConnectionError, APITimeoutError))``
  严格白名单。

为什么独立成模块(issue #167):重试语义与"怎么建 FAISS 索引"是两件会因不同
理由变化的事。抽出来后 ``core/siliconflow_client`` 想复用同一层退避不必再抄
一份;``KBIndexWriter`` 当前是唯一调用方。
"""
from __future__ import annotations

# OpenAI SDK 的连接层错误类型(ADR-0007 §2 重试白名单)。openai 是硬依赖
# (pyproject.toml),缺包时 import 即崩;不留 fallback 兜底,故障定位更直接。
from openai import APIConnectionError, APITimeoutError
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

# 之所以用 ``Retrying(...)`` 而非 ``@retry`` 装饰器：常量在调用时解析，测试
# 可 monkeypatch 三个 ``_EMBED_BATCH_RETRY_*`` 常量把等待压缩到 0，避免 2s+4s
# 的真实 wall-time。装饰器版本会把值烘在装饰时，测试改不动。

#: 批量 embedding 连接层重试次数（ADR-0007 §2:3 次）。
_EMBED_BATCH_RETRY_ATTEMPTS = 3
#: 指数退避下界（秒）。第二次起按 2× 翻倍，封顶 ``_EMBED_BATCH_RETRY_MAX_S``。
_EMBED_BATCH_RETRY_MIN_S = 2
#: 指数退避上界（秒）。2 × 2^(n-1) 不会真的超过 30s。
_EMBED_BATCH_RETRY_MAX_S = 30


def embed_batch_with_retry(embed_model, texts: list[str]) -> list:
    """批量 embedding，**连接层**瞬态错误自动重试（最多 3 次，2s→30s 指数退避）。

    仅 ``APIConnectionError`` / ``APITimeoutError`` 重试 —— HTTP 层错误
    （429/408/409/5xx）由 OpenAI SDK 内置 ``max_retries=2`` 负责（见
    ``make_siliconflow_client``）。其他错误（模型缺失 / ValueError / CUDA
    OOM 等）**不**进重试，立即抛出给调用方按 ADR-0007 §1（无自动兜底）、
    §3（每稿隔离）处置。``reraise=True`` 保证上抛的是原始异常而非
    ``RetryError`` —— 调用方按异常类型写 ``embedding_error``。

    查询路径不调用本函数：``search()`` → ``retriever.retrieve()`` →
    ``encode_query_for_siliconflow`` → 直接 ``_embed_with_siliconflow``
    无 tenacity 包裹 —— 用户在等，长退避体感即挂死。

    Args:
        embed_model: 任何提供 ``get_text_embedding_batch(texts)`` 的 embedder
            (llama_index ``BaseEmbedding`` 或等价 duck type)。issue #165 的
            规格里称其为 ``client``——本模块按实际契约命名为 ``embed_model``,
            调用点一律位置传参,两种叫法可互换。
        texts: 本批要 embed 的文本(即规格里的 ``batch``);本层原样透传,
            不做切分 / 去空 / 截断。
    """
    retrying = Retrying(
        stop=stop_after_attempt(_EMBED_BATCH_RETRY_ATTEMPTS),
        wait=wait_exponential(
            multiplier=2,
            min=_EMBED_BATCH_RETRY_MIN_S,
            max=_EMBED_BATCH_RETRY_MAX_S,
        ),
        retry=retry_if_exception_type(
            (APIConnectionError, APITimeoutError)
        ),
        reraise=True,
    )
    return retrying(embed_model.get_text_embedding_batch, texts)
