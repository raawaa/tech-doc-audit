"""SiliconFlow 客户端 — embedding + rerank(T3/T4 spike 通过的硬约束)。

本模块集中所有与 SiliconFlow 在线 API 的接触面,统一封装三个职责:

1. **Embedding 适配器** ``SiliconFlowEmbedding`` —— 给 LlamaIndex 当 ``embed_model`` 用。
   自动按 XLM-R tokenizer 截断到 ``MAX_TOKENS_PER_CHUNK``(防 SF 服务端 HTTP 400 code 20015,
   T4 §5.2 实测 74421 字符极端 chunk 被拒);批量调用 ``client.embeddings.create``。
2. **Encode 工具函数** —— ``encode_for_siliconflow()`` / ``encode_query_for_siliconflow()``。
   分别用于写入(``index_document``)和查询(``search``)。两者最终走 SF 同一端点,
   与本机 bge-m3 的角色语义完全一致(T3 §1/§6)。
3. **Rerank 适配函数** ``rerank_with_siliconflow()`` —— 给 ``run_reranker()`` 调用。
   用 httpx 直接 POST ``/v1/rerank``(非 OpenAI 兼容 schema,T3 §2.3 实测)。

## 锁定的不变量(由 #138 map 决策,#143 T5 收尾)

1. **每个 chunk 在编码前按 XLM-R tokenizer 截到前 ~7000 token**(留 ~1000 余量,避开 SF 端 HTTP 400)。
   同时满足:(a) 与存量 3977 chunks 的物理编码边界对齐(本机 bge-m3 ``max_length=512`` 截断);
   (b) chunk splitter 边界不动(本模块不切分块)。
2. **客户端不调 normalize_l2**:T3 §1.3 + T4 §2.1 实测 SF 已归一化,``max|Δ| = 5.96e-08``。
3. **客户端不主动剥 query instruction 前缀**:T3 §3.3 实测 SF 不偷加,``qvec_cos ≥ 0.9999``。
4. **代理绕过范式**:与 ``make_deepseek_client`` 同模式 —— ``httpx.Client(trust_env=False)`` +
   ``_proxy_env_lock`` 下临时摘除 ``ALL_PROXY``/``all_proxy``(T3 §4.2)。
5. **行为变化的回归捕获**:契约测试写在 ``tests/test_siliconflow_contract.py``,**不每条请求跑**。

回退:
- 客户端不需要再 normalize、不需要再剥前缀。
- 服务端行为变化时再恢复这两步。
"""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING

from llama_index.core.base.embeddings.base import BaseEmbedding

from core.logger import get_logger

if TYPE_CHECKING:
    from openai import OpenAI
    import httpx

_logger = get_logger(__name__)


# ── 硬编码常量(由 #138 map 锁定,#143 T5 收尾)─────────────────────────────

#: bge-m3 模型 ID(SF 端点名 + 本机 HF 模型名同字面)。
EMBED_MODEL_ID = "BAAI/bge-m3"
#: SF bge-m3 输出维度(由 SF API 决定,与本机 bge-m3 完全一致)。
EMBEDDING_DIM = 1024
#: bge-reranker-v2-m3 模型 ID(SF 端点 + 本机 CrossEncoder 同字面)。
RERANK_MODEL_ID = "BAAI/bge-reranker-v2-m3"
#: 客户端 XLM-R 预截断阈值 — 与本机 bge-m3 ``max_length=512`` 锁同字面
#: (issues/144 不变量 §1:"每个 chunk 在编码前按 XLM-R tokenizer 截到前 512 token
#: ``max_length=512``"。与存量 3977 chunks 的物理编码边界对齐)。
#:
#: 备注:SF 端实测拒绝 ≥ ~7000 token 的极端 chunk(HTTP 400 code 20015,T4
#: §5.2 实测 74421 字符被拒),但本地路径已经是 512;保持 512 = 锁同边界,
#: 跨 provider 一致性 ≠ 服务端限制由前端薄包硬编码。
MAX_TOKENS_PER_CHUNK = 512
#: 批量 embedding 单次最多条数(SF API 文档建议)。
EMBED_BATCH_SIZE = 32


