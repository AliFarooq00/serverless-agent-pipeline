import io
import json
import urllib.error

import pytest

from acme_common.llm import (
    FakeLLM,
    LLMConfigurationError,
    LLMRequestError,
    LLMResponseError,
    LLMThrottledError,
    LLMUnavailableError,
)
from acme_common.llm import http as llm_http
from acme_common.llm.anthropic_client import AnthropicClient
from acme_common.llm.factory import build_client
from acme_common.llm.openai_client import OpenAIClient
from acme_common.llm.schema import SchemaError, validate
from acme_common.schemas import CLASSIFICATION_SCHEMA

GOOD = {
    "category": "bug",
    "priority": "p2",
    "sentiment": "negative",
    "confidence": 0.9,
    "rationale": "Sync error.",
}


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def http_stub(monkeypatch):
    captured = {}

    def install(response=None, error=None):
        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["payload"] = json.loads(request.data)
            if error:
                raise error
            return _Resp(json.dumps(response).encode())

        monkeypatch.setattr(llm_http, "urlopen", fake_urlopen)
        return captured

    return install


def _call(client):
    return client.complete_json(
        system="sys", user="usr", schema=CLASSIFICATION_SCHEMA, schema_name="classification"
    )


def test_openai_uses_strict_json_schema_and_reports_usage(http_stub):
    captured = http_stub(
        {
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(GOOD)}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30},
        }
    )
    result = _call(OpenAIClient(model="m", api_key="sk-test"))

    assert result.data == GOOD
    assert (result.usage.input_tokens, result.usage.output_tokens) == (120, 30)
    assert result.usage.provider == "openai"
    fmt = captured["payload"]["response_format"]
    assert fmt["json_schema"]["strict"] is True
    assert captured["headers"]["Authorization"] == "Bearer sk-test"


def test_anthropic_forces_tool_call_and_reads_tool_input(http_stub):
    captured = http_stub(
        {
            "stop_reason": "tool_use",
            "content": [
                {"type": "text", "text": "thinking..."},
                {"type": "tool_use", "name": "classification", "input": GOOD},
            ],
            "usage": {"input_tokens": 200, "output_tokens": 40},
        }
    )
    result = _call(AnthropicClient(model="m", api_key="ak-test"))

    assert result.data == GOOD
    assert result.usage.input_tokens == 200
    assert captured["payload"]["tool_choice"] == {"type": "tool", "name": "classification"}
    assert captured["headers"]["X-api-key"] == "ak-test"


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (429, LLMThrottledError),
        (500, LLMUnavailableError),
        (503, LLMUnavailableError),
        (529, LLMUnavailableError),
        (400, LLMRequestError),
        (401, LLMRequestError),
    ],
)
def test_http_errors_map_to_retryable_taxonomy(http_stub, code, expected):
    err = urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(b"{}"))
    http_stub(error=err)
    with pytest.raises(expected):
        _call(OpenAIClient(model="m", api_key="k"))


def test_transport_errors_are_retryable(http_stub):
    http_stub(error=urllib.error.URLError("connection reset"))
    with pytest.raises(LLMUnavailableError):
        _call(AnthropicClient(model="m", api_key="k"))


def test_schema_violations_and_truncation_raise_response_error(http_stub):
    bad = {**GOOD, "priority": "urgent"}
    http_stub({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(bad)}}]})
    with pytest.raises(LLMResponseError, match="schema"):
        _call(OpenAIClient(model="m", api_key="k"))

    http_stub({"choices": [{"finish_reason": "length", "message": {"content": "{"}}]})
    with pytest.raises(LLMResponseError, match="truncated"):
        _call(OpenAIClient(model="m", api_key="k"))

    http_stub({"stop_reason": "end_turn", "content": [{"type": "text", "text": "hi"}]})
    with pytest.raises(LLMResponseError, match="tool_use"):
        _call(AnthropicClient(model="m", api_key="k"))


def test_api_key_never_appears_in_repr():
    assert "secret" not in repr(OpenAIClient(model="m", api_key="secret"))
    assert "secret" not in repr(AnthropicClient(model="m", api_key="secret"))


def test_factory_builds_fake_without_secrets_and_rejects_unknown():
    assert isinstance(build_client("fake"), FakeLLM)
    with pytest.raises(LLMConfigurationError):
        build_client("cohere")
    with pytest.raises(LLMConfigurationError):
        build_client("openai", api_key_param="")


def test_factory_reads_key_from_ssm_securestring():
    import boto3
    from moto import mock_aws

    with mock_aws():
        boto3.client("ssm").put_parameter(
            Name="/acme-notes/triage/anthropic-api-key", Value="ak-from-ssm", Type="SecureString"
        )
        client = build_client("anthropic", api_key_param="/acme-notes/triage/anthropic-api-key")
    assert isinstance(client, AnthropicClient)
    assert client._api_key == "ak-from-ssm"
    assert client.model == "claude-haiku-4-5"


def test_mini_validator():
    validate({"a": None}, {"type": "object", "properties": {"a": {"type": ["string", "null"]}}})
    with pytest.raises(SchemaError):
        validate(True, {"type": "integer"})  # bools are not ints
    with pytest.raises(SchemaError):
        validate(
            {"a": 1, "b": 2},
            {"type": "object", "properties": {"a": {}}, "additionalProperties": False},
        )
    with pytest.raises(SchemaError):
        validate([1, "x"], {"type": "array", "items": {"type": "integer"}})


def test_fake_llm_script_modes():
    fake = FakeLLM(
        script={
            "classification": [GOOD, {**GOOD, "priority": "p1"}],
            "plan": lambda system, user: {"steps": ["classify"], "reason": user},
        }
    )
    assert _call(fake).data["priority"] == "p2"
    assert _call(fake).data["priority"] == "p1"
    with pytest.raises(AssertionError, match="exhausted"):
        _call(fake)
    plan = fake.complete_json(
        system="s", user="hello", schema={"type": "object"}, schema_name="plan"
    )
    assert plan.data["reason"] == "hello"
    assert len(fake.calls) == 4
