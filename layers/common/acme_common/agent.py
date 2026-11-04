"""Uniform agent invocation: one LLM call, validated output, usage record, metrics."""

from __future__ import annotations

from typing import Any

from aws_lambda_powertools.metrics import MetricUnit, single_metric

from acme_common.llm.base import LLMClient
from acme_common.observability import NAMESPACE, logger


def run_agent(
    agent: str,
    *,
    llm: LLMClient,
    system: str,
    user: str,
    schema: dict[str, Any],
    schema_name: str,
    max_tokens: int = 800,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Returns (validated_output, usage_record). Usage is persisted on the ticket item."""
    result = llm.complete_json(
        system=system, user=user, schema=schema, schema_name=schema_name, max_tokens=max_tokens
    )
    usage = {"agent": agent, **result.usage.as_dict()}

    for name, unit, value in (
        ("AgentLatency", MetricUnit.Milliseconds, usage["latency_ms"]),
        ("AgentInputTokens", MetricUnit.Count, usage["input_tokens"]),
        ("AgentOutputTokens", MetricUnit.Count, usage["output_tokens"]),
    ):
        with single_metric(
            name=name, unit=unit, value=value, namespace=NAMESPACE, default_dimensions={}
        ) as metric:
            metric.add_dimension(name="agent", value=agent)
            metric.add_dimension(name="provider", value=usage["provider"])

    logger.info("agent call complete", extra={"usage": usage})
    return result.data, usage
