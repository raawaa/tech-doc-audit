"""LlamaIndex 全局配置。

所有 embedding / chunking / LLM / 可观测性 在此处统一管理。
Settings 会在首次 import 时自动配置。
"""

import os
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

from llama_index.core import Settings
from llama_index.core.node_parser import SentenceSplitter
from llama_index.core.callbacks import CallbackManager, LlamaDebugHandler


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


def get_embed_model():
    """延迟加载 embedding 模型（首次调用时加载 ~2GB bge-m3）。"""
    global _embed_model
    if _embed_model is not None:
        return _embed_model
    from llama_index.embeddings.huggingface import HuggingFaceEmbedding
    _modelscope_path = os.path.expanduser("~/.cache/modelscope/hub/BAAI/bge-m3")
    if os.path.isdir(_modelscope_path):
        # ModelScope 本地缓存优先（无网络环境）
        _model_path = _modelscope_path
    else:
        _model_path = "BAAI/bge-m3"
    _embed_model = HuggingFaceEmbedding(
        model_name=_model_path,
        normalize=True,
        device=os.getenv("EMBED_DEVICE", None),
        embed_batch_size=2,   # 默认 10，减少为 2 以降低峰值内存 ~80%
        max_length=512,       # 匹配 chunk_size，防止超长序列拉高内存
    )
    Settings.embed_model = _embed_model
    return _embed_model


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
        base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
        # DeepSeek 模型名不在 OpenAI 白名单中，注册到校验列表避免报错
        from llama_index.llms.openai import utils as _openai_utils
        _openai_utils.ALL_AVAILABLE_MODELS[model] = 128000
        _openai_utils.CHAT_MODELS[model] = 128000
        _llm = OpenAILLM(
            model=model, api_base=base_url, api_key=api_key,
            request_timeout=timeout, is_chat_model=True,
        )
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
