"""The one interface every agent talks to.

`complete_json` is a template method: providers implement `_complete` (one HTTP call), while
latency measurement and schema validation happen here, identically for every provider.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from typing import Any

from acme_common.llm.errors import LLMResponseError
from acme_common.llm.schema import SchemaError, validate


@dataclass(frozen=True)
class Usage:
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LLMResult:
    data: dict[str, Any]
    usage: Usage


@dataclass(frozen=True)
class RawCompletion:
    data: Any
    input_tokens: int
    output_tokens: int


class LLMClient(ABC):
    provider: str = "abstract"

    def __init__(self, model: str):
        self.model = model

    @abstractmethod
    def _complete(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str,
        max_tokens: int,
    ) -> RawCompletion:
        """Perform one structured-output call and return the parsed JSON object."""

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str,
        max_tokens: int = 800,
    ) -> LLMResult:
        started = time.perf_counter()
        raw = self._complete(
            system=system, user=user, schema=schema, schema_name=schema_name, max_tokens=max_tokens
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        try:
            validate(raw.data, schema)
        except SchemaError as exc:
            raise LLMResponseError(
                f"{schema_name}: output failed schema validation: {exc}"
            ) from exc
        return LLMResult(
            data=raw.data,
            usage=Usage(
                provider=self.provider,
                model=self.model,
                input_tokens=raw.input_tokens,
                output_tokens=raw.output_tokens,
                latency_ms=latency_ms,
            ),
        )
