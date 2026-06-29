"""core.settings.make_deepseek_client 的构造测试。

只验证 client 构造（base_url / trust_env / proxy 恢复），不发任何 API 请求、不加载模型。
"""
import os

import httpx
import pytest
from openai import OpenAI

from core.settings import make_deepseek_client


def test_base_url_from_env(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://example.deepseek.test/v1/")
    client = make_deepseek_client()
    assert isinstance(client, OpenAI)
    # rstrip("/") 与 settings.py 的 DeepSeek provider 一致
    assert str(client.base_url).rstrip("/").endswith("example.deepseek.test/v1")


def test_default_base_url(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    client = make_deepseek_client()
    assert str(client.base_url).rstrip("/").endswith("api.deepseek.com/v1")


def test_httpx_client_trust_env_false(monkeypatch):
    """构造底层 httpx client 时必须传 trust_env=False（禁用环境代理）。"""
    captured = {}
    real = httpx.Client

    class _Spy(real):
        def __init__(self, **kwargs):
            captured.update(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "Client", _Spy)
    make_deepseek_client()
    assert captured.get("trust_env") is False


def test_all_proxy_restored_after_construction(monkeypatch):
    """proxy-bypass dance 必须在构造后恢复 ALL_PROXY 原值。"""
    monkeypatch.setenv("ALL_PROXY", "http://test-proxy:9999")
    make_deepseek_client()
    assert os.environ.get("ALL_PROXY") == "http://test-proxy:9999"


def test_no_network_call(monkeypatch):
    """即便设置了恶意代理，trust_env=False 且不调用 .chat.* 即不发请求、不卡住。"""
    monkeypatch.setenv("ALL_PROXY", "http://evil-proxy:1")
    client = make_deepseek_client()
    assert client is not None
