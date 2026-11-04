"""Helpers for reading the state-machine payload (Map results, usage records)."""

from __future__ import annotations

from typing import Any


def index_results(results: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    """Map output is a list of {step, output, usage}; index it by step name."""
    return {r["step"]: r for r in (results or []) if isinstance(r, dict) and "step" in r}


def latest_draft(
    results: list[dict[str, Any]] | None, redraft: dict[str, Any] | None
) -> dict[str, Any] | None:
    if redraft and redraft.get("output"):
        return redraft["output"]
    entry = index_results(results).get("draft_reply")
    return entry["output"] if entry else None


def collect_usage(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Every agent call in execution order: planner, specialists, reviews, redraft."""
    calls: list[dict[str, Any]] = []

    def add(obj: Any) -> None:
        if isinstance(obj, dict) and isinstance(obj.get("usage"), dict):
            calls.append(obj["usage"])

    add(state.get("plan"))
    for r in state.get("results") or []:
        add(r)
    loop = state.get("review_loop") or {}
    add(loop.get("previous_review"))
    add(loop.get("redraft"))
    add(state.get("review"))
    return calls


def usage_totals(calls: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "agent_calls": len(calls),
        "input_tokens": sum(int(c.get("input_tokens", 0)) for c in calls),
        "output_tokens": sum(int(c.get("output_tokens", 0)) for c in calls),
        "llm_latency_ms": sum(int(c.get("latency_ms", 0)) for c in calls),
    }
