"""POST /tickets - validate, de-duplicate, archive, record PENDING, enqueue.

Idempotency: the client may send an `Idempotency-Key` header. Without one, the key is a
SHA-256 of the normalised ticket, so an accidental double-submit of the same ticket inside the
24h window returns the original ticket instead of creating a second one. Reusing a key with a
different payload is rejected with 422 (Powertools payload validation).
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import UTC, datetime
from http import HTTPStatus

import boto3
from aws_lambda_powertools.event_handler import APIGatewayRestResolver, Response, content_types
from aws_lambda_powertools.logging import correlation_paths
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.idempotency import (
    DynamoDBPersistenceLayer,
    IdempotencyConfig,
    idempotent_function,
)
from aws_lambda_powertools.utilities.idempotency.exceptions import (
    IdempotencyAlreadyInProgressError,
    IdempotencyValidationError,
)

from acme_common.models import IDEMPOTENCY_KEY_RE, TicketValidationError, validate_ticket
from acme_common.observability import logger, metrics, tracer
from acme_common.store import TicketStore

TABLE_NAME = os.environ["TABLE_NAME"]
BUCKET_NAME = os.environ["BUCKET_NAME"]
QUEUE_URL = os.environ["QUEUE_URL"]
IDEMPOTENCY_TTL_SECONDS = int(os.environ.get("IDEMPOTENCY_TTL_SECONDS", "86400"))

# Clients are created once per execution environment (cold start) and reused.
_s3 = boto3.client("s3")
_sqs = boto3.client("sqs")
_store = TicketStore(TABLE_NAME)

persistence = DynamoDBPersistenceLayer(
    table_name=TABLE_NAME,
    key_attr="pk",
    sort_key_attr="sk",
    static_pk_value="IDEMPOTENCY#ingest",
)
idempotency_config = IdempotencyConfig(
    event_key_jmespath="idempotency_key",
    payload_validation_jmespath="ticket",
    raise_on_no_idempotency_key=True,
    expires_after_seconds=IDEMPOTENCY_TTL_SECONDS,
)

app = APIGatewayRestResolver()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _json(status: HTTPStatus, body: dict, headers: dict | None = None) -> Response:
    return Response(
        status_code=status.value,
        content_type=content_types.APPLICATION_JSON,
        body=json.dumps(body),
        headers=headers or {},
    )


def _header(name: str) -> str | None:
    for key, value in (app.current_event.headers or {}).items():
        if key.lower() == name.lower():
            return value
    return None


def derive_idempotency_key(ticket: dict[str, str]) -> str:
    canonical = json.dumps(ticket, sort_keys=True, separators=(",", ":"))
    return "body-" + hashlib.sha256(canonical.encode()).hexdigest()


@idempotent_function(
    data_keyword_argument="request",
    config=idempotency_config,
    persistence_store=persistence,
)
@tracer.capture_method
def create_ticket(request: dict) -> dict:
    ticket = request["ticket"]
    ticket_id = str(uuid.uuid4())
    received_at = _now()
    raw_key = f"raw/{received_at[:10].replace('-', '/')}/{ticket_id}.json"

    # 1. Archive the raw payload first: it is the audit record, and cheap if later steps fail.
    _s3.put_object(
        Bucket=BUCKET_NAME,
        Key=raw_key,
        Body=json.dumps(
            {
                "ticket_id": ticket_id,
                "received_at": received_at,
                "idempotency_key": request["idempotency_key"],
                "ticket": ticket,
            }
        ).encode(),
        ContentType="application/json",
        ServerSideEncryption="AES256",
    )

    # 2. Record the ticket so GET works immediately.
    _store.put_pending(
        ticket_id=ticket_id,
        ticket=ticket,
        raw_s3_key=raw_key,
        idempotency_key=request["idempotency_key"],
        now=received_at,
    )

    # 3. Hand off to the async side. If this fails the row would be orphaned in PENDING, so mark
    #    it FAILED (best effort) and re-raise - Powertools then deletes the in-progress
    #    idempotency record and the client can safely retry.
    try:
        _sqs.send_message(
            QueueUrl=QUEUE_URL,
            MessageBody=json.dumps(
                {
                    "ticket_id": ticket_id,
                    "received_at": received_at,
                    "raw_s3_key": raw_key,
                    "ticket": ticket,
                }
            ),
        )
    except Exception as exc:
        logger.exception("enqueue failed", extra={"ticket_id": ticket_id})
        try:
            _store.mark_failed(ticket_id, error="EnqueueFailed", cause=str(exc), now=_now())
        except Exception:  # pragma: no cover - best effort only
            logger.exception("could not mark ticket FAILED after enqueue error")
        raise

    metrics.add_metric(name="TicketsAccepted", unit=MetricUnit.Count, value=1)
    logger.info("ticket accepted", extra={"ticket_id": ticket_id, "tier": ticket["customer_tier"]})
    return {"ticket_id": ticket_id, "status": "PENDING", "received_at": received_at}


@app.post("/tickets")
def post_ticket() -> Response:
    try:
        payload = app.current_event.json_body
    except (json.JSONDecodeError, TypeError):
        return _json(HTTPStatus.BAD_REQUEST, {"message": "body must be valid JSON"})

    try:
        ticket = validate_ticket(payload)
    except TicketValidationError as exc:
        return _json(HTTPStatus.BAD_REQUEST, {"message": "invalid ticket", "errors": exc.errors})

    supplied_key = _header("Idempotency-Key")
    if supplied_key is not None and not IDEMPOTENCY_KEY_RE.match(supplied_key):
        return _json(
            HTTPStatus.BAD_REQUEST,
            {"message": "Idempotency-Key must be 8-128 chars of [A-Za-z0-9_.:-]"},
        )
    key = supplied_key or derive_idempotency_key(ticket)

    try:
        result = create_ticket(request={"idempotency_key": key, "ticket": ticket})
    except IdempotencyValidationError:
        return _json(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            {"message": "Idempotency-Key was already used with a different payload"},
        )
    except IdempotencyAlreadyInProgressError:
        return _json(
            HTTPStatus.CONFLICT,
            {"message": "a request with this Idempotency-Key is still in progress; retry shortly"},
            headers={"Retry-After": "2"},
        )

    return _json(
        HTTPStatus.ACCEPTED,
        {**result, "links": {"self": f"/tickets/{result['ticket_id']}"}},
        headers={"Location": f"/tickets/{result['ticket_id']}"},
    )


@logger.inject_lambda_context(correlation_id_path=correlation_paths.API_GATEWAY_REST)
@tracer.capture_lambda_handler
@metrics.log_metrics(raise_on_empty_metrics=False)
def lambda_handler(event: dict, context) -> dict:
    idempotency_config.register_lambda_context(context)
    return app.resolve(event, context)
