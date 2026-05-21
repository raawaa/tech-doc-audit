"""LlamaIndex 全局配置。

所有 embedding / chunking / LLM 配置在此处统一管理。
Settings 会在首次 import 时自动配置。
"""

import os

from llama_index.core import Settings
from llama_index.core.node_parser import SentenceSplitter


def _init():
    Settings.transformations = [
        SentenceSplitter(chunk_size=512, chunk_overlap=50),
    ]
    Settings.chunk_size = 512
    Settings.chunk_overlap = 50


_init()


def get_embed_model():
    """延迟加载 embedding 模型（首次调用时加载 ~2GB bge-m3）。"""
    if Settings.embed_model is None:
        from llama_index.embeddings.huggingface import HuggingFaceEmbedding
        Settings.embed_model = HuggingFaceEmbedding(
            model_name="BAAI/bge-m3",
            normalize_embeddings=True,
        )
    return Settings.embed_model


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
