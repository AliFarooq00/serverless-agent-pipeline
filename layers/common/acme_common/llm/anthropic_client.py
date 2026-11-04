"""Anthropic Messages API; structured output via a forced tool call."""

from __future__ import annotations

from typing import Any

from acme_common.llm import http
from acme_common.llm.base import LLMClient, RawCompletion
from acme_common.llm.errors import LLMResponseError

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"


class AnthropicClient(LLMClient):
    provider = "anthropic"

    def __init__(self, model: str, api_key: str, timeout: float = 30.0):
        super().__init__(model)
        self._api_key = api_key
        self._timeout = timeout

    def __repr__(self) -> str:
        return f"AnthropicClient(model={self.model!r})"

    def _complete(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str,
        max_tokens: int,
    ) -> RawCompletion:
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "tools": [
                {
                    "name": schema_name,
                    "description": f"Record the {schema_name} result. Always call this tool.",
                    "input_schema": schema,
                }
            ],
            "tool_choice": {"type": "tool", "name": schema_name},
        }
        resp = http.post_json(
            API_URL,
            {"x-api-key": self._api_key, "anthropic-version": API_VERSION},
            payload,
            self._timeout,
        )
        if resp.get("stop_reason") == "max_tokens":
            raise LLMResponseError("Anthropic output truncated (max tokens)")
        tool_use = next(
            (
                block
                for block in resp.get("content") or []
                if block.get("type") == "tool_use" and block.get("name") == schema_name
            ),
            None,
        )
        if tool_use is None:
            raise LLMResponseError("Anthropic response had no tool_use block")
        usage = resp.get("usage") or {}
        return RawCompletion(
            data=tool_use.get("input"),
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
        )
