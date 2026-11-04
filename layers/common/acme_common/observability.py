"""Powertools singletons so every function logs, traces and emits metrics the same way."""

from __future__ import annotations

import os

from aws_lambda_powertools import Logger, Metrics, Tracer

SERVICE = os.environ.get("POWERTOOLS_SERVICE_NAME", "acme-triage")
NAMESPACE = os.environ.get("POWERTOOLS_METRICS_NAMESPACE", "AcmeNotes/Triage")

logger = Logger(service=SERVICE)
tracer = Tracer(service=SERVICE)
metrics = Metrics(namespace=NAMESPACE, service=SERVICE)
