"""LLM error taxonomy.

The class names matter: Lambda reports the exception class as `errorType`, and the state
machine's Retry blocks match on these names (see statemachine/triage.asl.json).
"""


class LLMError(Exception):
    """Base class - not retried by the state machine."""


class LLMThrottledError(LLMError):
    """HTTP 429 / provider overloaded. Retried with exponential backoff + jitter."""


class LLMUnavailableError(LLMError):
    """HTTP 5xx, timeouts, connection errors. Retried with exponential backoff + jitter."""


class LLMResponseError(LLMError):
    """Response was not valid JSON / failed the schema / was truncated. Retried once."""


class LLMRequestError(LLMError):
    """Non-retryable 4xx (bad request, auth). Goes straight to FAILED."""


class LLMConfigurationError(LLMError):
    """Missing provider / key configuration."""
