#!/usr/bin/env python3
"""Send the synthetic tickets in samples/ to a deployed stack and (optionally) wait for results.

    # resolve URL + API key from the CloudFormation stack (needs AWS credentials)
    python scripts/send_samples.py --stack-name acme-notes-triage-dev --wait

    # or pass them explicitly
    python scripts/send_samples.py --api-url https://.../dev --api-key "$KEY" --only 002

Used as the post-deploy smoke test in .github/workflows/deploy.yml with --strict --check-auth.
Standard library only (boto3 is needed just for --stack-name).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {"COMPLETED", "NEEDS_HUMAN", "FAILED"}


def resolve_stack(stack_name: str, region: str | None) -> tuple[str, str]:
    import boto3

    cfn = boto3.client("cloudformation", region_name=region)
    outputs = {
        o["OutputKey"]: o["OutputValue"]
        for o in cfn.describe_stacks(StackName=stack_name)["Stacks"][0].get("Outputs", [])
    }
    key = boto3.client("apigateway", region_name=region).get_api_key(
        apiKey=outputs["ApiKeyId"], includeValue=True
    )["value"]
    return outputs["ApiUrl"].rstrip("/"), key


def request(method: str, url: str, api_key: str | None, body: dict | None = None, headers=None):
    req = urllib.request.Request(
        url,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "content-type": "application/json",
            **({"x-api-key": api_key} if api_key else {}),
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        try:
            return exc.code, json.loads(payload or b"{}")
        except json.JSONDecodeError:
            return exc.code, {"raw": payload.decode(errors="replace")}


def wait_for(base: str, key: str, ticket_id: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    delay = 1.0
    while True:
        status, body = request("GET", f"{base}/tickets/{ticket_id}", key)
        if status == 200 and body.get("status") in TERMINAL:
            return body
        if time.monotonic() > deadline:
            return {"status": f"TIMEOUT (last: {body.get('status', status)})"}
        time.sleep(delay)
        delay = min(delay * 1.5, 5.0)


def summarise(name: str, ticket_id: str, item: dict) -> str:
    result = item.get("result") or {}
    cls = result.get("classification") or {}
    review = result.get("review") or {}
    totals = item.get("usage_totals") or {}
    return (
        f"{name:<34} {ticket_id[:8]}  {item.get('status', '?'):<12} "
        f"{cls.get('category', '-'):<16} {cls.get('priority', '-'):<3} "
        f"rev={review.get('revisions', '-')!s:<2} "
        f"tokens={totals.get('input_tokens', 0)}/{totals.get('output_tokens', 0)}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--stack-name")
    parser.add_argument("--region")
    parser.add_argument("--api-url")
    parser.add_argument("--api-key")
    parser.add_argument("--only", action="append", default=[], help="sample name prefix")
    parser.add_argument("--wait", action="store_true", help="poll until each ticket finishes")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--strict", action="store_true", help="exit 1 unless all finish OK")
    parser.add_argument("--check-auth", action="store_true", help="assert 403 without API key")
    args = parser.parse_args()

    if args.stack_name:
        base, key = resolve_stack(args.stack_name, args.region)
    elif args.api_url and args.api_key:
        base, key = args.api_url.rstrip("/"), args.api_key
    else:
        parser.error("pass --stack-name, or both --api-url and --api-key")

    failures = 0
    if args.check_auth:
        status, _ = request(
            "POST", f"{base}/tickets", None, {"subject": "x", "body": "x", "customer_tier": "free"}
        )
        ok = status == 403
        failures += not ok
        print(f"{'ok ' if ok else 'BAD'} unauthenticated POST -> {status} (expected 403)")

    samples = sorted((ROOT / "samples").glob("*.json"))
    if args.only:
        samples = [p for p in samples if any(p.stem.startswith(o) for o in args.only)]

    submitted: list[tuple[str, str]] = []
    run_id = uuid.uuid4().hex[:8]
    for path in samples:
        ticket = json.loads(path.read_text())
        # A per-run key keeps reruns from being de-duplicated into the previous run's tickets.
        status, body = request(
            "POST",
            f"{base}/tickets",
            key,
            ticket,
            {"Idempotency-Key": f"sample-{run_id}-{path.stem}"},
        )
        if status != 202:
            failures += 1
            print(f"BAD {path.stem}: POST -> {status} {body}")
            continue
        submitted.append((path.stem, body["ticket_id"]))
        print(f"sent {path.stem} -> {body['ticket_id']}")

    if args.wait:
        print()
        for name, ticket_id in submitted:
            item = wait_for(base, key, ticket_id, args.timeout)
            print(summarise(name, ticket_id, item))
            if item.get("status") not in {"COMPLETED", "NEEDS_HUMAN"}:
                failures += 1

    if args.strict and failures:
        print(f"\n{failures} problem(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
