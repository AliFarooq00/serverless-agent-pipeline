"""Planner agent: decides which specialist agents this ticket needs.

The model's plan is advisory - `validate_plan` enforces the allow-list and invariants.
"""

from __future__ import annotations

from aws_lambda_powertools.metrics import MetricUnit

from acme_common.agent import run_agent
from acme_common.llm import get_llm_client
from acme_common.observability import logger, metrics, tracer
from acme_common.plan import ALLOWED_STEPS, validate_plan
from acme_common.prompts import UNTRUSTED_INPUT_RULE, render_ticket
from acme_common.schemas import PLAN_SCHEMA

SYSTEM_PROMPT = f"""You are the planning agent in a support-ticket triage pipeline.
Choose the smallest set of specialist steps this ticket needs. Available steps:
- classify: category, priority and sentiment (needed for routing; always useful)
- extract: structured diagnostic fields (product area, error code, OS, app version)
- draft_reply: a reply to the customer (it will be reviewed before anyone sees it)
Only use these exact step names: {", ".join(ALLOWED_STEPS)}.
Skip extract when the ticket has no product problem (spam, feature ideas, praise).
Skip draft_reply for spam. Give a one-sentence reason.
{UNTRUSTED_INPUT_RULE}"""


@logger.inject_lambda_context(clear_state=True)
@tracer.capture_lambda_handler
@metrics.log_metrics(raise_on_empty_metrics=False)
def lambda_handler(event: dict, _context) -> dict:
    logger.append_keys(ticket_id=event.get("ticket_id"))
    data, usage = run_agent(
        "planner",
        llm=get_llm_client(),
        system=SYSTEM_PROMPT,
        user=render_ticket(event["ticket"]),
        schema=PLAN_SCHEMA,
        schema_name="plan",
        max_tokens=300,
    )
    plan = validate_plan(data)
    if plan.rejected_steps:
        logger.warning("planner proposed steps outside the allow-list", extra=plan.as_dict())
        metrics.add_metric(
            name="PlanStepsRejected", unit=MetricUnit.Count, value=len(plan.rejected_steps)
        )
    return {**plan.as_dict(), "usage": usage}
