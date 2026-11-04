"""Builds the configured LLMClient once per execution environment (cold start)."""

from __future__ import annotations

import os

from acme_common.llm.anthropic_client import AnthropicClient
from acme_common.llm.base import LLMClient
from acme_common.llm.errors import LLMConfigurationError
from acme_common.llm.fake import FakeLLM
from acme_common.llm.openai_client import OpenAIClient
from acme_common.secrets import get_secret

# Defaults are deliberately small/fast models - triage is a high-volume, latency-sensitive task.
DEFAULT_MODELS = {
    "fake": "fake-heuristic-1",
    "openai": "gpt-4.1-mini",
    "anthropic": "claude-haiku-4-5",
}

_client: LLMClient | None = None


def build_client(
    provider: str, model: str | None = None, api_key_param: str = "", timeout: float = 30.0
) -> LLMClient:
    provider = (provider or "fake").lower()
    if provider not in DEFAULT_MODELS:
        raise LLMConfigurationError(f"unknown LLM provider '{provider}'")
    model = model or DEFAULT_MODELS[provider]
    if provider == "fake":
        return FakeLLM(model=model)
    api_key = get_secret(api_key_param)
    if provider == "openai":
        return OpenAIClient(model=model, api_key=api_key, timeout=timeout)
    return AnthropicClient(model=model, api_key=api_key, timeout=timeout)


def get_llm_client() -> LLMClient:
    global _client
    if _client is None:
        _client = build_client(
            provider=os.environ.get("LLM_PROVIDER", "fake"),
            model=os.environ.get("LLM_MODEL") or None,
            api_key_param=os.environ.get("LLM_API_KEY_PARAM", ""),
            timeout=float(os.environ.get("LLM_TIMEOUT_SECONDS", "30")),
        )
    return _client


def set_llm_client(client: LLMClient | None) -> None:
    """Test hook: inject a client (or reset with None)."""
    global _client
    _client = client