# 代理锁的物理位置在 ``core.settings`` —— 沿用 ``make_deepseek_client`` /
# ``get_llm`` deepseek 分支已经持有的同一把锁,避免再多创建一把(issues/144
# Standards review smell: "Duplicated Code - proxy lock")。
from core.settings import _proxy_env_lock  # noqa: F401  (re-export for向后兼容)


def _strip_proxy_env():
    """临时移除 ALL_PROXY / all_proxy,httpx.Client() 初始化时不再读代理配置。

    必须在 ``_proxy_env_lock`` 内调用,完成后用 ``finally`` 恢复原值。
    """
    return (
        os.environ.pop("ALL_PROXY", None),
        os.environ.pop("all_proxy", None),
    )


def _restore_proxy_env(saved):
    """_strip_proxy_env 的反向操作。"""
    orig_all, orig_lower = saved
    if orig_all is not None:
        os.environ["ALL_PROXY"] = orig_all
    if orig_lower is not None:
        os.environ["all_proxy"] = orig_lower


# ── OpenAI SDK 客户端(embedding 走这条)──────────────────────────────────────────


def make_siliconflow_client(*, timeout: int = 60) -> "OpenAI":
    """构造 SiliconFlow OpenAI SDK 客户端(与 make_deepseek_client 同模式)。

    代理绕过:
    - ``httpx.Client(trust_env=False)`` 绕开 SOCKS 代理干扰;
    - 在 ``core.settings._proxy_env_lock`` 下临时摘除 ``ALL_PROXY/all_proxy``,
      避免 ``httpx.Client()`` 初始化时再读一次环境变量 —— 与四处现有 bypass
      共享同一把锁(get_llm deepseek / make_deepseek_client / _create_safe_ollama)。

    行为不变量:
    - 不调 ``normalize_l2``(T3 §1.3 实测 SF 已归一化);
    - 不主动加/剥 query instruction 前缀(T3 §3.3 实测 SF 不偷加)。
    """
    from openai import OpenAI
    import httpx

    api_key = os.environ.get("SILICONFLOW_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "SILICONFLOW_API_KEY 未设置;无法构造 SiliconFlow 客户端。"
            "请在 .env 配置 key 或切回本地 bge-m3(EMBED_PROVIDER=local)。"
        )
    base_url = os.environ.get(
        "SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"
    ).rstrip("/")
    http_client = httpx.Client(trust_env=False, timeout=httpx.Timeout(timeout))

    with _proxy_env_lock:
        saved = _strip_proxy_env()
        try:
            return OpenAI(api_key=api_key, base_url=base_url, http_client=http_client)
        finally:
            _restore_proxy_env(saved)


# ── XLM-R tokenizer 懒加载(只为预截断,不依赖 torch)─────────────────────────────


_tokenizer_lock = threading.Lock()
_tokenizer_singleton = None
#: 默认模型本地路径(优先)或 HF 名(回退)。本机没有 GPU 也能跑 tokenizer。
_DEFAULT_TOKENIZER_PATH = "BAAI/bge-m3"


def _get_xlmr_tokenizer():
    """懒加载 XLM-R tokenizer(只为 ~7000 token 截断,不加载 transformer 模型)。

    sentence-transformers 5.x 把 tokenizer 分离到独立 ``tokenizers`` 包,无需 torch。
    找不到本地缓存时按模型名构造(HF_HUB_OFFLINE=1 时会失败并清晰报错)。
    """
    global _tokenizer_singleton
    if _tokenizer_singleton is not None:
        return _tokenizer_singleton
    with _tokenizer_lock:
        if _tokenizer_singleton is not None:
            return _tokenizer_singleton
        try:
            from transformers import AutoTokenizer
        except ImportError as e:
            raise RuntimeError(
                "transformers 包未安装,无法执行 XLM-R 预截断。"
                "请安装 transformers 或设为 EMBED_PROVIDER=local。"
            ) from e
        # 优先用 ModelScope 本地缓存(本机无外网时仍能截断)
        modelscope_path = os.path.expanduser(
            f"~/.cache/modelscope/hub/{EMBED_MODEL_ID}"
        )
        path = modelscope_path if os.path.isdir(modelscope_path) else _DEFAULT_TOKENIZER_PATH
        _tokenizer_singleton = AutoTokenizer.from_pretrained(path)
    return _tokenizer_singleton


