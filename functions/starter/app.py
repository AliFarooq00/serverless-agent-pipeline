"""SQS -> Step Functions. One STANDARD execution per ticket, named after the ticket id.

Execution names are unique per state machine (for 90 days), so SQS redelivery or a duplicate
message can never start a second workflow for the same ticket: `ExecutionAlreadyExists` is
treated as success. Failures are reported per message (partial batch response), so one bad
record doesn't force the whole batch to be retried.
"""

from __future__ import annotations

import json
import os

import boto3
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.batch import (
    BatchProcessor,
    EventType,
    process_partial_response,
)
from aws_lambda_powertools.utilities.data_classes.sqs_event import SQSRecord

from acme_common.models import is_valid_ticket_id
from acme_common.observability import logger, metrics, tracer

STATE_MACHINE_ARN = os.environ["STATE_MACHINE_ARN"]

_sfn = boto3.client("stepfunctions")
processor = BatchProcessor(event_type=EventType.SQS)


class PoisonMessageError(ValueError):
    """Malformed message - retrying won't help; it will land in the DLQ after maxReceiveCount."""


@tracer.capture_method
def start_execution(record: SQSRecord) -> str:
    message = json.loads(record.body)
    ticket_id = message.get("ticket_id", "")
    if not is_valid_ticket_id(ticket_id) or not isinstance(message.get("ticket"), dict):
        raise PoisonMessageError(f"invalid message {record.message_id}")

    execution_input = {
        "ticket_id": ticket_id,
        "received_at": message.get("received_at"),
        "raw_s3_key": message.get("raw_s3_key"),
        "ticket": message["ticket"],
    }
    kwargs = {
        "stateMachineArn": STATE_MACHINE_ARN,
        "name": ticket_id,
        "input": json.dumps(execution_input),
    }
    trace_header = os.environ.get("_X_AMZN_TRACE_ID")
    if trace_header:  # stitch the X-Ray trace across SQS -> Lambda -> Step Functions
        kwargs["traceHeader"] = trace_header[:256]
    try:
        resp = _sfn.start_execution(**kwargs)
    except _sfn.exceptions.ExecutionAlreadyExists:
        logger.info("execution already exists - duplicate delivery", extra={"ticket_id": ticket_id})
        metrics.add_metric(name="DuplicateStartsSkipped", unit=MetricUnit.Count, value=1)
        return "duplicate"

    metrics.add_metric(name="ExecutionsStarted", unit=MetricUnit.Count, value=1)
    logger.info("execution started", extra={"ticket_id": ticket_id, "arn": resp["executionArn"]})
    return resp["executionArn"]


@logger.inject_lambda_context(clear_state=True)
@tracer.capture_lambda_handler
@metrics.log_metrics(raise_on_empty_metrics=False)
def lambda_handler(event: dict, context) -> dict:
    return process_partial_response(
        event=event, record_handler=start_execution, processor=processor, context=context
    )
