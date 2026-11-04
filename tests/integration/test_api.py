import json

import pytest
from conftest import api_event, load_cfn, load_function

from acme_common.models import CUSTOMER_TIERS, MAX_BODY_LEN, MAX_SUBJECT_LEN

pytestmark = pytest.mark.integration

TICKET = {
    "subject": "Export to PDF?",
    "body": "How do I export a notebook?",
    "customer_tier": "free",
}


@pytest.fixture
def api(aws):
    return load_function("ingest"), load_function("get_ticket")


def _get(get_fn, ctx, ticket_id):
    event = api_event(
        "GET",
        f"/tickets/{ticket_id}",
        resource="/tickets/{ticket_id}",
        path_params={"ticket_id": ticket_id},
    )
    resp = get_fn.lambda_handler(event, ctx)
    return resp["statusCode"], json.loads(resp["body"])


def test_get_returns_pending_ticket_without_internal_fields(api, lambda_context):
    ingest, get_fn = api
    created = json.loads(
        ingest.lambda_handler(api_event("POST", "/tickets", TICKET), lambda_context)["body"]
    )
    status, body = _get(get_fn, lambda_context, created["ticket_id"])

    assert status == 200
    assert body["status"] == "PENDING"
    assert body["terminal"] is False
    assert body["subject"] == TICKET["subject"]
    assert not {"pk", "sk", "gsi1pk", "idempotency_key", "raw_s3_key"} & set(body)


def test_get_unknown_and_malformed_ids(api, lambda_context):
    _, get_fn = api
    assert _get(get_fn, lambda_context, "6f1c2a8e-3b4d-4e5f-9a0b-1c2d3e4f5a6b")[0] == 404
    assert _get(get_fn, lambda_context, "not-a-uuid")[0] == 400


def test_gateway_request_model_matches_lambda_validation():
    """API Gateway rejects bad bodies before Lambda runs; both layers must agree."""
    api = load_cfn("stacks/api.yaml")["Resources"]["TicketsApi"]["Properties"]["DefinitionBody"]
    model = api["definitions"]["TicketRequest"]
    assert model["additionalProperties"] is False
    assert set(model["required"]) == {"subject", "body", "customer_tier"}
    assert model["properties"]["subject"]["maxLength"] == MAX_SUBJECT_LEN
    assert model["properties"]["body"]["maxLength"] == MAX_BODY_LEN
    assert tuple(model["properties"]["customer_tier"]["enum"]) == CUSTOMER_TIERS
    for path in api["paths"].values():
        for method in path.values():
            assert method["security"] == [{"api_key": []}], "every route requires an API key"
