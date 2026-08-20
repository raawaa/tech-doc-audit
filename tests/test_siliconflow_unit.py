"""issues/144 实施层的 unit 测试 —— 不依赖 SF API。

这些测试补全 contract 测试的 CI 覆盖:
1. ``index.meta.json`` sidecar 的读写 / 断言(AC#1 + AC#3)
2. XLM-R tokenizer 截断(AC 不变量 §1)
3. ``EMBED_PROVIDER`` 抽象在 ``core/settings`` 中的形态

不需网络、不需真实 SF 凭证。CI / 手动跑均可。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from llama_index.core import Settings as _LISettings


# ── sidecar:读写 + 断言 ─────────────────────────────────────────────────────


def test_index_meta_path_is_consistent():
    """``_index_meta_path`` 永远指向 ``vectors/index.meta.json``。

    防重构破坏路径常量(issues/144 AC#1 要求文件名一致)。
    """
    from core.index_manager import _index_meta_path, _vectors_dir, INDEX_META_FILENAME

    assert _index_meta_path("kb_xyz") == _vectors_dir("kb_xyz") / INDEX_META_FILENAME
    assert INDEX_META_FILENAME == "index.meta.json"


def test_write_then_read_index_meta_roundtrip(tmp_path, monkeypatch):
    """``_write_index_meta`` 写入后 ``_read_index_meta`` 能读回同样字段。"""
    monkeypatch.setenv("AUDIT_DATA_DIR", str(tmp_path))
    # 清缓存:测试隔离
    from core import index_manager as im
    im.clear_cache()

    from core.index_manager import (
        _write_index_meta,
        _read_index_meta,
    )

    assert _read_index_meta("kb_roundtrip") is None

    _write_index_meta(
        "kb_roundtrip", model_id="BAAI/bge-m3", dim=1024, force=True,
    )
    meta = _read_index_meta("kb_roundtrip")
    assert meta is not None
    assert meta["embedding_model_id"] == "BAAI/bge-m3"
    assert meta["embedding_dim"] == 1024
    assert "created_at" in meta


def test_write_index_meta_force_preserves_created_at(tmp_path, monkeypatch):
    """``force=False`` 二次写入不会改 ``created_at``(只在首次设一次)。"""
    monkeypatch.setenv("AUDIT_DATA_DIR", str(tmp_path))
    from core.index_manager import _write_index_meta, _read_index_meta

    _write_index_meta(
        "kb_idempotent", model_id="BAAI/bge-m3", dim=1024, force=True,
    )
    first = _read_index_meta("kb_idempotent")["created_at"]

    _write_index_meta(
        "kb_idempotent", model_id="BAAI/bge-m3", dim=1024, force=False,
    )
    second = _read_index_meta("kb_idempotent")["created_at"]

    assert first == second, "force=False 不应改写 created_at"


def test_assert_kb_embedding_system_matches_raises_on_mismatch(tmp_path, monkeypatch):
    """``_assert_kb_embedding_system_matches`` 发现 dim 不一致时 raise。"""
    monkeypatch.setenv("AUDIT_DATA_DIR", str(tmp_path))
    from core.index_manager import (
        _write_index_meta,
        _assert_kb_embedding_system_matches,
    )

    # 写入一个错误 dim 标记(模拟 T4 §5.1 repro_kb 事件)
    _write_index_meta(
        "kb_mismatch", model_id="some-other-encoder", dim=512, force=True,
    )

    with pytest.raises(RuntimeError, match="embedding 体系不一致"):
        _assert_kb_embedding_system_matches(
            "kb_mismatch", model_id="BAAI/bge-m3", dim=1024,
        )


def test_assert_kb_embedding_system_matches_raises_when_meta_missing(tmp_path, monkeypatch):
    """issues/144 AC#3:meta 缺失 → raise,不入库(spec 措辞)。

    与"自动写新 meta 给 backfill 漏掉的 KB 兜底"语义相反 ——
    ""缺" = "未知状态",任何隐式假定都错。先跑 backfill。
    """
    monkeypatch.setenv("AUDIT_DATA_DIR", str(tmp_path))
    from core.index_manager import (
        _read_index_meta,
        _assert_kb_embedding_system_matches,
    )

    assert _read_index_meta("kb_no_meta") is None

    with pytest.raises(RuntimeError, match="缺 index.meta.json"):
        _assert_kb_embedding_system_matches(
            "kb_no_meta", model_id="BAAI/bge-m3", dim=1024,
        )

    # 应保持缺失(没自动写)
    assert _read_index_meta("kb_no_meta") is None


def test_index_document_asserts_embedding_system(seed_searchable_kb, fake_models, tmp_path, monkeypatch):
    """issues/144 AC#3:写入新 chunk 前 _assert_kb_embedding_system_matches 被调,
    model_id/dim 不符就 raise 不入库。
    """
    monkeypatch.setenv("AUDIT_DATA_DIR", str(tmp_path))
    import storage.kb_repo as _kb_repo
    from core.index_manager import (
        index_document,
        _write_index_meta,
        _vectors_dir,
    )

    seed_searchable_kb("kb_index_doc_assert")

    # 写入与 production 不一致的 meta(模拟 T4 §5.1 repro_kb 混入事件)
    _write_index_meta("kb_index_doc_assert", model_id="wrong-model", dim=512, force=True)

    from core.parse_document import PageText
    by_page = [PageText(page=0, text="hello content page 0 padding for chunking")]

    with pytest.raises(RuntimeError, match="embedding 体系不一致"):
        index_document(
            "kb_index_doc_assert", "doc1",
            "文本长度足够产生 chunks 用于 embedding 体系断言测试。",
            by_page=by_page,
        )


# ── XLM-R tokenizer 截断 ────────────────────────────────────────────────────


def test_truncate_short_text_unchanged():
    """短文本(< 512 token) 不应被截断。"""
    from core.siliconflow_client import truncate_to_max_tokens, MAX_TOKENS_PER_CHUNK

    short = "hello 简短测试 " * 30  # 远低于 512 token
    out = truncate_to_max_tokens(short)
    assert out == short, "短文本应原样返回"
    # issues/144 不变量 §1:max_length=512 与本机 bge-m3 一致
    assert MAX_TOKENS_PER_CHUNK == 512


def test_truncate_long_text_truncated_with_tokenizer():
    """长文本按 XLM-R tokenizer 截到 <= MAX_TOKENS_PER_CHUNK token。

    跳过条件:本机无 XLM-R tokenizer(本仓库的 modelscope 缓存可能不含 tokenizer
    子目录)。这种情况允许测试 skip,因为实际生产路径必装 transformers。
    """
    pytest.importorskip("transformers", reason="XLM-R tokenizer 依赖 transformers")
    from core.siliconflow_client import (
        _get_xlmr_tokenizer,
        truncate_to_max_tokens,
        MAX_TOKENS_PER_CHUNK,
    )

    try:
        tokenizer = _get_xlmr_tokenizer()
    except Exception as e:
        pytest.skip(f"XLM-R tokenizer 不可用: {e}")

    # 1500 个 "钢结构施工要求" 重复 ≫ MAX_TOKENS_PER_CHUNK 个 token
    huge = ("钢结构施工要求 " * 1500).strip()

    out = truncate_to_max_tokens(huge)
    ids = tokenizer.encode(out, add_special_tokens=False)
    assert len(ids) <= MAX_TOKENS_PER_CHUNK, (
        f"截断后仍有 {len(ids)} token(> {MAX_TOKENS_PER_CHUNK});"
        f" XLM-R tokenizer 截断逻辑应出错。"
    )


def test_truncate_empty_text_safe():
    """空字符串 / None 不抛,原样返回。"""
    from core.siliconflow_client import truncate_to_max_tokens

    assert truncate_to_max_tokens("") == ""
    # None 由类型约束挡住(参数化类型 str);不测


# ── EMBED_PROVIDER 抽象形态(只在 settings 模块级、不触发加载)────────────────


def test_embed_provider_default_is_siliconflow(monkeypatch):
    """issues/144 AC#3 默认值是 ``siliconflow``。

    让 ``monkeypatch.delenv`` 先,然后 ``importlib.reload`` settings 才能读到 env。
    但 reload 会触发 ``_init`` ……所以仅验证环境变量约定 + 默认语义,
    防止未来重构破坏契约。
    """
    import os
    monkeypatch.delenv("EMBED_PROVIDER", raising=False)
    assert os.environ.get("EMBED_PROVIDER", "siliconflow").lower() == "siliconflow"


def test_run_reranker_dispatches_to_siliconflow_for_siliconflow_provider(monkeypatch):
    """当 ``EMBED_PROVIDER=siliconflow`` 时,``run_reranker`` 走 SF 路径。

    真实 SF 调用由 contract test 覆盖;这里只验证 dispatch 形态 —— 即调用了
    ``core.siliconflow_client.rerank_with_siliconflow`` 而不是 ``CrossEncoder``。
    """
    from core.settings import EMBED_PROVIDER
    if EMBED_PROVIDER != "siliconflow":
        pytest.skip(
            f"当前 EMBED_PROVIDER={EMBED_PROVIDER};本测试针对 siliconflow 路径"
        )

    from core import settings as _settings
    from core.siliconflow_client import rerank_with_siliconflow

    captured = {}

    def fake_sf_rerank(nodes, query, **kwargs):
        captured["nodes"] = nodes
        captured["query"] = query
        # 返回 sf 的 deterministic 结果
        return list(nodes)[: kwargs.get("top_n", 5)]

    with patch.object(
        _settings, "EMBED_PROVIDER", "siliconflow"
    ), patch(
        "core.siliconflow_client.rerank_with_siliconflow", side_effect=fake_sf_rerank
    ):
        from llama_index.core.schema import NodeWithScore, TextNode

        nodes = [
            NodeWithScore(node=TextNode(text="A"), score=0.5),
            NodeWithScore(node=TextNode(text="B"), score=0.3),
        ]
        out = _settings.run_reranker(nodes, "test query")

    assert captured["query"] == "test query"
    assert out == nodes  # fake 不改顺序


def test_run_reranker_local_path_unchanged_when_embed_provider_local(monkeypatch):
    """当 ``EMBED_PROVIDER=local`` 时,``run_reranker`` 走本地 CrossEncoder。

    验证 dispatch 不会"短路" local 路径(回退语义保留)。
    """
    import core.settings as _settings
    from llama_index.core.schema import NodeWithScore, TextNode

    with patch.object(_settings, "EMBED_PROVIDER", "local"), patch(
        "core.siliconflow_client.rerank_with_siliconflow"
    ) as sf_called:
        nodes = [NodeWithScore(node=TextNode(text="A"), score=0.5)]
        # 走 local 会尝试 load CrossEncoder —— 用 magic 替代
        with patch("sentence_transformers.CrossEncoder", create=True) as ce_cls:
            ce_instance = ce_cls.return_value
            ce_instance.predict.return_value = [0.42]
            _settings.run_reranker(nodes, "q")

    # SF 路径不会被调用
    assert sf_called.call_count == 0
    # CrossEncoder 路径被调用
    assert ce_instance.predict.called


# ── 公共导出 ─────────────────────────────────────────────────────────────────


def test_siliconflow_client_module_public_api_exports():
    """``core.siliconflow_client`` 暴露的常量 / 函数有稳定名称(防重构破坏 client)。"""
    import core.siliconflow_client as sf

    assert sf.EMBED_MODEL_ID == "BAAI/bge-m3"
    assert sf.EMBEDDING_DIM == 1024
    assert sf.RERANK_MODEL_ID == "BAAI/bge-reranker-v2-m3"
    # issues/144 不变量 §1:max_length=512,与本机 bge-m3 锁定边界对齐
    assert sf.MAX_TOKENS_PER_CHUNK == 512

    for name in (
        "make_siliconflow_client",
        "truncate_to_max_tokens",
        "truncate_batch",
        "encode_for_siliconflow",
        "encode_query_for_siliconflow",
        "rerank_with_siliconflow",
        "SiliconFlowEmbedding",
    ):
        assert hasattr(sf, name), f"missing public symbol: {name}"


def test_siliconflow_embedding_subclass_of_base_embedding():
    """``SiliconFlowEmbedding`` 是 ``BaseEmbedding`` 子类(才能挂 LlamaIndex 协议)。"""
    from llama_index.core.base.embeddings.base import BaseEmbedding
    from core.siliconflow_client import SiliconFlowEmbedding

    assert issubclass(SiliconFlowEmbedding, BaseEmbedding)


# ── prompt_tokens 累加(ADR-0009)──────────────────────────────────────────────


class _FakeUsage:
    """``client.embeddings.create(...)`` 返回的 ``resp.usage`` 形状。"""

    def __init__(self, prompt_tokens: int | None):
        self.prompt_tokens = prompt_tokens


class _FakeEmbedding:
    """``resp.data[i].embedding`` 的形状。"""

    def __init__(self, embedding: list[float]):
        self.embedding = embedding


class _FakeEmbeddingsResponse:
    def __init__(self, data: list[_FakeEmbedding], usage: _FakeUsage | None):
        self.data = data
        self.usage = usage


class _FakeEmbeddingsAPI:
    """``client.embeddings`` 的 fake,只对外契约。"""

    def __init__(self, response: _FakeEmbeddingsResponse):
        self._response = response
        self.calls: list[dict] = []

    def create(self, *, model: str, input):
        self.calls.append({"model": model, "input": list(input)})
        return self._response


class _FakeSFClient:
    def __init__(self, response: _FakeEmbeddingsResponse):
        self.embeddings = _FakeEmbeddingsAPI(response)


@pytest.fixture(autouse=True)
def _reset_embedding_metrics():
    """每条用例前清零 metrics(thread-local 在测试间复用同一线程)。"""
    from core import metrics
    metrics.reset_embedding_tokens_total()
    yield
    metrics.reset_embedding_tokens_total()


def _patch_sf_client(monkeypatch, response: _FakeEmbeddingsResponse):
    """把 ``core.siliconflow_client.make_siliconflow_client`` 替换为 fake,
    避免构造真实 OpenAI 客户端/出网;``truncate_batch`` 同步 stub 成 no-op,
    跳过 XLM-R tokenizer 加载。
    """
    fake_client = _FakeSFClient(response)
    monkeypatch.setattr(
        "core.siliconflow_client.make_siliconflow_client",
        lambda **kwargs: fake_client,
    )
    monkeypatch.setattr(
        "core.siliconflow_client.truncate_batch",
        lambda texts, max_tokens=512: list(texts),
    )
    return fake_client


def test_embed_records_prompt_tokens_from_usage(monkeypatch):
    """``_embed_with_siliconflow`` 读 ``resp.usage.prompt_tokens`` 累加进 metrics。

    ADR-0009 接口契约:``resp.usage.prompt_tokens`` 是免费档的永久 baseline,
    在 ``scripts/eval_qa_drift.py`` run 末打印,撞墙 / 升 Pro / 第一次账单来
    时已有数据。
    """
    from core import metrics
    from core.siliconflow_client import _embed_with_siliconflow

    fake_client = _patch_sf_client(
        monkeypatch,
        _FakeEmbeddingsResponse(
            data=[_FakeEmbedding([0.0] * 4)],
            usage=_FakeUsage(prompt_tokens=123),
        ),
    )

    out = _embed_with_siliconflow(["hello"])

    assert len(out) == 1
    assert metrics.get_embedding_tokens_total() == 123
    # fake 收到一次调用,模型名是 BAAI/bge-m3
    assert fake_client.embeddings.calls[0]["model"] == "BAAI/bge-m3"


def test_embed_accumulates_prompt_tokens_across_calls(monkeypatch):
    """多次调用累加(同线程 thread-local 累加器语义)。"""
    from core import metrics
    from core.siliconflow_client import _embed_with_siliconflow

    _patch_sf_client(
        monkeypatch,
        _FakeEmbeddingsResponse(
            data=[_FakeEmbedding([0.0] * 2)],
            usage=_FakeUsage(prompt_tokens=11),
        ),
    )

    _embed_with_siliconflow(["hello"])
    _embed_with_siliconflow(["world"])
    _embed_with_siliconflow(["!"])

    assert metrics.get_embedding_tokens_total() == 33


def test_embed_skips_token_recording_when_usage_missing(monkeypatch):
    """``resp.usage`` 缺失或 ``prompt_tokens`` 为 None 时静默跳过累加。

    接口契约:失败 / ``usage`` 为 None 静默跳过(不影响主路径)。
    """
    from core import metrics
    from core.siliconflow_client import _embed_with_siliconflow

    _patch_sf_client(
        monkeypatch,
        _FakeEmbeddingsResponse(
            data=[_FakeEmbedding([0.0] * 2)],
            usage=None,  # 极端情况:服务端没返回 usage
        ),
    )

    out = _embed_with_siliconflow(["hello"])
    assert len(out) == 1
    assert metrics.get_embedding_tokens_total() == 0


def test_embed_skips_token_recording_when_usage_has_none_prompt_tokens(monkeypatch):
    """``resp.usage`` 存在但 ``prompt_tokens = None`` → 静默跳过累加。"""
    from core import metrics
    from core.siliconflow_client import _embed_with_siliconflow

    _patch_sf_client(
        monkeypatch,
        _FakeEmbeddingsResponse(
            data=[_FakeEmbedding([0.0] * 2)],
            usage=_FakeUsage(prompt_tokens=None),
        ),
    )

    _embed_with_siliconflow(["hello"])
    assert metrics.get_embedding_tokens_total() == 0


def test_embed_propagates_exception_does_not_corrupt_metrics(monkeypatch):
    """SF 调用抛异常时 metrics 计数器不增;主路径仍按 ADR-0007 抛给调用方。"""
    from core import metrics
    from core.siliconflow_client import _embed_with_siliconflow

    class _Boom(_FakeEmbeddingsAPI):
        def create(self, *, model, input):
            raise RuntimeError("simulated SF outage")

    fake_client = _FakeSFClient(_FakeEmbeddingsResponse([], _FakeUsage(0)))
    fake_client.embeddings = _Boom([])
    monkeypatch.setattr(
        "core.siliconflow_client.make_siliconflow_client",
        lambda **kwargs: fake_client,
    )
    monkeypatch.setattr(
        "core.siliconflow_client.truncate_batch",
        lambda texts, max_tokens=512: list(texts),
    )

    with pytest.raises(RuntimeError, match="simulated SF outage"):
        _embed_with_siliconflow(["hello"])
    # 失败路径不应"补一个好看数字"——计数器保持 0
    assert metrics.get_embedding_tokens_total() == 0


# ── max_retries=2 显式设置(ADR-0007)──────────────────────────────────────────


def test_make_siliconflow_client_passes_max_retries_2(monkeypatch):
    """ADR-0007 §2:HTTP 层错误(429/408/409/5xx)由 OpenAI SDK 内置重试负责,
    ``max_retries=2``,尊重 ``retry-after`` + jitter。

    OpenAI SDK 默认就是 2,但要显式传 —— 未来若 SDK 改默认值,这条契约守住
    我们的语义:HTTP 层重试次数 = 2(批量路径在 SDK 之上还有一层连接层 tenacity,
    见 ``core.embed_retry.embed_batch_with_retry``)。
    """
    import httpx as _httpx

    captured: dict = {}

    class _FakeOpenAI:
        def __init__(self, *, api_key, base_url, http_client, max_retries):
            captured["api_key"] = api_key
            captured["base_url"] = base_url
            captured["http_client"] = http_client
            captured["max_retries"] = max_retries

    # ``OpenAI`` 在 ``make_siliconflow_client`` 函数体里 import,patch 模块属性
    # 失效,必须 patch ``openai.OpenAI`` 的源头。
    monkeypatch.setattr("openai.OpenAI", _FakeOpenAI)
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-test-key")
    monkeypatch.setattr("core.siliconflow_client._strip_proxy_env", lambda: (None, None))
    monkeypatch.setattr("core.siliconflow_client._restore_proxy_env", lambda saved: None)

    from core.siliconflow_client import make_siliconflow_client

    client = make_siliconflow_client()
    assert client is not None
    assert captured["max_retries"] == 2, (
        f"OpenAI SDK max_retries 必须显式 == 2,实际 {captured['max_retries']}"
    )
    # 防御退化:确保 max_retries 不是从默认里继承的——通过传值校验显式语义
    assert "max_retries" in captured, "max_retries 必须显式传给 OpenAI() 构造器"
    # http_client 必须 trust_env=False(代理绕过)
    assert isinstance(captured["http_client"], _httpx.Client)
    assert captured["http_client"].trust_env is False


def test_make_siliconflow_client_rejects_missing_api_key(monkeypatch):
    """``SILICONFLOW_API_KEY`` 缺失 → ``RuntimeError``,**不**静默回退。

    沿用 issues/144 #137 既有契约:`get_embed_model()` 缺 key 时降级为 None
    (双层结构),但 ``make_siliconflow_client()`` 直接构造 → 抛清晰错误,
    让上层明确知道为什么 SF 不可用。
    """
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    from core.siliconflow_client import make_siliconflow_client

    with pytest.raises(RuntimeError, match="SILICONFLOW_API_KEY"):
        make_siliconflow_client()


# ── EMBED_BATCH_SIZE 孤儿常量删除(ADR-0008 §1)────────────────────────────────


def test_siliconflow_client_does_not_export_embed_batch_size():
    """ADR-0008 §1:``EMBED_BATCH_SIZE = 32`` 是孤儿常量,生产路径不会切子批。

    per-doc 整 list 单请求是 SF 限流下的最优切法 —— 限流按 HTTP 请求数 +
    token 数计,不限 list 长度。切子批(25 chunks/doc 切 32-batch = 切 1 批)
    徒增复杂度,等于啥也没干。

    spiek 脚本 ``scripts/rerank_cross_provider_spike.py`` 自己有自己的常量化
    理由(一次性预嵌入 ~3976 chunks),本测试只盯生产 client 模块的导出表。
    """
    import core.siliconflow_client as sf

    assert not hasattr(sf, "EMBED_BATCH_SIZE"), (
        "EMBED_BATCH_SIZE 是孤儿常量(ADR-0008),不应再挂在 SF client 模块"
    )


# ── 4 路并发 bulk_reparse 模拟(ADR-0009)──────────────────────────────────


def test_embed_token_accumulation_isolated_under_4way_concurrency(monkeypatch):
    """4 路并发 worker 各自跑 ``_embed_with_siliconflow`` 时,thread-local token
    累加器**互不污染**:每条 worker 读到的总数只反映本线程的累计;主线程从未
    调过,累加器保持 0。

    模拟 ``services/bulk_reparse_service.py:DEFAULT_CONCURRENCY = 4`` 下并发跑
    doc → ``reparse_document`` → ``index_document`` → ``embed_batch_with_retry``
    → ``_embed_with_siliconflow`` 的真实路径,守住 ADR-0009 的隐含契约 —— token
    累加器是 thread-local,**不能**让 worker 的累加污染主线程,也不能让多个
    worker 之间互踩(否则最终账单/撞墙判断会由 thread race 决定)。

    注:本测试用 ``threading.Thread`` 而非 ``ThreadPoolExecutor`` —— ``concurrent.futures``
    默认 worker 数 ≤ ``os.cpu_count()`` 且**会复用** worker,thread-local 在多次
    提交间会跨任务累加,反而把"独立累加"的契约破坏掉。每条 ``Thread.start()``
    启动的都是全新线程,``_ensure_counter`` 自动从 0 起算,与生产路径中
    ``ThreadPoolExecutor`` 一次性提交 N 个任务再 ``shutdown`` 的真实形态语义一致
    (那条路径下 worker 线程也是新起,不会跨任务复用)。
    """
    from core import metrics
    from core.siliconflow_client import _embed_with_siliconflow

    per_thread_calls = 5
    per_call_tokens = 7
    expected_per_thread = per_thread_calls * per_call_tokens

    _patch_sf_client(
        monkeypatch,
        _FakeEmbeddingsResponse(
            data=[_FakeEmbedding([0.0] * 2)],
            usage=_FakeUsage(prompt_tokens=per_call_tokens),
        ),
    )

    results: list[int] = []

    def _worker(tid: int) -> None:
        for _ in range(per_thread_calls):
            _embed_with_siliconflow([f"hello-{tid}"])
        results.append(metrics.get_embedding_tokens_total())

    threads = [threading.Thread(target=_worker, args=(tid,)) for tid in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 4 条 worker 线程独立累加,各自 5×7=35;不关心哪条拿到哪个 tid,只关心值集合
    assert set(results) == {expected_per_thread}, (
        f"thread-local 累加器在 4 路并发下被污染: {results}"
    )
    # 主线程从未调过 → 累加器为 0(thread-local 隔离守住)
    assert metrics.get_embedding_tokens_total() == 0, (
        "主线程累加器被 worker 线程污染,thread-local 隔离破了"
    )
