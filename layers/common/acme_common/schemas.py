"""Structured-output schemas for each agent.

All schemas are compatible with OpenAI strict mode (every property required,
additionalProperties false, nullable via type unions) and with Anthropic tool input schemas.
Range checks (e.g. confidence 0..1) are enforced in code, not in the schema, for portability.
"""

from __future__ import annotations

from typing import Any

CATEGORIES = ["bug", "billing", "account", "outage", "feature_request", "how_to", "other"]
PRIORITIES = ["p1", "p2", "p3", "p4"]
SENTIMENTS = ["positive", "neutral", "negative", "angry"]
REVIEW_ISSUE_TYPES = [
    "refund_or_credit",
    "timeline_commitment",
    "unsupported_promise",
    "credential_request",
    "hallucinated_feature",
    "legal_escalation",
    "tone",
    "other",
]


def _obj(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


PLAN_SCHEMA = _obj(
    {
        # Not an enum on purpose: the allow-list is enforced server-side (acme_common.plan) so it
        # holds regardless of provider and is observable when a model proposes something else.
        "steps": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
    }
)

CLASSIFICATION_SCHEMA = _obj(
    {
        "category": {"type": "string", "enum": CATEGORIES},
        "priority": {"type": "string", "enum": PRIORITIES},
        "sentiment": {"type": "string", "enum": SENTIMENTS},
        "confidence": {"type": "number"},
        "rationale": {"type": "string"},
    }
)

DRAFT_SCHEMA = _obj(
    {
        "reply": {"type": "string"},
        "tone": {"type": "string", "enum": ["empathetic", "neutral", "upbeat"]},
        "needs_followup": {"type": "boolean"},
    }
)

REVIEW_SCHEMA = _obj(
    {
        "verdict": {"type": "string", "enum": ["approve", "revise"]},
        "issues": {
            "type": "array",
            "items": _obj(
                {
                    "type": {"type": "string", "enum": REVIEW_ISSUE_TYPES},
                    "detail": {"type": "string"},
                }
            ),
        },
        "feedback": {"type": "string"},
    }
)
