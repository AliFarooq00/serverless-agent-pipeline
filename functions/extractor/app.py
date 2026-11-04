"""Extractor agent: pulls the fields declared in config/extraction_registry.yaml.

There is no field-specific code here. Schema, prompt and validation all come from the registry.
"""

from __future__ import annotations

from acme_common.agent import run_agent
from acme_common.llm import get_llm_client
from acme_common.observability import logger, metrics, tracer
from acme_common.prompts import UNTRUSTED_INPUT_RULE, render_ticket
from acme_common.registry import load_registry

SYSTEM_TEMPLATE = """You extract structured fields from Acme Notes support tickets.
Return every field. Use null when the ticket does not state the value - never guess.
Fields:
{fields}
{rule}"""


@logger.inject_lambda_context(clear_state=True)
@tracer.capture_lambda_handler
@metrics.log_metrics(raise_on_empty_metrics=False)
def lambda_handler(event: dict, _context) -> dict:
    logger.append_keys(ticket_id=event.get("ticket_id"))
    registry = load_registry()
    data, usage = run_agent(
        "extractor",
        llm=get_llm_client(),
        system=SYSTEM_TEMPLATE.format(fields=registry.prompt_block(), rule=UNTRUSTED_INPUT_RULE),
        user=render_ticket(event["ticket"]),
        schema=registry.json_schema(),
        schema_name="extraction",
        max_tokens=400,
    )
    fields, warnings = registry.coerce(data)
    if warnings:
        logger.warning("extracted values failed registry validation", extra={"fields": warnings})
    return {
        "step": "extract",
        "output": {
            "fields": fields,
            "registry_version": registry.version,
            "rejected_values": warnings,
        },
        "usage": usage,
    }