def truncate_to_max_tokens(text: str, max_tokens: int = MAX_TOKENS_PER_CHUNK) -> str:
    """把 ``text`` 按 XLM-R tokenizer 截到前 ``max_tokens`` token(锁的不变量 §1)。

    截断点按 token 边界取整 → 字符位置 → 子串(避免半个 token 送 SF)。
    失败时降级为字符截断(防 tokenizer 加载失败就索引完全瘫痪)。
    """
    if not text:
        return text
    try:
        tokenizer = _get_xlmr_tokenizer()
        ids = tokenizer.encode(text, add_special_tokens=False)
        if len(ids) <= max_tokens:
            return text
        truncated_ids = ids[:max_tokens]
        # decode 一遍 tokenizer,得到"完整"token 的字符串形式 —— 比按字符位置切更稳
        truncated_text = tokenizer.decode(truncated_ids, skip_special_tokens=True)
        return truncated_text
    except Exception as e:
        _logger.warning(
            "XLM-R tokenizer truncate failed (%s); 降级为字符截断 (~4 字符/token 上限)",
            e,
        )
        # 降级:按 ~4 字符/token 估算,留余量
        char_limit = max_tokens * 4
        return text[:char_limit]


def truncate_batch(texts: list[str], max_tokens: int = MAX_TOKENS_PER_CHUNK) -> list[str]:
    """对一组文本逐个执行 ``truncate_to_max_tokens``。"""
    return [truncate_to_max_tokens(t, max_tokens) for t in texts]


# ── Embedding 调用(T3 §1 / T4 §2 硬约束)────────────────────────────────────────


def _embed_with_siliconflow(texts: list[str]) -> list[list[float]]:
    """调用 SF embeddings.create,返回与输入等长的向量列表。

    内部会按 ``MAX_TOKENS_PER_CHUNK`` 预截断;**不调 normalize_l2**(T3 §1.3);
    **不加/剥 query instruction 前缀**(T3 §3.3)。
    """
    if not texts:
        return []
    truncated = truncate_batch(texts)
    client = make_siliconflow_client()
    resp = client.embeddings.create(model=EMBED_MODEL_ID, input=truncated)
    return [list(d.embedding) for d in resp.data]


def encode_for_siliconflow(texts: list[str]) -> list[list[float]]:
    """批量 embedding 入口(给 ``index_document`` / ``index_documents_batch`` 用)。

    调用方契约:
    - 输入是节点文本(分块后、已 ``truncate_to_max_tokens`` 友好);
    - 输出与输入等长,每个元素是 ``list[float]`` 长度 ``EMBEDDING_DIM``(1024);
    - 失败(SF 异常)向上抛,由调用方的 tenacity 重试层统一处理。
    """
    return _embed_with_siliconflow(texts)


def encode_query_for_siliconflow(query: str) -> list[float]:
    """单条 query embedding(给 ``search()`` / ``retriever.retrieve()`` 用)。

    与 ``encode_for_siliconflow([query])[0]`` 等价,但少一层 list 包装,
    避免 retriever 路径上不必要的 list 重建。
    """
    return _embed_with_siliconflow([query])[0]


# ── Rerank 适配器(T3 §2 / T4 §4 schema)─────────────────────────────────────────


