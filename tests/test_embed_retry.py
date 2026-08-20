"""``core.embed_retry`` 重试层契约(ADR-0007 §2)。

协议(唯一 owner 原则——每类瞬态失败只有一层重试):
- **HTTP 层错误**(429/408/409/5xx)由 OpenAI SDK 内置 ``max_retries=2`` 负责,
  本层**不**重试;
- **连接层错误**(``APIConnectionError`` / ``APITimeoutError``)由本层 tenacity
  负责:3 次,2s→30s 指数退避;
- **不可重试错误**(模型缺失 / ValueError / CUDA OOM 等)零重试,立即上抛。

本文件的用例由 ``tests/test_index_manager.py`` 迁入(issue #167 把重试层
搬到 ``core/embed_retry``),断言对象从私有 ``im._embed_batch_with_retry``
换成公开 ``core.embed_retry.embed_batch_with_retry``。
"""
import httpx
import pytest
from openai import APIConnectionError, APIStatusError, APITimeoutError

import core.embed_retry as er
from core.embed_retry import embed_batch_with_retry

_URL = "https://api.siliconflow.cn/v1/embeddings"

#: 导入时快照生产常量 —— 下面的 autouse fixture 会把退避压成 0,
#: 常量契约测试必须对着快照断言而不是被改过的模块属性。
_PRODUCTION_CONSTANTS = (
    er._EMBED_BATCH_RETRY_ATTEMPTS,
    er._EMBED_BATCH_RETRY_MIN_S,
    er._EMBED_BATCH_RETRY_MAX_S,
)


def _conn_error() -> APIConnectionError:
    return APIConnectionError(request=httpx.Request("POST", _URL))


def _timeout_error() -> APITimeoutError:
    return APITimeoutError(request=httpx.Request("POST", _URL))


def _status_error(code: int = 429) -> APIStatusError:
    return APIStatusError(
        "rate limit",
        response=httpx.Response(code, request=httpx.Request("POST", _URL)),
        body=None,
    )


class _FakeEmbedModel:
    """``embed_batch_with_retry`` 的可控 fake。

    记录每次调用次数与收到的 texts,按 ``errors`` 队列逐次抛错(队列耗尽 →
    成功返回),验证 tenacity 重试层只重试连接错误、对其他异常零重试。
    """

    def __init__(self, *, return_value=None, errors=None):
        self._return_value = return_value if return_value is not None else [[0.0] * 4]
        self._errors = list(errors or [])
        self.call_count = 0
        self.seen_texts: list[list[str]] = []

    def get_text_embedding_batch(self, texts):
        self.call_count += 1
        self.seen_texts.append(texts)
        if self._errors:
            raise self._errors.pop(0)
        return self._return_value


@pytest.fixture(autouse=True)
def _compress_retry_wait(monkeypatch):
    """把退避下界/上界压成 0。

    生产值 2s 起 → 30s 上限,跑 3 次连接错误要真等 2s+4s。常量在**调用时**
    读(用 ``Retrying(...)`` 而非 ``@retry`` 装饰器的原因),故 monkeypatch
    模块属性即可生效。
    """
    monkeypatch.setattr(er, "_EMBED_BATCH_RETRY_MIN_S", 0)
    monkeypatch.setattr(er, "_EMBED_BATCH_RETRY_MAX_S", 0)


# ── 成功路径 ──────────────────────────────────────────────────────────────────


def test_returns_immediately_on_success():
    """成功调用 → 1 次,无重试。"""
    fake = _FakeEmbedModel(return_value=[[0.1, 0.2, 0.3, 0.4]])
    out = embed_batch_with_retry(fake, ["hello"])
    assert out == [[0.1, 0.2, 0.3, 0.4]]
    assert fake.call_count == 1


def test_passes_texts_through_untouched():
    """本层只管重试,不碰 batch 内容 —— 原样透传给 embed model。"""
    fake = _FakeEmbedModel(return_value=[[0.0] * 4, [0.0] * 4])
    texts = ["第一条 应急救援", "第二条 指挥中心"]
    embed_batch_with_retry(fake, texts)
    assert fake.seen_texts == [texts]


def test_empty_batch_still_delegates():
    """空 batch 不在本层短路 —— 语义由 embed model 决定,重试层不加判断。"""
    fake = _FakeEmbedModel(return_value=[])
    assert embed_batch_with_retry(fake, []) == []
    assert fake.call_count == 1


# ── 连接层错误:重试(ADR-0007 §2)────────────────────────────────────────────


def test_retries_api_connection_error_3_times():
    """``APIConnectionError`` 重试 3 次后 reraise(``stop_after_attempt(3)``)。"""
    fake = _FakeEmbedModel(errors=[_conn_error()] * 5)
    with pytest.raises(APIConnectionError):
        embed_batch_with_retry(fake, ["hello"])
    assert fake.call_count == 3


