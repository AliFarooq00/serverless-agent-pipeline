"""Reviewer agent (guardrail): approve, request one revision, or escalate to a human.

Layered checks, cheapest first:
  1. ticket-level escalation rules (e.g. legal threats) -> `escalate`, no LLM call needed
  2. deterministic draft rules (refund promises, timelines, credential requests)
  3. LLM review for subtler problems (hallucinated features, tone)
Any issue from 2 or 3 -> `revise`. The *loop limit* is owned by the state machine, not here.
"""

from __future__ import annotations

from aws_lambda_powertools.metrics import MetricUnit

from acme_common.agent import run_agent
from acme_common.guardrails import check_draft, ticket_requires_human
from acme_common.llm import get_llm_client
from acme_common.observability import logger, metrics, tracer
from acme_common.prompts import UNTRUSTED_INPUT_RULE, block, render_ticket
from acme_common.results import latest_draft
from acme_common.schemas import REVIEW_ISSUE_TYPES, REVIEW_SCHEMA

SYSTEM_PROMPT = f"""You are the policy reviewer for Acme Notes support replies.
Check the <draft> against the <ticket>. Flag an issue for any of: {", ".join(REVIEW_ISSUE_TYPES)}.
- refund_or_credit / unsupported_promise / timeline_commitment: commitments support can't make
- credential_request: asking for passwords, codes or card numbers
- hallucinated_feature: claims about Acme Notes features or fixes not grounded in the ticket
- tone: dismissive, blaming or overly casual language
verdict = "approve" only if there are no issues; otherwise "revise" with actionable feedback.
{UNTRUSTED_INPUT_RULE}"""


@logger.inject_lambda_context(clear_state=True)
@tracer.capture_lambda_handler
@metrics.log_metrics(raise_on_empty_metrics=False)
def lambda_handler(event: dict, _context) -> dict:
    revision = int(event.get("revision", 0))
    logger.append_keys(ticket_id=event.get("ticket_id"), revision=revision)
    ticket = event["ticket"]
    draft = latest_draft(event.get("results"), event.get("redraft"))

    escalation = ticket_requires_human(ticket)
    if escalation or not draft:
        detail = escalation or "No draft was produced."
        metrics.add_metric(name="ReviewEscalations", unit=MetricUnit.Count, value=1)
        return {
            "verdict": "escalate",
            "issues": [{"type": "legal_escalation", "detail": detail, "source": "rule"}],
            "feedback": detail,
            "revision": revision,
        }

    issues = check_draft(draft.get("reply", ""))
    data, usage = run_agent(
        "reviewer",
        llm=get_llm_client(),
        system=SYSTEM_PROMPT,
        user=f"{render_ticket(ticket)}\n\n{block('draft', draft)}",
        schema=REVIEW_SCHEMA,
        schema_name="review",
        max_tokens=400,
    )
    known = {i["type"] for i in issues}
    issues += [{**i, "source": "llm"} for i in data["issues"] if i["type"] not in known]

    verdict = "revise" if issues else "approve"
    feedback = " ".join(dict.fromkeys(i["detail"] for i in issues)) if issues else data["feedback"]
    metrics.add_metric(
        name="ReviewRevisionsRequested" if issues else "ReviewApprovals",
        unit=MetricUnit.Count,
        value=1,
    )
    return {
        "verdict": verdict,
        "issues": issues,
        "feedback": feedback,
        "revision": revision,
        "usage": usage,
    }
