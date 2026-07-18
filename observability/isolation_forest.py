"""
IsolationForest wrapper for AWS — train, persist to S3, load for scoring.
"""
from __future__ import annotations

import io
import logging
import os
import pickle
from typing import Optional

import numpy as np
from sklearn.ensemble import IsolationForest

logger = logging.getLogger(__name__)

CONTAMINATION = float(os.environ.get("IFOREST_CONTAMINATION", "0.05"))


def train(X: np.ndarray, contamination: float = CONTAMINATION) -> IsolationForest:
    if len(X) < 10:
        raise ValueError(f"Need at least 10 samples, got {len(X)}")
    model = IsolationForest(n_estimators=100, contamination=contamination, random_state=42, n_jobs=-1)
    model.fit(X)
    logger.info("IForest trained: %d samples, %d features", len(X), X.shape[1])
    return model


def save_local(model: IsolationForest, path: str) -> None:
    with open(path, "wb") as f:
        pickle.dump(model, f)


def upload_to_s3(local_path: str, bucket: str, key: str) -> None:
    import boto3
    boto3.client("s3").upload_file(local_path, bucket, key)
    logger.info("IForest model uploaded: s3://%s/%s", bucket, key)


def load_from_s3(bucket: str, key: str) -> Optional[IsolationForest]:
    try:
        import boto3
        buf = io.BytesIO()
        boto3.client("s3").download_fileobj(bucket, key, buf)
        return pickle.loads(buf.getvalue())
    except Exception as exc:
        logger.warning("Could not load IForest from S3: %s", exc)
        return None


def train_and_upload(
    X: np.ndarray,
    bucket: str,
    key: str,
    local_path: str = "/tmp/iforest.pkl",
    contamination: float = CONTAMINATION,
) -> IsolationForest:
    model = train(X, contamination=contamination)
    save_local(model, local_path)
    upload_to_s3(local_path, bucket, key)
    return model


def score_features(model: IsolationForest, features: np.ndarray) -> tuple[float, bool]:
    x = features.reshape(1, -1)
    return float(model.decision_function(x)[0]), int(model.predict(x)[0]) == -1
