"""Unified LLM client — supports both Ollama and OpenAI-compatible APIs.

Configure via environment variables:

  LLM_PROVIDER=ollama                # (default) Use local Ollama
  OLLAMA_BASE_URL=http://localhost:11434
  OLLAMA_MODEL=qwen3.5:0.8b

  LLM_PROVIDER=minimax               # Use MiniMax CN (OpenAI-compatible)
  MINIMAX_CN_API_KEY=sk-...
  MINIMAX_CN_MODEL=MiniMax-M2.7      # or MiniMax-M2.5

  LLM_PROVIDER=openai                # Any OpenAI-compatible API
  OPENAI_API_KEY=sk-...
  OPENAI_BASE_URL=https://api.xxx.com/v1
  OPENAI_MODEL=gpt-4o-mini
"""

import json
import os
import httpx
from typing import Optional


def _detect_provider() -> str:
    """Auto-detect provider from env, default to ollama."""
    return os.environ.get("LLM_PROVIDER", "ollama").lower().strip()


def _get_ollama_config() -> tuple[str, str]:
    return (
        os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
        os.environ.get("OLLAMA_MODEL", "qwen3.5:0.8b"),
    )


def _get_minimax_config() -> tuple[str, str, str]:
    return (
        os.environ.get("MINIMAX_CN_BASE_URL", "https://api.minimaxi.com/v1"),
        os.environ.get("MINIMAX_CN_API_KEY", ""),
        os.environ.get("MINIMAX_CN_MODEL", "MiniMax-M2.7"),
    )


def _get_openai_config() -> tuple[str, str, str]:
    return (
        os.environ.get("OPENAI_BASE_URL", ""),
        os.environ.get("OPENAI_API_KEY", ""),
        os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
    )


def _is_openai_compatible() -> bool:
    """Check if the current provider uses the OpenAI-compatible API."""
    return _detect_provider() in ("minimax", "minimax-cn", "openai")


# =============================================================================
# Simple text generation (existing API, kept for backward compat)
# =============================================================================

def generate(prompt: str, *, system_prompt: Optional[str] = None,
             timeout: int = 180, max_tokens: Optional[int] = None,
             response_format: Optional[str] = None) -> str:
    """Send a prompt to the configured LLM and return the response text.

    Args:
        prompt: User message / instruction.
        system_prompt: Optional system message (placed first for prompt caching).
        timeout: Request timeout in seconds.
        max_tokens: Max output tokens.
        response_format: Set to "json" to force valid JSON output.
    """
    provider = _detect_provider()
    if provider == "ollama":
        return _generate_ollama(prompt, timeout)
    elif provider in ("minimax", "minimax-cn"):
        return _generate_openai_like(prompt, system_prompt, timeout, provider="minimax",
                                     max_tokens=max_tokens, response_format=response_format)
    elif provider == "openai":
        return _generate_openai_like(prompt, system_prompt, timeout, provider="openai",
                                     max_tokens=max_tokens, response_format=response_format)
    else:
        raise ValueError(f"Unknown LLM_PROVIDER: {provider}")