def rerank_with_siliconflow(
    nodes: list,
    query: str,
    *,
    top_n: int | None = None,
    model: str = RERANK_MODEL_ID,
    timeout: int = 60,
) -> list:
    """SiliconFlow 在线 rerank(给 ``run_reranker()`` 调用)。

    Args:
        nodes: LlamaIndex NodeWithScore 列表(同本机 run_reranker 语义)。
        query: 查询字符串。
        top_n: 截前 N 条。None 时取 SF 默认 5(``RERANKER_TOP_N`` 环境变量可覆盖)。
        model: SF rerank 模型,默认 bge-reranker-v2-m3。
        timeout: HTTP 超时。

    Returns:
        重排序后的 NodeWithScore 列表(取 top_n),失败时返回原列表。

    行为不变量:
    - relevance_score ∈ [0, 1];**不可**直接拿本机 cross-encoder 阈值迁移(T3 §2.3)。
    - 同一 query+docs 调用是 deterministic(T3 §2.3),调试阈值可重复。
    """
    if not nodes:
        return nodes
    if top_n is None:
        top_n = int(os.environ.get("RERANKER_TOP_N", "5"))

    api_key = os.environ.get("SILICONFLOW_API_KEY", "")
    if not api_key:
        _logger.warning("SILICONFLOW_API_KEY 未设置;rerank 走原排序")
        return nodes
    base_url = os.environ.get(
        "SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"
    ).rstrip("/")

    docs = [n.node.text or "" for n in nodes]
    payload = {
        "model": model,
        "query": query,
        "documents": docs,
        "return_documents": True,
        "top_n": top_n,
        "max_chunks_per_doc": 1024,
        "overlap_tokens": 0,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    http_client = make_siliconflow_http_client(timeout=timeout)
    try:
        with _proxy_env_lock:
            saved = _strip_proxy_env()
            try:
                resp = http_client.post(
                    f"{base_url}/rerank", json=payload, headers=headers
                )
            finally:
                _restore_proxy_env(saved)
        resp.raise_for_status()
        body = resp.json()
        results = body.get("results") or []
        # results 已按 relevance_score 降序、按 index 标识原位置
        indexed = sorted(
            results, key=lambda r: r.get("relevance_score", 0.0), reverse=True
        )
        indexed = indexed[:top_n]
        # 用 SF 分数覆盖原 node.score,按 SF 排序取 top_n
        original_by_idx = list(nodes)
        reranked: list = []
        for r in indexed:
            idx = int(r.get("index", -1))
            if 0 <= idx < len(original_by_idx):
                n = original_by_idx[idx]
                n.score = float(r.get("relevance_score", 0.0))
                reranked.append(n)
        return reranked if reranked else nodes[:top_n]
    except Exception as e:
        _logger.warning("SiliconFlow rerank failed, fallback to raw ranking: %s", e)
        return nodes


# ── LlamaIndex 适配器(BaseEmbedding)─────────────────────────────────────────────


class SiliconFlowEmbedding(BaseEmbedding):
    """LlamaIndex ``embed_model`` 适配器 —— 后端实为 SiliconFlow。

    只实现 ``BaseEmbedding`` 的同步私有方法(``_get_query_embedding`` /
    ``_get_text_embedding`` / ``_get_text_embeddings``)和 async 等价版本。
    异步版本在本仓几乎用不到,但 BaseEmbedding 要求实现。

    字段:
        model_name: 字面量 ``BAAI/bge-m3``,与本机 HF embedding 类一致;
                    让 ``Settings.embed_model.model_name`` 查得到。
    """

    model_name: str = EMBED_MODEL_ID
    embed_dim: int = EMBEDDING_DIM

    def __init__(self, *, model_name: str = EMBED_MODEL_ID, embed_dim: int = EMBEDDING_DIM, **kwargs):
        # LlamaIndex BaseEmbedding 用 pydantic 字段验证,显式设字段
        kwargs.pop("model_name", None)
        kwargs.pop("embed_dim", None)
        super().__init__(model_name=model_name, embed_dim=embed_dim, **kwargs)
        # 同步简化字段(BaseEmbedding 已暴露 model_name)

    # ── 同步实现(SiliconFlow 主路径)────────────────────────────────────────

    def _get_query_embedding(self, query: str) -> list[float]:
        return encode_query_for_siliconflow(query)

    def _get_text_embedding(self, text: str) -> list[float]:
        return encode_query_for_siliconflow(text)

    def _get_text_embeddings(self, texts: list[str]) -> list[list[float]]:
        return encode_for_siliconflow(texts)

    # ── 异步实现(占位,同步包裹)────────────────────────────────────────────

    async def _aget_query_embedding(self, query: str) -> list[float]:
        return self._get_query_embedding(query)

    async def _aget_text_embedding(self, text: str) -> list[float]:
        return self._get_text_embedding(text)

    async def _aget_text_embeddings(self, texts: list[str]) -> list[list[float]]:
        return self._get_text_embeddings(texts)


# 公共导出
__all__ = [
    "EMBED_MODEL_ID",
    "EMBEDDING_DIM",
    "RERANK_MODEL_ID",
    "MAX_TOKENS_PER_CHUNK",
    "make_siliconflow_client",
    "make_siliconflow_http_client",
    "truncate_to_max_tokens",
    "truncate_batch",
    "encode_for_siliconflow",
    "encode_query_for_siliconflow",
    "rerank_with_siliconflow",
    "SiliconFlowEmbedding",
]
