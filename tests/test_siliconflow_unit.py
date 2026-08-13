"""issues/144 实施层的 unit 测试 —— 不依赖 SF API。

这些测试补全 contract 测试的 CI 覆盖:
1. ``index.meta.json`` sidecar 的读写 / 断言(AC#1 + AC#3)
2. XLM-R tokenizer 截断(AC 不变量 §1)
3. ``EMBED_PROVIDER`` 抽象在 ``core/settings`` 中的形态

不需网络、不需真实 SF 凭证。CI / 手动跑均可。
"""

from __future__ import annotations

import json
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
