"""Domain model: ticket validation and workflow statuses.

Kept dependency-free on purpose (no pydantic) so the layer stays small and cold starts stay fast.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

CUSTOMER_TIERS = ("free", "pro", "business", "enterprise")
MAX_SUBJECT_LEN = 200
MAX_BODY_LEN = 10_000
TICKET_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")


class Status(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    FAILED = "FAILED"


TERMINAL_STATUSES = frozenset({Status.COMPLETED, Status.NEEDS_HUMAN, Status.FAILED})


class TicketValidationError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def validate_ticket(payload: Any) -> dict[str, str]:
    """Validate and normalise an inbound ticket. Returns a new dict; never mutates input."""
    if not isinstance(payload, dict):
        raise TicketValidationError(["body must be a JSON object"])

    errors: list[str] = []
    allowed = {"subject", "body", "customer_tier"}
    extra = sorted(set(payload) - allowed)
    if extra:
        errors.append(f"unexpected fields: {', '.join(extra)}")

    def _text(field: str, max_len: int) -> str:
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{field} is required and must be a non-empty string")
            return ""
        value = value.strip()
        if len(value) > max_len:
            errors.append(f"{field} must be at most {max_len} characters")
        return value

    subject = _text("subject", MAX_SUBJECT_LEN)
    body = _text("body", MAX_BODY_LEN)
    tier = payload.get("customer_tier")
    if tier not in CUSTOMER_TIERS:
        errors.append(f"customer_tier must be one of {', '.join(CUSTOMER_TIERS)}")

    if errors:
        raise TicketValidationError(errors)
    return {"subject": subject, "body": body, "customer_tier": tier}


def is_valid_ticket_id(value: str) -> bool:
    return bool(TICKET_ID_RE.match(value or ""))
