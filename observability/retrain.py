#!/usr/bin/env python3
"""
Scheduled IsolationForest retraining (AWS). Reads last N days of JSONL
telemetry from S3, retrains IForest, uploads new model.pkl.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
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
LOOKBACK_DAYS        = int(os.environ.get("RETRAIN_LOOKBACK_DAYS", "30"))
MIN_ROWS             = int(os.environ.get("MIN_RETRAIN_ROWS", "20"))


def load_recent_telemetry() -> list[dict]:
    import boto3
    s3 = boto3.client("s3")
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    rows = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_MODEL_BUCKET, Prefix=S3_TELEMETRY_PREFIX):
        for obj in page.get("Contents", []):
            if obj.get("LastModified") and obj["LastModified"] < cutoff:
                continue
            key = obj["Key"]
            if not key.endswith((".json", ".jsonl")):
                continue
            body = s3.get_object(Bucket=S3_MODEL_BUCKET, Key=key)["Body"].read().decode()
            for line in body.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
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
    logger.info("Loading last %d days of telemetry...", LOOKBACK_DAYS)
    rows = load_recent_telemetry()
    logger.info("Found %d rows", len(rows))

    if len(rows) < MIN_ROWS:
        print(f"⚠ Only {len(rows)} rows — not enough to retrain. Keeping existing model.")
        sys.exit(0)

    X = np.array([from_row(r) for r in rows])
    model = train_and_upload(X=X, bucket=S3_MODEL_BUCKET, key=S3_MODEL_KEY,
                              local_path="/tmp/iforest_retrained.pkl")

    print(f"\n✓ IsolationForest retrained on {len(X)} samples")
    print(f"✓ Model uploaded → s3://{S3_MODEL_BUCKET}/{S3_MODEL_KEY}")
    print(f"  Lambda will pick up new model on next cold start")


if __name__ == "__main__":
    main()
