import json

import pytest
from conftest import BUCKET, api_event, load_function, receive_all

pytestmark = pytest.mark.integration

TICKET = {
    "subject": "Sync stuck at 99%",
    "body": "My notes have not synced since yesterday. Error SYNC-409 on Windows.",
    "customer_tier": "pro",
}


@pytest.fixture
def ingest(aws):
    return load_function("ingest")


def _post(ingest, ctx, body, headers=None):
    resp = ingest.lambda_handler(api_event("POST", "/tickets", body, headers), ctx)
    return resp["statusCode"], json.loads(resp["body"]), resp


def test_accepts_ticket_and_fans_out_to_s3_dynamodb_sqs(ingest, aws, lambda_context):
    status, body, resp = _post(ingest, lambda_context, TICKET)

    assert status == 202
    ticket_id = body["ticket_id"]
    assert body["status"] == "PENDING"
    assert resp["multiValueHeaders"]["Location"] == [f"/tickets/{ticket_id}"]

    item = ingest._store.get(ticket_id)
    assert item["status"] == "PENDING"
    assert item["customer_tier"] == "pro"

    raw = aws["s3"].get_object(Bucket=BUCKET, Key=item["raw_s3_key"])
    archived = json.loads(raw["Body"].read())
    assert archived["ticket"] == TICKET

    messages = receive_all(aws["sqs"])
    assert len(messages) == 1
    assert json.loads(messages[0]["Body"])["ticket_id"] == ticket_id


def test_same_idempotency_key_returns_same_ticket_and_enqueues_once(ingest, aws, lambda_context):
    headers = {"Idempotency-Key": "client-retry-0001"}
    _, first, _ = _post(ingest, lambda_context, TICKET, headers)
    _, second, _ = _post(ingest, lambda_context, TICKET, headers)

    assert first["ticket_id"] == second["ticket_id"]
    assert len(receive_all(aws["sqs"])) == 1


def test_header_lookup_is_case_insensitive(ingest, aws, lambda_context):
    _, first, _ = _post(ingest, lambda_context, TICKET, {"idempotency-key": "lowercase-key-01"})
    _, second, _ = _post(ingest, lambda_context, TICKET, {"IDEMPOTENCY-KEY": "lowercase-key-01"})
    assert first["ticket_id"] == second["ticket_id"]


def test_identical_body_without_key_is_deduplicated(ingest, aws, lambda_context):
    _, first, _ = _post(ingest, lambda_context, TICKET)
    _, second, _ = _post(ingest, lambda_context, TICKET)
    assert first["ticket_id"] == second["ticket_id"]

    _, third, _ = _post(ingest, lambda_context, {**TICKET, "subject": "Different subject"})
    assert third["ticket_id"] != first["ticket_id"]
    assert len(receive_all(aws["sqs"])) == 2


def test_reused_key_with_different_payload_is_rejected(ingest, aws, lambda_context):
    headers = {"Idempotency-Key": "client-retry-0002"}
    _post(ingest, lambda_context, TICKET, headers)
    status, body, _ = _post(ingest, lambda_context, {**TICKET, "body": "changed"}, headers)

    assert status == 422
    assert "different payload" in body["message"]
    assert len(receive_all(aws["sqs"])) == 1


@pytest.mark.parametrize(
    ("payload", "fragment"),
    [
        ({**TICKET, "customer_tier": "platinum"}, "customer_tier"),
        ({**TICKET, "subject": "   "}, "subject"),
        ({**TICKET, "body": "x" * 10_001}, "at most"),
        ({**TICKET, "admin": True}, "unexpected fields"),
        ([1, 2, 3], "JSON object"),
    ],
)
def test_rejects_invalid_tickets(ingest, aws, lambda_context, payload, fragment):
    status, body, _ = _post(ingest, lambda_context, payload)
    assert status == 400
    assert fragment in json.dumps(body)
    assert receive_all(aws["sqs"]) == []


def test_rejects_malformed_json_and_bad_keys(ingest, aws, lambda_context):
    status, _, _ = _post(ingest, lambda_context, "{not json")
    assert status == 400
    status, _, _ = _post(ingest, lambda_context, TICKET, {"Idempotency-Key": "short"})
    assert status == 400


def test_enqueue_failure_marks_ticket_failed_and_allows_retry(ingest, aws, lambda_context):
    original = ingest._sqs.send_message
    ingest._sqs.send_message = lambda **_: (_ for _ in ()).throw(RuntimeError("sqs down"))
    headers = {"Idempotency-Key": "client-retry-0003"}

    with pytest.raises(RuntimeError):
        _post(ingest, lambda_context, TICKET, headers)

    # The idempotency record was released, so the client retry goes through.
    ingest._sqs.send_message = original
    status, body, _ = _post(ingest, lambda_context, TICKET, headers)
    assert status == 202
    assert ingest._store.get(body["ticket_id"])["status"] == "PENDING"
    assert len(receive_all(aws["sqs"])) == 1
