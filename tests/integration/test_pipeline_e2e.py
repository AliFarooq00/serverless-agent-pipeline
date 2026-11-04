"""End to end, in-process: ingest -> SQS -> starter -> state machine -> persist -> DynamoDB.

AWS is moto. The workflow is the real triage.asl.json run by the test simulator, with the real
Lambda handlers (FakeLLM heuristics) and the DynamoDB service integration executed via boto3.
"""

import json
from pathlib import Path

import boto3
import pytest
from conftest import REGION, ROOT, TABLE, FakeLambdaContext, api_event, load_function, receive_all
from support.asl import Simulator

from acme_common.llm import set_llm_client
from acme_common.registry import load_registry

pytestmark = pytest.mark.integration

DEFINITION = json.loads((ROOT / "statemachine" / "triage.asl.json").read_text())
SAMPLES = sorted((ROOT / "samples").glob("*.json"))
CTX = FakeLambdaContext()

EXPECTED = {
    "001-sync-error": ("COMPLETED", 0),
    "002-double-charge-refund": ("COMPLETED", 1),  # refund promise caught, fixed on redraft
    "003-legal-threat": ("NEEDS_HUMAN", 0),  # escalated, no pointless redraft
    "005-spam": ("COMPLETED", None),  # classify-only plan, no review
    "011-prompt-injection": ("COMPLETED", 1),  # injected promise caught by the guardrail
}


@pytest.fixture
def pipeline(aws):
    set_llm_client(None)  # factory -> FakeLLM (LLM_PROVIDER=fake)
    load_registry.cache_clear()
    sfn = boto3.client("stepfunctions", region_name=REGION)
    arn = sfn.create_state_machine(
        name="triage-test",
        definition=json.dumps(DEFINITION),
        roleArn="arn:aws:iam::123456789012:role/triage-test",
    )["stateMachineArn"]

    fns = {
        n: load_function(n)
        for n in (
            "ingest",
            "starter",
            "planner",
            "classifier",
            "extractor",
            "responder",
            "reviewer",
            "persist",
        )
    }
    ddb = boto3.client("dynamodb", region_name=REGION)

    def ddb_update(params):
        assert params["TableName"] == "${TableName}"
        ddb.update_item(**{**params, "TableName": TABLE})
        return {}

    def lam(name):
        return lambda event: fns[name].lambda_handler(event, CTX)

    simulator = Simulator(
        DEFINITION,
        {
            "${PlannerFunctionArn}": lam("planner"),
            "${ClassifierFunctionArn}": lam("classifier"),
            "${ExtractorFunctionArn}": lam("extractor"),
            "${ResponderFunctionArn}": lam("responder"),
            "${ReviewerFunctionArn}": lam("reviewer"),
            "${PersistFunctionArn}": lam("persist"),
            "arn:aws:states:::dynamodb:updateItem": ddb_update,
        },
    )

    def submit(ticket: dict):
        resp = fns["ingest"].lambda_handler(api_event("POST", "/tickets", ticket), CTX)
        assert resp["statusCode"] == 202, resp["body"]
        ticket_id = json.loads(resp["body"])["ticket_id"]

        messages = receive_all(aws["sqs"])
        sqs_event = {
            "Records": [
                {
                    "messageId": m["MessageId"],
                    "receiptHandle": m["ReceiptHandle"],
                    "body": m["Body"],
                    "attributes": {},
                    "messageAttributes": {},
                    "eventSource": "aws:sqs",
                    "eventSourceARN": f"arn:aws:sqs:{REGION}:123456789012:triage-ingest-test",
                    "awsRegion": REGION,
                }
                for m in messages
            ]
        }
        batch = fns["starter"].lambda_handler(sqs_event, CTX)
        assert batch == {"batchItemFailures": []}

        execution = sfn.describe_execution(
            executionArn=arn.replace(":stateMachine:", ":execution:") + f":{ticket_id}"
        )
        result = simulator.run(json.loads(execution["input"]), name=ticket_id)
        return ticket_id, result, fns["persist"]._store.get(ticket_id)

    yield {"submit": submit, "fns": fns, "sfn": sfn, "arn": arn, "aws": aws}
    set_llm_client(None)


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda p: p.stem)
def test_every_sample_reaches_a_terminal_state(pipeline, sample: Path):
    _ticket_id, execution, item = pipeline["submit"](json.loads(sample.read_text()))

    assert execution.status == "SUCCEEDED", execution.cause
    assert item["status"] in {"COMPLETED", "NEEDS_HUMAN"}
    assert item["result"]["classification"]["category"]
    # Token usage and latency are recorded for every agent call.
    assert item["agent_calls"], "no agent usage recorded"
    for call in item["agent_calls"]:
        assert {"agent", "provider", "model", "input_tokens", "output_tokens", "latency_ms"} <= set(
            call
        )
    totals = item["usage_totals"]
    assert totals["agent_calls"] == len(item["agent_calls"])
    assert totals["input_tokens"] == sum(c["input_tokens"] for c in item["agent_calls"])

    expected = EXPECTED.get(sample.stem)
    if expected:
        status, revisions = expected
        assert item["status"] == status
        review = item["result"]["review"]
        assert (review["revisions"] if review else None) == revisions


