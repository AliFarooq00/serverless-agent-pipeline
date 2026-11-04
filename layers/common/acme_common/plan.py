"""Planner output validation - the LLM proposes, code disposes.

Rules:
* only steps on the allow-list survive; anything else is dropped and recorded in
  `rejected_steps` (observable via logs/metrics, never executed);
* duplicates are removed and steps are put in canonical order;
* `classify` is always present - every ticket gets a category and priority for routing;
* `requires_review` is derived (true iff a customer-facing draft will be produced) - it is never
  taken from the model, so the guardrail cannot be planned away.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

ALLOWED_STEPS: tuple[str, ...] = ("classify", "extract", "draft_reply")
MANDATORY_STEPS: tuple[str, ...] = ("classify",)
MAX_REASON_CHARS = 500


class PlanValidationError(ValueError):
    pass


@dataclass(frozen=True)
class Plan:
    steps: list[str]
    reason: str
    requires_review: bool
    rejected_steps: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_plan(raw: Any) -> Plan:
    if not isinstance(raw, dict) or not isinstance(raw.get("steps"), list):
        raise PlanValidationError("plan must be an object with a 'steps' list")

    proposed: list[str] = []
    rejected: list[str] = []
    for step in raw["steps"]:
        if not isinstance(step, str):
            rejected.append(repr(step))
            continue
        name = step.strip().lower()
        if name in ALLOWED_STEPS:
            proposed.append(name)
        elif name not in rejected:
            rejected.append(name)

    kept = {*proposed, *MANDATORY_STEPS}
    steps = [s for s in ALLOWED_STEPS if s in kept]
    reason = raw.get("reason") if isinstance(raw.get("reason"), str) else ""
    return Plan(
        steps=steps,
        reason=reason[:MAX_REASON_CHARS],
        requires_review="draft_reply" in steps,
        rejected_steps=rejected,
    )
