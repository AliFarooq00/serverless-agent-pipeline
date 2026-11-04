"""Deterministic guardrails that run *before* the LLM reviewer.

Cheap, explainable, and impossible to prompt-inject. The LLM reviewer catches subtler problems
(hallucinated features, tone); these rules catch the expensive-if-missed ones every time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_FLAGS = re.IGNORECASE


@dataclass(frozen=True)
class Rule:
    type: str
    pattern: re.Pattern[str]
    detail: str


DRAFT_RULES: tuple[Rule, ...] = (
    Rule(
        "refund_or_credit",
        re.compile(
            r"\b(we|i)(\s+will|'ll|\s+can|\s+are going to)\s+(issue|process|give|send|grant)?"
            r"\s*(you\s+)?(a\s+)?(full\s+|partial\s+)?(refund|credit)"
            r"|\brefund\b[^.]{0,40}\bin full\b",
            _FLAGS,
        ),
        "Support agents cannot promise refunds or credits; billing must approve them.",
    ),
    Rule(
        "timeline_commitment",
        re.compile(
            r"\b(by|before|within)\s+(tomorrow|tonight|today|end of (the )?(day|week)|"
            r"\d+\s*(hours?|days?|business days?))\b|\bwill be fixed\b",
            _FLAGS,
        ),
        "Do not commit to fix dates or turnaround times.",
    ),
    Rule(
        "unsupported_promise",
        re.compile(r"\b(we|i)\s+(guarantee|promise)\b", _FLAGS),
        "Avoid guarantees and promises.",
    ),
    Rule(
        "credential_request",
        re.compile(
            r"\b(send|share|provide|tell|give)\b[^.]{0,40}\b(password|2fa code|"
            r"verification code|recovery code|card number|cvv)\b",
            _FLAGS,
        ),
        "Never ask customers for passwords, codes or card details.",
    ),
)

LEGAL_PATTERN = re.compile(
    r"\b(lawyer|attorney|legal action|lawsuit|sue you|small claims|court)\b", _FLAGS
)


def check_draft(reply: str) -> list[dict[str, str]]:
    return [
        {"type": rule.type, "detail": rule.detail, "source": "rule"}
        for rule in DRAFT_RULES
        if rule.pattern.search(reply or "")
    ]


def ticket_requires_human(ticket: dict[str, str]) -> str | None:
    """Some tickets must reach a human no matter how good the draft is."""
    text = f"{ticket.get('subject', '')} {ticket.get('body', '')}"
    if LEGAL_PATTERN.search(text):
        return "Ticket mentions legal action - route to a human."
    return None
