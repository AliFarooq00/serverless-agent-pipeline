"""Shared fixtures.

* Puts the Lambda layer on sys.path exactly as /opt/python would be in Lambda.
* Loads each `functions/<name>/app.py` under a unique module name (they all share `app.py`).
* Creates moto resources from the *real* key schema in stacks/data.yaml so tests cannot drift
  from the infrastructure definition.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "layers" / "common"))
sys.path.insert(0, str(ROOT / "tests"))

REGION = "us-east-1"
TABLE = "triage-test"
BUCKET = "triage-archive-test"
QUEUE = "triage-ingest-test"

os.environ.update(
    {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "TABLE_NAME": TABLE,
        "BUCKET_NAME": BUCKET,
        "QUEUE_URL": f"https://sqs.{REGION}.amazonaws.com/123456789012/{QUEUE}",
        "STATE_MACHINE_ARN": f"arn:aws:states:{REGION}:123456789012:stateMachine:triage-test",
        "LLM_PROVIDER": "fake",
        "POWERTOOLS_TRACE_DISABLED": "1",
        "POWERTOOLS_SERVICE_NAME": "acme-triage-test",
        "POWERTOOLS_METRICS_NAMESPACE": "AcmeNotes/Triage/Test",
        "POWERTOOLS_DEV": "false",
        "EXTRACTION_REGISTRY_PATH": str(ROOT / "config" / "extraction_registry.yaml"),
    }
)


# --------------------------------------------------------------------------- helpers
class _CfnLoader(yaml.SafeLoader):
    """YAML loader that tolerates CloudFormation short-form tags (!Ref, !Sub, ...)."""


def _any_tag(loader, _suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


_CfnLoader.add_multi_constructor("!", _any_tag)


def load_cfn(path: str | Path) -> dict[str, Any]:
    with open(ROOT / path, encoding="utf-8") as fh:
        return yaml.load(fh, Loader=_CfnLoader)  # noqa: S506 - SafeLoader subclass


def load_function(name: str):
    """Import functions/<name>/app.py fresh, under a unique module name."""
    path = ROOT / "functions" / name / "app.py"
    module_name = f"fn_{name}_{uuid.uuid4().hex[:8]}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@dataclass
class FakeLambdaContext:
    function_name: str = "test-fn"
    memory_limit_in_mb: int = 512
    invoked_function_arn: str = f"arn:aws:lambda:{REGION}:123456789012:function:test-fn"
    aws_request_id: str = "00000000-0000-4000-8000-000000000000"

    def get_remaining_time_in_millis(self) -> int:
        return 30_000


@pytest.fixture
def lambda_context() -> FakeLambdaContext:
    return FakeLambdaContext(aws_request_id=str(uuid.uuid4()))


def api_event(
    method: str,
    path: str,
    body: Any = None,
    headers: dict[str, str] | None = None,
    path_params: dict[str, str] | None = None,
    resource: str | None = None,
) -> dict[str, Any]:
    return {
        "resource": resource or path,
        "path": path,
        "httpMethod": method,
        "headers": {"Content-Type": "application/json", **(headers or {})},
        "multiValueHeaders": {},
        "queryStringParameters": None,
        "multiValueQueryStringParameters": None,
        "pathParameters": path_params,
        "stageVariables": None,
        "requestContext": {
            "requestId": str(uuid.uuid4()),
            "stage": "test",
            "httpMethod": method,
            "path": path,
            "resourcePath": resource or path,
            "identity": {"sourceIp": "203.0.113.10"},
        },
        "body": body if body is None or isinstance(body, str) else json.dumps(body),
        "isBase64Encoded": False,
    }


# --------------------------------------------------------------------------- AWS (moto)
@pytest.fixture
def aws():
    """Moto-mocked account with the table, bucket and queues the stacks define."""
    import boto3
    from moto import mock_aws

    with mock_aws():
        table_props = load_cfn("stacks/data.yaml")["Resources"]["Table"]["Properties"]
        ddb = boto3.client("dynamodb", region_name=REGION)
        ddb.create_table(
            TableName=TABLE,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=table_props["AttributeDefinitions"],
            KeySchema=table_props["KeySchema"],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": g["IndexName"],
                    "KeySchema": g["KeySchema"],
                    "Projection": g["Projection"],
                }
                for g in table_props["GlobalSecondaryIndexes"]
            ],
        )
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET)
        sqs = boto3.client("sqs", region_name=REGION)
        sqs.create_queue(QueueName=QUEUE)
        yield {"dynamodb": ddb, "s3": s3, "sqs": sqs}


def receive_all(sqs, queue_url: str | None = None) -> list[dict[str, Any]]:
    url = queue_url or os.environ["QUEUE_URL"]
    out: list[dict[str, Any]] = []
    while True:
        resp = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10, WaitTimeSeconds=0)
        msgs = resp.get("Messages", [])
        if not msgs:
            return out
        out.extend(msgs)
        for m in msgs:
            sqs.delete_message(QueueUrl=url, ReceiptHandle=m["ReceiptHandle"])
