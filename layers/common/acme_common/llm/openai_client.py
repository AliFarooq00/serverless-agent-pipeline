"""OpenAI Chat Completions with strict JSON-schema structured output."""

from __future__ import annotations

import json
from typing import Any

from acme_common.llm import http
from acme_common.llm.base import LLMClient, RawCompletion
from acme_common.llm.errors import LLMResponseError

API_URL = "https://api.openai.com/v1/chat/completions"


class OpenAIClient(LLMClient):
    provider = "openai"

    def __init__(self, model: str, api_key: str, timeout: float = 30.0):
        super().__init__(model)
        self._api_key = api_key
        self._timeout = timeout

    def __repr__(self) -> str:  # never leak the key into logs
        return f"OpenAIClient(model={self.model!r})"

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
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_completion_tokens": max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": schema, "strict": True},
            },
        }
        resp = http.post_json(
            API_URL, {"authorization": f"Bearer {self._api_key}"}, payload, self._timeout
        )
        try:
            choice = resp["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMResponseError("unexpected OpenAI response shape") from exc
        if choice.get("finish_reason") == "length":
            raise LLMResponseError("OpenAI output truncated (max tokens)")
        if message.get("refusal"):
            raise LLMResponseError(f"OpenAI refused: {message['refusal'][:200]}")
        try:
            data = json.loads(message.get("content") or "")
        except json.JSONDecodeError as exc:
            raise LLMResponseError("OpenAI content was not JSON") from exc
        usage = resp.get("usage") or {}
        return RawCompletion(
            data=data,
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
        )