def _generate_ollama(prompt: str, timeout: int) -> str:
    url, model = _get_ollama_config()
    try:
        resp = httpx.post(
            f"{url}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json().get("response", "")
    except Exception as e:
        raise RuntimeError(f"Ollama request failed: {e}")


def _generate_openai_like(prompt: str, system_prompt: Optional[str], timeout: int,
                           provider: str, max_tokens: Optional[int] = None,
                           response_format: Optional[str] = None) -> str:
    """Send a prompt via OpenAI-compatible /v1/chat/completions.

    System prompt is sent as a separate ``system`` message (cache-friendly).
    """
    base_url, api_key, model = _resolve_config(provider)
    if not api_key:
        raise RuntimeError(f"{provider.upper()}_API_KEY not set")

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    body = _build_body(model, messages, max_tokens, response_format, provider)

    return _post_chat(base_url, api_key, body, timeout)


# =============================================================================
# Tool / Function Calling API
# =============================================================================

def generate_with_tools(
    messages: list[dict],
    tools: list[dict],
    *,
    tool_choice: Optional[str | dict] = "auto",
    timeout: int = 180,
    max_tokens: Optional[int] = None,
) -> dict:
    """Send messages + tool definitions and return the parsed response.

    Args:
        messages: Full message list (system + user + assistant + tool).
        tools: OpenAI-format tool definitions.
        tool_choice: ``"auto"``, ``"required"``, ``"none"``, or ``{"type": "function", "function": {"name": "..."}}``.
        timeout: Request timeout.
        max_tokens: Max output tokens.

    Returns:
        A dict with either:
        - ``{"type": "tool_calls", "tool_calls": [{"name": ..., "arguments": {...}}, ...]}``
        - ``{"type": "text", "content": "..."}``  (when tool_choice="none" or model declines)
    """
    provider = _detect_provider()
    if provider == "ollama":
        return _generate_with_tools_ollama(messages, tools, tool_choice, timeout)

    base_url, api_key, model = _resolve_config(provider)
    if not api_key:
        raise RuntimeError(f"{provider.upper()}_API_KEY not set")

    body = _build_body(model, messages, max_tokens, response_format=None, provider=provider)
    body["tools"] = tools
    if tool_choice is not None:
        body["tool_choice"] = tool_choice

    raw = _post_chat(base_url, api_key, body, timeout)
    return _parse_tool_response(raw)


def _parse_tool_response(raw_response: str) -> dict:
    """Parse the raw API response string into a structured dict."""
    data = json.loads(raw_response)
    msg = data["choices"][0]["message"]

    if msg.get("tool_calls"):
        calls = []
        for tc in msg["tool_calls"]:
            calls.append({
                "name": tc["function"]["name"],
                "arguments": json.loads(tc["function"]["arguments"]),
            })
        return {"type": "tool_calls", "tool_calls": calls}

    return {"type": "text", "content": msg.get("content", "")}


def _generate_with_tools_ollama(
    messages: list[dict],
    tools: list[dict],
    tool_choice,
    timeout: int,
) -> dict:
    """Fallback: Ollama doesn't support tools in the same way.
    Convert to a text prompt with JSON instruction instead.
    """
    # Extract the last user message and prepend tool instructions
    user_msg = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    tool_schemas = json.dumps(tools, ensure_ascii=False, indent=2)
    prompt = f"""{user_msg}

You MUST output the result as a valid JSON object matching one of the following function schemas.
Select the most appropriate function and output ONLY the arguments JSON object.

Available functions:
{tool_schemas}

Output ONLY the JSON object that would be passed as arguments to the selected function.
Do NOT include any other text, explanation, or markdown."""

    system = next((m["content"] for m in messages if m["role"] == "system"), None)
    text = generate(prompt, system_prompt=system, timeout=timeout, response_format="json")
    # Try to extract JSON from the response (may have think tags)
    import re
    match = re.search(r'\{[\s\S]*\}', text)
    if match:
        try:
            args = json.loads(match.group())
            name = tools[0]["function"]["name"]
            return {"type": "tool_calls", "tool_calls": [{"name": name, "arguments": args}]}
        except json.JSONDecodeError:
            pass
    return {"type": "text", "content": text}


# =============================================================================
# Shared helpers
# =============================================================================

def _resolve_config(provider: str) -> tuple[str, str, str]:
    if provider in ("minimax", "minimax-cn"):
        return _get_minimax_config()
    return _get_openai_config()


def _build_body(
    model: str,
    messages: list[dict],
    max_tokens: Optional[int],
    response_format: Optional[str],
    provider: str,
) -> dict:
    body: dict = {"model": model, "messages": messages, "stream": False}
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    if provider in ("minimax", "minimax-cn"):
        # MiniMax 推理 token 消耗大，默认开大一点
        body.setdefault("max_tokens", 8192)
    if response_format == "json":
        body["response_format"] = {"type": "json_object"}
    return body


def _post_chat(base_url: str, api_key: str, body: dict, timeout: int) -> str:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        resp = httpx.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json=body,
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        raise RuntimeError(f"API request failed: {e}")
