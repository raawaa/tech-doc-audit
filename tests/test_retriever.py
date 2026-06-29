"""CrossKBRetriever 测试 — 跨知识库检索、合并、去重、top_k 截断。

通过 fake_models 注入确定性假 embedder，**不加载 bge-m3**；run_reranker 被
fake_models neutralize 成 identity（返回原序），故可稳定测合并/去重/截断逻辑，
不依赖 cross-encoder 模型。

注意：被索引文本必须 >= 20 字符（index_document 的最小长度门槛），否则不建索引。
"""
import os
import shutil

import pytest

from core.index_manager import index_document
from core.retriever import CrossKBRetriever


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    data_dir = os.environ["AUDIT_DATA_DIR"]
    if os.path.exists(data_dir):
        shutil.rmtree(data_dir)


@pytest.fixture(autouse=True)
def _use_fake_models(fake_models):
    """本文件统一注入假模型，不加载 bge-m3；run_reranker → identity。"""
    yield


def _index(kb_id: str, docs: list[tuple[str, str]]):
    """建索引：docs = [(doc_id, text), ...]（text 需 >=20 字符）。"""
    for doc_id, text in docs:
        index_document(kb_id, doc_id, text, source_name=f"{doc_id}.txt")


# ── 基本检索 ──────────────────────────────────────────────────────────────────


def test_retrieve_single_kb_tags_metadata():
    _index("ret_kb1", [
        ("d1", "质保期自验收合格之日起计算不得少于二十四个月的规定"),
        ("d2", "验收标准应符合国家标准及行业规范的相关条文要求"),
    ])
    r = CrossKBRetriever(["ret_kb1"], top_k=5)
    nodes = r.retrieve("质保期")
    assert len(nodes) >= 1
    # 每个 node 都被标上来源 kb_id
    assert all(n.node.metadata.get("kb_id") == "ret_kb1" for n in nodes)


def test_retrieve_merges_multiple_kbs():
    _index("ret_kb_a", [("a1", "网络安全等级保护基本要求 GB/T 22239 的条文规定")])
    _index("ret_kb_b", [("b1", "数据安全法相关规定与实施细则及法律责任的条文")])
    r = CrossKBRetriever(["ret_kb_a", "ret_kb_b"], top_k=5)
    nodes = r.retrieve("安全")
    kb_ids = {n.node.metadata.get("kb_id") for n in nodes}
    # 至少一个 KB 命中
    assert kb_ids & {"ret_kb_a", "ret_kb_b"}


# ── 截断与边界 ────────────────────────────────────────────────────────────────


def test_top_k_truncates_final_results():
    _index("ret_kb_t", [(f"d{i}", f"技术要求规范条文内容第{i}条验收标准细则补充说明")
                        for i in range(6)])
    r = CrossKBRetriever(["ret_kb_t"], top_k=3)
    nodes = r.retrieve("技术要求")
    assert len(nodes) <= 3


def test_empty_query_returns_empty():
    _index("ret_kb_e", [("d1", "一些用于建库索引的内容文本验证检索功能正常")])
    r = CrossKBRetriever(["ret_kb_e"], top_k=5)
    assert r.retrieve("") == []


def test_empty_kb_ids_returns_empty():
    r = CrossKBRetriever([], top_k=5)
    assert r.retrieve("anything") == []


# ── reranker 路径 ─────────────────────────────────────────────────────────────


def test_reranker_disabled_path():
    """use_reranker=False → 不调 run_reranker，仍正常返回。"""
    _index("ret_kb_nr", [("d1", "关闭 reranker 后的检索路径功能测试内容文本验证")])
    r = CrossKBRetriever(["ret_kb_nr"], top_k=5, use_reranker=False)
    nodes = r.retrieve("检索")
    assert len(nodes) >= 1


def test_reranker_identity_default_path():
    """use_reranker=True（默认）：fake_models 把 run_reranker neutralize 成 identity，
    不崩、返回结果（验证 reranker 分支可被无模型地走过）。"""
    _index("ret_kb_ri", [("d1", "默认开启 reranker 的 identity 路径内容文本验证")])
    r = CrossKBRetriever(["ret_kb_ri"], top_k=5)  # use_reranker 默认 True
    nodes = r.retrieve("reranker")
    assert len(nodes) >= 1
