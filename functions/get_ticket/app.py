"""GET /tickets/{ticket_id} - current status and, once finished, the triage result."""

from __future__ import annotations

import json
import os
from http import HTTPStatus

from aws_lambda_powertools.event_handler import APIGatewayRestResolver, Response, content_types
from aws_lambda_powertools.logging import correlation_paths

from acme_common.models import TERMINAL_STATUSES, Status, is_valid_ticket_id
from acme_common.observability import logger, metrics, tracer
from acme_common.store import TicketStore

_store = TicketStore(os.environ["TABLE_NAME"])
app = APIGatewayRestResolver()

# Internal bookkeeping that clients don't need to see.
_HIDDEN_FIELDS = {"idempotency_key", "raw_s3_key", "execution_arn"}


def _json(status: HTTPStatus, body: dict) -> Response:
    return Response(
        status_code=status.value,
        content_type=content_types.APPLICATION_JSON,
        body=json.dumps(body, default=str),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/tickets/<ticket_id>")
def get_ticket(ticket_id: str) -> Response:
    if not is_valid_ticket_id(ticket_id):
        return _json(HTTPStatus.BAD_REQUEST, {"message": "ticket_id must be a UUID"})
    item = _store.get(ticket_id)
    if item is None:
        return _json(HTTPStatus.NOT_FOUND, {"message": "ticket not found"})
    body = {k: v for k, v in item.items() if k not in _HIDDEN_FIELDS}
    body["terminal"] = Status(item["status"]) in TERMINAL_STATUSES
    return _json(HTTPStatus.OK, body)


@logger.inject_lambda_context(correlation_id_path=correlation_paths.API_GATEWAY_REST)
@tracer.capture_lambda_handler
@metrics.log_metrics(raise_on_empty_metrics=False)
def lambda_handler(event: dict, context) -> dict:
    return app.resolve(event, context)