def test_hallucinated_plan_step_is_dropped(pipeline):
    sample = ROOT / "samples" / "010-escalate-hallucinated-step.json"
    _, _, item = pipeline["submit"](json.loads(sample.read_text()))
    assert item["result"]["plan"]["rejected_steps"] == ["escalate_to_engineering"]
    assert item["result"]["plan"]["steps"] == ["classify", "extract", "draft_reply"]


def test_extraction_fields_come_from_the_registry(pipeline):
    sample = ROOT / "samples" / "001-sync-error.json"
    _, _, item = pipeline["submit"](json.loads(sample.read_text()))
    fields = item["result"]["extraction"]["fields"]
    assert fields == {
        "product_area": "sync",
        "error_code": "SYNC-409",
        "os": "windows",
        "app_version": "4.12.1",
    }


def test_redelivered_message_does_not_start_a_second_execution(pipeline):
    """Execution name = ticket_id makes the SQS -> Step Functions hop idempotent."""
    fns, sfn, arn = pipeline["fns"], pipeline["sfn"], pipeline["arn"]
    body = json.dumps(
        {
            "ticket_id": "6f1c2a8e-3b4d-4e5f-9a0b-1c2d3e4f5a6b",
            "ticket": {"subject": "s", "body": "b", "customer_tier": "free"},
        }
    )
    record = {
        "messageId": "m1",
        "receiptHandle": "r",
        "body": body,
        "attributes": {},
        "messageAttributes": {},
        "eventSource": "aws:sqs",
        "eventSourceARN": "arn:aws:sqs:us-east-1:123456789012:q",
        "awsRegion": REGION,
    }
    event = {"Records": [record, {**record, "messageId": "m2"}]}

    assert fns["starter"].lambda_handler(event, CTX) == {"batchItemFailures": []}
    assert len(sfn.list_executions(stateMachineArn=arn)["executions"]) == 1


def test_poison_message_is_reported_as_a_batch_item_failure(pipeline):
    """A bad record fails alone; the good one in the same batch still starts."""
    good = {
        "ticket_id": "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
        "ticket": {"subject": "s", "body": "b", "customer_tier": "free"},
    }

    def record(message_id, body):
        return {
            "messageId": message_id,
            "receiptHandle": "r",
            "body": body,
            "attributes": {},
            "messageAttributes": {},
            "eventSource": "aws:sqs",
            "eventSourceARN": "arn:aws:sqs:us-east-1:123456789012:q",
            "awsRegion": REGION,
        }

    event = {
        "Records": [record("bad-1", '{"ticket_id": "nope"}'), record("ok-1", json.dumps(good))]
    }
    resp = pipeline["fns"]["starter"].lambda_handler(event, CTX)
    assert resp == {"batchItemFailures": [{"itemIdentifier": "bad-1"}]}
    executions = pipeline["sfn"].list_executions(stateMachineArn=pipeline["arn"])["executions"]
    assert [e["name"] for e in executions] == [good["ticket_id"]]
