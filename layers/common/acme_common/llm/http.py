"""Minimal HTTPS JSON client on the standard library.

No vendor SDKs in the layer: two POST endpoints don't justify ~30 MB of dependencies and the
cold-start cost. Status codes are mapped onto the retryable / non-retryable error taxonomy.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from acme_common.llm.errors import (
    LLMRequestError,
    LLMResponseError,
    LLMThrottledError,
    LLMUnavailableError,
)

urlopen = urllib.request.urlopen  # indirection so tests can patch it


def post_json(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float) -> Any:
    if not url.startswith("https://"):
        raise LLMRequestError("refusing non-HTTPS endpoint")
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json", "accept": "application/json", **headers},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode(errors="replace") if exc.fp else ""
        if exc.code == 429:
            raise LLMThrottledError(f"HTTP 429: {detail}") from exc
        if exc.code == 529 or exc.code >= 500:  # 529 = Anthropic "overloaded"
            raise LLMUnavailableError(f"HTTP {exc.code}: {detail}") from exc
        raise LLMRequestError(f"HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise LLMUnavailableError(f"transport error: {exc}") from exc

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMResponseError("provider returned non-JSON body") from exc
