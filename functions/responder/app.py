"""Responder agent: drafts the customer reply.

First pass (inside the Map state) sees only the ticket. A redraft, requested by the reviewer,
also receives the reviewer's feedback, the rejected draft and the other specialists' outputs.
"""

from __future__ import annotations

from acme_common.agent import run_agent
from acme_common.llm import get_llm_client
from acme_common.observability import logger, metrics, tracer
from acme_common.prompts import UNTRUSTED_INPUT_RULE, block, render_ticket
from acme_common.results import index_results, latest_draft
from acme_common.schemas import DRAFT_SCHEMA

SYSTEM_PROMPT = f"""You write first-response emails for Acme Notes customer support.
Be warm, concise (under 120 words) and concrete. Sign off as "The Acme Notes Support Team".
Policy - never: promise refunds, credits or compensation; commit to dates or timelines;
guarantee outcomes; ask for passwords, codes or card numbers; invent product features.
If you cannot help directly, say the right team will follow up.
If <reviewer_feedback> is present, rewrite the draft so every issue is resolved.
{UNTRUSTED_INPUT_RULE}"""


def build_prompt(event: dict) -> str:
    parts = [render_ticket(event["ticket"])]
    context = {k: v["output"] for k, v in index_results(event.get("results")).items()}
    context.pop("draft_reply", None)
    if context:
        parts.append(block("triage_context", context))
    feedback = event.get("feedback")
    if feedback:
        previous = latest_draft(event.get("results"), event.get("redraft"))
        parts.append(block("previous_draft", previous))
        parts.append(block("reviewer_feedback", feedback))
    return "\n\n".join(parts)


@logger.inject_lambda_context(clear_state=True)
@tracer.capture_lambda_handler
@metrics.log_metrics(raise_on_empty_metrics=False)
def lambda_handler(event: dict, _context) -> dict:
    logger.append_keys(ticket_id=event.get("ticket_id"), revision=event.get("revision", 0))
    data, usage = run_agent(
        "responder",
        llm=get_llm_client(),
        system=SYSTEM_PROMPT,
        user=build_prompt(event),
        schema=DRAFT_SCHEMA,
        schema_name="draft_reply",
        max_tokens=500,
    )
    return {
        "step": "draft_reply",
        "revision": int(event.get("revision", 0)),
        "output": data,
        "usage": usage,
    }
