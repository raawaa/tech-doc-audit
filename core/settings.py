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


# ── Reranker ────────────────────────────────────────────────────────────────────

_reranker = None
_reranker_lock = threading.Lock()


def get_reranker():
    """线程安全地延迟加载 reranker 模型（用于检索结果重排序，提升精度）。

    Reranker 是一种 cross-encoder，对 query-doc 对做精确打分重排，
    弥补 bi-encoder（bge-m3）ANN 检索的精度损失。

    如果加载失败（模型未下载 / OOM / 依赖缺失），静默降级不阻断裂。
    """
    global _reranker
    if _reranker is not None:
        return _reranker
    with _reranker_lock:
        if _reranker is not None:
            return _reranker

    model_name = os.environ.get("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
    top_n = int(os.environ.get("RERANKER_TOP_N", "5"))
    # 默认自动检测 GPU（None → SentenceTransformerRerank 自动选 cuda 或 cpu）
    # 如果设置了 EMBED_DEVICE 但没单独设 RERANKER_DEVICE，跟随 EMBED_DEVICE
    device = os.environ.get("RERANKER_DEVICE") or os.environ.get("EMBED_DEVICE", None)

    try:
        # ModelScope 本地缓存优先（同 bge-m3 策略）
        _modelscope_path = os.path.expanduser(f"~/.cache/modelscope/hub/{model_name}")
        if os.path.isdir(_modelscope_path):
            model_name = _modelscope_path

        from llama_index.core.postprocessor import SentenceTransformerRerank
        _reranker = SentenceTransformerRerank(
            model=model_name,
            top_n=top_n,
            device=device,
            trust_remote_code=True,
        )
    except Exception as e:
        _logger.warning("reranker init failed (%s), degraded to raw ranking: %s", model_name, e)
        _reranker = None  # type: ignore[assignment]

    return _reranker


# ── LLM ────────────────────────────────────────────────────────────────────────

_llm = None


def get_llm():
    """延迟加载 LLM（支持 Ollama / OpenAI / MiniMax）。"""
    global _llm
    if _llm is not None:
        return _llm

    provider = os.environ.get("LLM_PROVIDER", "ollama").lower().strip()
    timeout = int(os.environ.get("LLM_TIMEOUT", "180"))

    if provider == "ollama":
        base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
        model = os.environ.get("OLLAMA_MODEL", "qwen3.5:0.8b")
        _llm = _create_safe_ollama(model=model, base_url=base_url, timeout=timeout)
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
        )
    elif provider == "openai":
        from llama_index.llms.openai import OpenAI as OpenAILLM
        base_url = os.environ.get("OPENAI_BASE_URL", "").rstrip("/")
        api_key = os.environ.get("OPENAI_API_KEY", "")
        model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        _llm = OpenAILLM(
            model=model, api_base=base_url or None, api_key=api_key,
            request_timeout=timeout, is_chat_model=True,
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
        _orig = os.environ.pop("ALL_PROXY", None)
        os.environ.pop("all_proxy", None)
        try:
            _llm = OpenAILLM(
                model=model, api_base=base_url, api_key=api_key,
                request_timeout=timeout, is_chat_model=True,
                http_client=http_client,
            )
        finally:
            if _orig is not None:
                os.environ["ALL_PROXY"] = _orig
    else:
        raise ValueError(f"Unknown LLM_PROVIDER: {provider}")

    Settings.llm = _llm
    return _llm


def _create_safe_ollama(*, model: str, base_url: str, timeout: int):
    """创建绕过 SOCKS 代理问题的 Ollama LLM 实例。"""
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

        return _SafeOllama(model=model, base_url=base_url, request_timeout=timeout)
    finally:
        if _orig is not None:
            os.environ["ALL_PROXY"] = _orig
