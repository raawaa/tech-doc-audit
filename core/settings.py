"""LlamaIndex 全局配置。

所有 embedding / chunking / LLM / 可观测性 在此处统一管理。
Settings 会在首次 import 时自动配置。
"""

import os
import threading
from pathlib import Path

from dotenv import load_dotenv

# 在 import 时自动加载项目根目录的 .env
_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    load_dotenv(_env_path)

# ── 内存控制 ──────────────────────────────────────────────────────────────────
# 在导入 PyTorch / sentence-transformers 前设置，控制 CPU 线程数以减少内存峰值
os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("MKL_NUM_THREADS", "2")
# huggingface_hub 离线模式：禁止 HEAD 请求 huggingface.co 检查文件元数据。
# 本机无法直连 HF，默认离线以 ModelScope 本地缓存优先；如需首次下载模型，
# 在 .env 中设 HF_HUB_OFFLINE=0 + HF_ENDPOINT=https://hf-mirror.com。
# 下载完成后切回 1（或不设，走默认）。
os.environ.setdefault("HF_HUB_OFFLINE", "1")

# ── 文件上传大小限制 ──────────────────────────────────────────────────────────
# 默认 100MB，通过 MAX_UPLOAD_SIZE_MB 环境变量可调整
MAX_UPLOAD_SIZE = int(os.environ.get("MAX_UPLOAD_SIZE_MB", "100")) * 1024 * 1024

from llama_index.core import Settings
from llama_index.core.node_parser import SentenceSplitter
from llama_index.core.callbacks import CallbackManager, LlamaDebugHandler
from core.logger import get_logger

_logger = get_logger(__name__)


def _init():
    Settings.transformations = [
        SentenceSplitter(chunk_size=512, chunk_overlap=50),
    ]
    Settings.chunk_size = 512
    Settings.chunk_overlap = 50

    # 全局 CallbackManager：追踪 LLM 调用耗时与 token 用量
    debug_handler = LlamaDebugHandler()
    Settings.callback_manager = CallbackManager([debug_handler])

    # 将 callback_manager 关联到全局 Settings
    # 后续 get_llm() 创建的 LLM 实例会自动继承此 callback_manager


_init()


_embed_model = None
_embed_model_lock = threading.Lock()
# 全局 GPU 推理锁：HuggingFaceEmbedding / SentenceTransformerRerank 的 GPU
# forward 非线程安全。此锁确保同时只有一个线程执行模型前向传播，
# 避免 N 个并发线程各自分配完整激活张量撑爆显存。
_gpu_inference_lock = threading.RLock()

# 代理环境变量操作锁：_create_safe_ollama 和 deepseek provider 需要临时
# 移除 ALL_PROXY 环境变量。此锁确保移除→恢复期间不会有其他线程读到中间状态。
_proxy_env_lock = threading.Lock()


def get_gpu_inference_lock() -> threading.RLock:
    """获取全局 GPU 推理锁。"""
    return _gpu_inference_lock


def get_embed_model():
    """线程安全地延迟加载 embedding 模型（首次调用时加载 ~2GB bge-m3）。

    get_embed_model 可能被多个线程同时调用（例如 ThreadPoolExecutor
    中 8 个 topic audit 并行），必须保证模型只被加载一次。

    加载失败时降级为 None（与 get_reranker() 行为一致），不抛异常。
    上游调用方（index_manager 等）需检查返回值并处理。
    """
    global _embed_model
    if _embed_model is not None:
        return _embed_model
    with _embed_model_lock:
        # 双检锁：避免多个线程同时进入后各自加载一份模型
        if _embed_model is not None:
            return _embed_model
        from llama_index.embeddings.huggingface import HuggingFaceEmbedding
        _modelscope_path = os.path.expanduser("~/.cache/modelscope/hub/BAAI/bge-m3")
        if os.path.isdir(_modelscope_path):
            # ModelScope 本地缓存优先（无网络环境）
            _model_path = _modelscope_path
        else:
            _model_path = "BAAI/bge-m3"
        try:
            _embed_model = HuggingFaceEmbedding(
                model_name=_model_path,
                normalize=True,
                device=os.getenv("EMBED_DEVICE", None),
                embed_batch_size=2,   # 默认 10，减少为 2 以降低峰值内存 ~80%
                max_length=512,       # 匹配 chunk_size，防止超长序列拉高内存
            )
            Settings.embed_model = _embed_model
        except Exception as e:
            _logger.warning(
                "embed_model init failed (%s). "
                "Please download bge-m3 first: "
                "modelscope download BAAI/bge-m3 --local_dir %s  "
                "or set HF_HUB_OFFLINE=0 HF_ENDPOINT=https://hf-mirror.com in .env",
                e, _modelscope_path,
            )
            _embed_model = None  # type: ignore[assignment]
    return _embed_model


