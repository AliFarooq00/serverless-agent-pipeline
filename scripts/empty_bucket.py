#!/usr/bin/env python3
"""Delete every object version and delete marker in a bucket (used by `make teardown`)."""

from __future__ import annotations

import sys

import boto3


def empty(bucket: str) -> int:
    s3 = boto3.client("s3")
    deleted = 0
    for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket):
        batch = [
            {"Key": v["Key"], "VersionId": v["VersionId"]}
            for v in page.get("Versions", []) + page.get("DeleteMarkers", [])
        ]
        for i in range(0, len(batch), 1000):
            s3.delete_objects(Bucket=bucket, Delete={"Objects": batch[i : i + 1000], "Quiet": True})
            deleted += len(batch[i : i + 1000])
    return deleted


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: empty_bucket.py <bucket>")
    print(f"deleted {empty(sys.argv[1])} object versions")
