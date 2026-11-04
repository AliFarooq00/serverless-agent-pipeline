"""API keys come from SSM Parameter Store SecureStrings - never from plaintext env vars.

Only the *parameter name* is in the environment. The value is fetched with decryption on first
use and cached in-process (Powertools parameters cache) for `max_age` seconds, so a warm
container makes zero SSM calls and a rotated key is picked up within the hour.
"""

from __future__ import annotations

from aws_lambda_powertools.utilities import parameters

from acme_common.llm.errors import LLMConfigurationError

CACHE_SECONDS = 3600


def get_secret(parameter_name: str) -> str:
    if not parameter_name:
        raise LLMConfigurationError("no API key parameter configured")
    value = parameters.get_parameter(parameter_name, decrypt=True, max_age=CACHE_SECONDS)
    if not value:
        raise LLMConfigurationError(f"parameter {parameter_name} is empty")
    return value
