#!/usr/bin/env python3
"""
Bootstrap IsolationForest training from eval telemetry (AWS).

Reads TELEMETRY JSON lines from S3 (written by CloudWatch → S3 export),
trains an IForest, and uploads model.pkl back to S3 for the Lambda to load.

Usage:
    S3_MODEL_BUCKET=my-bucket \
    S3_MODEL_KEY=models/iforest.pkl \
    S3_TELEMETRY_PREFIX=telemetry/ \
    python3 observability/train_baseline.py
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))
from observability.isolation_forest import train_and_upload
from observability.shared.features import from_bq_row as from_row

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

S3_MODEL_BUCKET      = os.environ["S3_MODEL_BUCKET"]
S3_MODEL_KEY         = os.environ.get("S3_MODEL_KEY", "models/iforest.pkl")
S3_TELEMETRY_PREFIX  = os.environ.get("S3_TELEMETRY_PREFIX", "telemetry/")
MIN_ROWS             = int(os.environ.get("MIN_BASELINE_ROWS", "20"))


def load_telemetry_from_s3() -> list[dict]:
    """Download all JSONL telemetry files from S3 prefix."""
    import boto3
    s3 = boto3.client("s3")
    rows = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_MODEL_BUCKET, Prefix=S3_TELEMETRY_PREFIX):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith(".json") and not key.endswith(".jsonl"):
                continue
            body = s3.get_object(Bucket=S3_MODEL_BUCKET, Key=key)["Body"].read().decode()
            for line in body.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    # CloudWatch Firehose wraps lines: extract TELEMETRY prefix lines
                    if line.startswith("TELEMETRY "):
                        row = json.loads(line[len("TELEMETRY "):])
                    else:
                        row = json.loads(line)
                    if row.get("retrieval_score_mean") is not None:
                        rows.append(row)
                except (json.JSONDecodeError, KeyError):
                    continue
    return rows


def main() -> None:
    logger.info("Loading telemetry from s3://%s/%s ...", S3_MODEL_BUCKET, S3_TELEMETRY_PREFIX)
    rows = load_telemetry_from_s3()
    logger.info("Found %d telemetry rows", len(rows))

    if len(rows) < MIN_ROWS:
        logger.error("Only %d rows (need >= %d). Run evals first.", len(rows), MIN_ROWS)
        sys.exit(1)

    X = np.array([from_row(r) for r in rows])
    logger.info("Feature matrix: %s", X.shape)

    model = train_and_upload(
        X=X,
        bucket=S3_MODEL_BUCKET,
        key=S3_MODEL_KEY,
        local_path="/tmp/iforest_baseline.pkl",
    )
    print(f"\n✓ IsolationForest trained on {len(X)} samples")
    print(f"✓ Model uploaded → s3://{S3_MODEL_BUCKET}/{S3_MODEL_KEY}")
    print(f"  Lambda will pick it up on next cold start")


if __name__ == "__main__":
    main()