# ── Reranker（按需加载→推理→卸载，避免与 embed 模型同时占用显存）──────────────


def get_reranker_config() -> dict:
    """返回 reranker 配置（不加载模型）。"""
    model_name = os.environ.get("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
    top_n = int(os.environ.get("RERANKER_TOP_N", "5"))
    device = os.environ.get("RERANKER_DEVICE") or os.environ.get("EMBED_DEVICE", None)
    # ModelScope 本地缓存优先
    path = os.path.expanduser(f"~/.cache/modelscope/hub/{model_name}")
    if os.path.isdir(path):
        model_name = path
    return {"model": model_name, "top_n": top_n, "device": device or "cuda"}


def run_reranker(nodes: list, query_str: str, config: dict | None = None) -> list:
    """按需加载 reranker → 推理 → 立即卸载释放显存。

    在 GPU 锁内调用（调用方负责持锁），确保不会与 embed 模型并发推理。
    加载 ~1.3s、推理 ~0.2s、卸载 ~0s，总开销可接受。

    Args:
        nodes: NodeWithScore 列表（LlamaIndex 格式）。
        query_str: 查询字符串。
        config: get_reranker_config() 返回的配置，None 则自动获取。

    Returns:
        重排序后的 NodeWithScore 列表（取 top_n），失败时返回原始列表。
    """
    if not nodes:
        return nodes
    if config is None:
        config = get_reranker_config()

    try:
        import gc
        import torch
        from sentence_transformers import CrossEncoder

        device = config["device"]
        try:
            ce = CrossEncoder(
                config["model"],
                device=device,
                trust_remote_code=True,
            )
        except Exception as gpu_err:
            # GPU 显存不足 → 降级到 CPU（较慢但可用）
            _logger.warning("reranker GPU load failed (%s), falling back to CPU", gpu_err)
            device = "cpu"
            ce = CrossEncoder(
                config["model"],
                device="cpu",
                trust_remote_code=True,
            )
        try:
            # 对 (query, node.text) 对打分
            pairs = [(query_str, n.node.text or "") for n in nodes]
            scores = ce.predict(pairs)

            # 用 cross-encoder 分数覆盖原始 score，按降序排序
            for node, score in zip(nodes, scores):
                node.score = float(score)
            nodes_sorted = sorted(nodes, key=lambda n: n.score or 0, reverse=True)
            return nodes_sorted[: config["top_n"]]
        finally:
            del ce
            gc.collect()
            torch.cuda.empty_cache()
    except Exception as e:
        _logger.warning("reranker on-demand load/predict failed, using raw ranking: %s", e)
        return nodes


# ── LLM ────────────────────────────────────────────────────────────────────────

_llm = None
_llm_lock = threading.Lock()


def get_llm():
    """延迟加载 LLM（支持 Ollama / OpenAI / MiniMax），线程安全。"""
    global _llm
    if _llm is not None:
        return _llm
    with _llm_lock:
        # 双检锁：避免多个线程同时进入后各自创建一份 LLM 实例
        if _llm is not None:
            return _llm

        provider = os.environ.get("LLM_PROVIDER", "ollama").lower().strip()
        timeout = int(os.environ.get("LLM_TIMEOUT", "180"))

        if provider == "ollama":
            base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
            model = os.environ.get("OLLAMA_MODEL", "qwen3.5:0.8b")
            _llm = _create_safe_ollama(model=model, base_url=base_url, timeout=timeout, temperature=0.0)
        elif provider in ("minimax", "minimax-cn"):
            from llama_index.llms.openai import OpenAI as OpenAILLM
            base_url = os.environ.get("MINIMAX_CN_BASE_URL", "https://api.minimaxi.com/v1").rstrip("/")
            api_key = os.environ.get("MINIMAX_CN_API_KEY", "")
            model = os.environ.get("MINIMAX_CN_MODEL", "MiniMax-M2.7")
            # MiniMax 模型名不在 OpenAI 白名单中，注册到校验列表避免报错
            from llama_index.llms.openai import utils as _openai_utils
            _openai_utils.ALL_AVAILABLE_MODELS[model] = 128000
            _openai_utils.CHAT_MODELS[model] = 128000
            _llm = OpenAILLM(
                model=model, api_base=base_url, api_key=api_key,
                request_timeout=timeout, is_chat_model=True,
                temperature=0.0,
            )
        elif provider == "openai":
            from llama_index.llms.openai import OpenAI as OpenAILLM
            base_url = os.environ.get("OPENAI_BASE_URL", "").rstrip("/")
            api_key = os.environ.get("OPENAI_API_KEY", "")
            model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
            _llm = OpenAILLM(
                model=model, api_base=base_url or None, api_key=api_key,
                request_timeout=timeout, is_chat_model=True,
                temperature=0.0,
            )
        elif provider == "deepseek":
            from llama_index.llms.openai import OpenAI as OpenAILLM
            import httpx
            base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
            api_key = os.environ.get("DEEPSEEK_API_KEY", "")
            model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
            # DeepSeek 模型名不在 OpenAI 白名单中，注册到校验列表避免报错
            from llama_index.llms.openai import utils as _openai_utils
            _openai_utils.ALL_AVAILABLE_MODELS[model] = 128000
            _openai_utils.CHAT_MODELS[model] = 128000
            # 创建 httpx client 时 trust_env=False，绕过 SOCKS 代理干扰
            # 与 _SafeOllama 的 trust_env=False 原理一致
            http_client = httpx.Client(trust_env=False, timeout=httpx.Timeout(timeout))
            # 临时移除代理环境变量防止 httpx Client() 初始化时读取
            # _proxy_env_lock 串行化此操作，确保移除→恢复期间无其他线程读到中间状态
            with _proxy_env_lock:
                _orig = os.environ.pop("ALL_PROXY", None)
                os.environ.pop("all_proxy", None)
                try:
                    _llm = OpenAILLM(
                        model=model, api_base=base_url, api_key=api_key,
                        request_timeout=timeout, is_chat_model=True,
                        http_client=http_client,
                        temperature=0.0,
                        # 禁用 DeepSeek 思考模式（thinking mode），
                        # 否则 as_structured_llm 的 tool_choice 参数报 400
                        additional_kwargs={'extra_body': {'thinking': {'type': 'disabled'}}},
                    )
                finally:
                    if _orig is not None:
                        os.environ["ALL_PROXY"] = _orig
        else:
            raise ValueError(f"Unknown LLM_PROVIDER: {provider}")

        Settings.llm = _llm

    return _llm


def _create_safe_ollama(*, model: str, base_url: str, timeout: int, temperature: float = 0.0):
    """创建绕过 SOCKS 代理问题的 Ollama LLM 实例。"""
    with _proxy_env_lock:
        _orig = os.environ.pop("ALL_PROXY", None)
        os.environ.pop("all_proxy", None)
        try:
            import ollama as _ollama
            from llama_index.llms.ollama import Ollama as _BaseOllama

            class _SafeOllama(_BaseOllama):
                """Ollama 子类，创建 httpx.Client 时禁用代理。"""

                @property
                def client(self):
                    if self._client is None:
                        self._client = _ollama.Client(
                            host=self.base_url,
                            timeout=self.request_timeout,
                            headers=self.headers or {},
                            trust_env=False,
                        )
                    return self._client

            return _SafeOllama(model=model, base_url=base_url, request_timeout=timeout, temperature=temperature)
        finally:
            if _orig is not None:
                os.environ["ALL_PROXY"] = _orig
