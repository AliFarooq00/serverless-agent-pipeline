"""Persist the final result (COMPLETED | NEEDS_HUMAN) with per-agent token usage and latency."""

from __future__ import annotations

import os
from datetime import UTC, datetime

from aws_lambda_powertools.metrics import MetricUnit

from acme_common.models import Status
from acme_common.observability import logger, metrics, tracer
from acme_common.results import collect_usage, index_results, latest_draft, usage_totals
from acme_common.store import TicketStore

_store = TicketStore(os.environ["TABLE_NAME"])


def build_result(state: dict) -> dict:
    results = index_results(state.get("results"))
    loop = state.get("review_loop") or {}
    review = state.get("review")
    draft = latest_draft(state.get("results"), loop.get("redraft"))
    plan = state.get("plan") or {}
    return {
        "plan": {
            "steps": plan.get("steps", []),
            "reason": plan.get("reason", ""),
            "rejected_steps": plan.get("rejected_steps", []),
        },
        "classification": (results.get("classify") or {}).get("output"),
        "extraction": (results.get("extract") or {}).get("output"),
        "draft_reply": draft,
        "review": (
            {
                "verdict": review.get("verdict"),
                "issues": review.get("issues", []),
                "feedback": review.get("feedback"),
                "revisions": int(loop.get("revision", 0)),
            }
            if review
            else None
        ),
    }


@logger.inject_lambda_context(clear_state=True)
@tracer.capture_lambda_handler
@metrics.log_metrics(raise_on_empty_metrics=False)
def lambda_handler(state: dict, _context) -> dict:
    ticket_id = state["ticket_id"]
    status = Status(state["final_status"])
    logger.append_keys(ticket_id=ticket_id)

    calls = collect_usage(state)
    totals = usage_totals(calls)
    _store.finalize(
        ticket_id,
        status=status,
        result=build_result(state),
        agent_calls=calls,
        usage_totals=totals,
        execution_arn=state.get("execution_arn"),
        now=datetime.now(UTC).isoformat(timespec="milliseconds"),
    )
    metrics.add_metric(
        name=f"Tickets{status.value.title().replace('_', '')}", unit=MetricUnit.Count, value=1
    )
    metrics.add_metric(
        name="TicketInputTokens", unit=MetricUnit.Count, value=totals["input_tokens"]
    )
    metrics.add_metric(
        name="TicketOutputTokens", unit=MetricUnit.Count, value=totals["output_tokens"]
    )
    logger.info("ticket finalised", extra={"status": status.value, "usage_totals": totals})
    return {"ticket_id": ticket_id, "status": status.value, "usage_totals": totals}