def test_retries_api_timeout_error():
    """``APITimeoutError``(``APIConnectionError`` 子类)同样进重试。"""
    fake = _FakeEmbedModel(errors=[_timeout_error()] * 5)
    with pytest.raises(APITimeoutError):
        embed_batch_with_retry(fake, ["hello"])
    assert fake.call_count == 3


def test_recovers_after_transient_connection_error():
    """前 2 次 ``APIConnectionError``,第 3 次成功 → 返回结果(不抛)。"""
    fake = _FakeEmbedModel(
        errors=[_conn_error()] * 2,
        return_value=[[0.1, 0.2, 0.3, 0.4]],
    )
    out = embed_batch_with_retry(fake, ["hello"])
    assert out == [[0.1, 0.2, 0.3, 0.4]]
    assert fake.call_count == 3


def test_recovers_on_second_attempt():
    """单次抖动 → 第 2 次即成功,不必耗满 3 次预算。"""
    fake = _FakeEmbedModel(errors=[_conn_error()], return_value=[[1.0] * 4])
    assert embed_batch_with_retry(fake, ["hello"]) == [[1.0] * 4]
    assert fake.call_count == 2


def test_reraises_original_exception_not_retry_error():
    """``reraise=True``:上抛的是原始异常而非 tenacity 的 ``RetryError``。

    调用方(``index_documents_batch``)按异常类型记账并写
    ``embedding_error``,包一层 RetryError 会把类型名污染成
    ``RetryError: RetryError[...]``。
    """
    original = _conn_error()
    fake = _FakeEmbedModel(errors=[original] * 5)
    with pytest.raises(APIConnectionError) as excinfo:
        embed_batch_with_retry(fake, ["hello"])
    assert excinfo.value is original


# ── 不可重试错误:零重试(ADR-0007 §1)───────────────────────────────────────


def test_does_not_retry_value_error():
    """``ValueError`` 不进重试白名单 → 立即抛。

    关键防退化测试:防止未来重构把 ``retry_if_exception_type`` 改成 retry
    all exceptions —— 那样会让 ValueError 等不可重试错误白白耗 3 次。
    """
    fake = _FakeEmbedModel(errors=[ValueError("不可重试的错误")])
    with pytest.raises(ValueError, match="不可重试"):
        embed_batch_with_retry(fake, ["hello"])
    assert fake.call_count == 1


def test_does_not_retry_runtime_error():
    """``RuntimeError`` 同样不进重试(本地 bge-m3 路径 CUDA OOM 也按"失败即抛")。"""
    fake = _FakeEmbedModel(errors=[RuntimeError("CUDA out of memory")])
    with pytest.raises(RuntimeError, match="CUDA out of memory"):
        embed_batch_with_retry(fake, ["hello"])
    assert fake.call_count == 1


def test_does_not_retry_http_status_errors():
    """``APIStatusError``(429/5xx 等)不在本层白名单 → 不重试。

    理由:HTTP 层错误由 OpenAI SDK 内置 ``max_retries=2`` 负责,本层不重复。
    SDK 实在扛不住时上抛的 ``RateLimitError`` / ``InternalServerError`` 等
    ``APIStatusError`` 子类**不**进本层重试 —— 避免双层重试(ADR-0007 §2)。
    """
    fake = _FakeEmbedModel(errors=[_status_error(429)])
    with pytest.raises(APIStatusError):
        embed_batch_with_retry(fake, ["hello"])
    assert fake.call_count == 1, (
        f"HTTP 错误不该被本层重试(SDK 自己 retry);实际 {fake.call_count} 次"
    )


def test_does_not_retry_server_side_status_errors():
    """5xx 与 429 同属 HTTP 层 —— 同样归 SDK,本层零重试。"""
    fake = _FakeEmbedModel(errors=[_status_error(503)])
    with pytest.raises(APIStatusError):
        embed_batch_with_retry(fake, ["hello"])
    assert fake.call_count == 1


def test_does_not_retry_bare_exception():
    """未知异常按不可重试处置(白名单而非黑名单)。"""
    fake = _FakeEmbedModel(errors=[Exception("模型未加载")])
    with pytest.raises(Exception, match="模型未加载"):
        embed_batch_with_retry(fake, ["hello"])
    assert fake.call_count == 1


# ── 常量契约 ──────────────────────────────────────────────────────────────────


def test_retry_constants_match_adr_0007():
    """3 次 / 2s 起 / 30s 封顶 —— 改动即 ADR-0007 §2 变更,必须显式过一次评审。

    读的是导入时的快照(``_PRODUCTION_CONSTANTS``),不受 autouse 压缩
    fixture 影响。
    """
    assert _PRODUCTION_CONSTANTS == (3, 2, 30)
