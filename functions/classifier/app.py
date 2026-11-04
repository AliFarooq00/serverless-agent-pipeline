"""Classifier agent: category, priority, sentiment (structured output)."""

from __future__ import annotations

from acme_common.agent import run_agent
from acme_common.llm import get_llm_client
from acme_common.observability import logger, metrics, tracer
from acme_common.prompts import UNTRUSTED_INPUT_RULE, render_ticket
from acme_common.schemas import CATEGORIES, CLASSIFICATION_SCHEMA

SYSTEM_PROMPT = f"""You classify support tickets for Acme Notes.
category: one of {", ".join(CATEGORIES)}.
priority: p1 = outage or data loss affecting many users; p2 = a customer is blocked;
p3 = degraded but has a workaround; p4 = question or idea.
Consider customer_tier: business/enterprise customers who are blocked are at least p2.
sentiment: the customer's emotional state. confidence: 0..1. rationale: one sentence.
{UNTRUSTED_INPUT_RULE}"""


@logger.inject_lambda_context(clear_state=True)
@tracer.capture_lambda_handler
@metrics.log_metrics(raise_on_empty_metrics=False)
def lambda_handler(event: dict, _context) -> dict:
    logger.append_keys(ticket_id=event.get("ticket_id"))
    data, usage = run_agent(
        "classifier",
        llm=get_llm_client(),
        system=SYSTEM_PROMPT,
        user=render_ticket(event["ticket"]),
        schema=CLASSIFICATION_SCHEMA,
        schema_name="classification",
        max_tokens=300,
    )
    data["confidence"] = round(min(max(float(data["confidence"]), 0.0), 1.0), 3)
    return {"step": "classify", "output": data, "usage": usage}
