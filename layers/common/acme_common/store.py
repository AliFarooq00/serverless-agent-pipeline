"""DynamoDB access for ticket items (single-table design, see stacks/data.yaml)."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr

from acme_common.models import Status

META = "META"
_INTERNAL_KEYS = {"pk", "sk", "gsi1pk", "gsi1sk"}


def ticket_pk(ticket_id: str) -> str:
    return f"TICKET#{ticket_id}"


def to_dynamo(value: Any) -> Any:
    """DynamoDB rejects floats; round-trip through JSON turning them into Decimals."""
    return json.loads(json.dumps(value), parse_float=Decimal)


def from_dynamo(value: Any) -> Any:
    if isinstance(value, list):
        return [from_dynamo(v) for v in value]
    if isinstance(value, dict):
        return {k: from_dynamo(v) for k, v in value.items()}
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


class TicketStore:
    def __init__(self, table_name: str, resource: Any | None = None):
        self._table = (resource or boto3.resource("dynamodb")).Table(table_name)

    def put_pending(
        self,
        *,
        ticket_id: str,
        ticket: dict[str, str],
        raw_s3_key: str,
        idempotency_key: str,
        now: str,
    ) -> None:
        self._table.put_item(
            Item={
                "pk": ticket_pk(ticket_id),
                "sk": META,
                "gsi1pk": f"STATUS#{Status.PENDING}",
                "gsi1sk": now,
                "ticket_id": ticket_id,
                "status": Status.PENDING.value,
                "subject": ticket["subject"],
                "customer_tier": ticket["customer_tier"],
                "raw_s3_key": raw_s3_key,
                "idempotency_key": idempotency_key,
                "created_at": now,
                "updated_at": now,
            },
            ConditionExpression=Attr("pk").not_exists(),
        )

    def mark_failed(self, ticket_id: str, *, error: str, cause: str, now: str) -> None:
        self._set_status(ticket_id, Status.FAILED, now, extra={"error": error, "cause": cause})

    def finalize(
        self,
        ticket_id: str,
        *,
        status: Status,
        result: dict[str, Any],
        agent_calls: list[dict[str, Any]],
        usage_totals: dict[str, Any],
        execution_arn: str | None,
        now: str,
    ) -> None:
        self._set_status(
            ticket_id,
            status,
            now,
            extra={
                "result": to_dynamo(result),
                "agent_calls": to_dynamo(agent_calls),
                "usage_totals": to_dynamo(usage_totals),
                "execution_arn": execution_arn,
                "completed_at": now,
            },
        )

    def _set_status(
        self, ticket_id: str, status: Status, now: str, extra: dict[str, Any] | None = None
    ) -> None:
        fields: dict[str, Any] = {
            "status": status.value,
            "updated_at": now,
            "gsi1pk": f"STATUS#{status}",
            "gsi1sk": now,
            **(extra or {}),
        }
        names = {f"#f{i}": k for i, k in enumerate(fields)}
        values = {f":v{i}": v for i, v in enumerate(fields.values())}
        sets = ", ".join(f"#f{i} = :v{i}" for i in range(len(fields)))
        self._table.update_item(
            Key={"pk": ticket_pk(ticket_id), "sk": META},
            UpdateExpression=f"SET {sets}",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            # Never resurrect a ticket that ingest did not create.
            ConditionExpression="attribute_exists(pk)",
        )

    def get(self, ticket_id: str) -> dict[str, Any] | None:
        item = self._table.get_item(Key={"pk": ticket_pk(ticket_id), "sk": META}).get("Item")
        if not item:
            return None
        return from_dynamo({k: v for k, v in item.items() if k not in _INTERNAL_KEYS})
