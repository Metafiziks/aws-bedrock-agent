"""
IsolationForest scorer for the AWS Lambda agent.
Loads model from S3 on first invocation (lazy, module-level cache = Lambda warm start).
Falls back gracefully if model not yet trained or S3 unreachable.
"""
from __future__ import annotations

import io
import logging
import math
import os
import pickle
import threading

import numpy as np

logger = logging.getLogger(__name__)

S3_MODEL_BUCKET = os.environ.get("S3_MODEL_BUCKET", "")
S3_MODEL_KEY    = os.environ.get("S3_MODEL_KEY", "models/iforest.pkl")

_model = None
_load_lock = threading.Lock()


def _load_model():
    global _model
    if _model is not None:
        return _model
    with _load_lock:
        if _model is not None:
            return _model
        if not S3_MODEL_BUCKET:
            return None
        try:
            import boto3
            s3 = boto3.client("s3")
            buf = io.BytesIO()
            s3.download_fileobj(S3_MODEL_BUCKET, S3_MODEL_KEY, buf)
            _model = pickle.loads(buf.getvalue())
            logger.info("IForest loaded from s3://%s/%s", S3_MODEL_BUCKET, S3_MODEL_KEY)
        except Exception as exc:
            logger.warning("IForest load failed (non-fatal): %s", exc)
    return _model


def _entropy(scores: list[float]) -> float:
    if not scores or sum(scores) == 0:
        return 0.0
    total = sum(scores)
    probs = [s / total for s in scores]
    return -sum(p * math.log(p + 1e-10) for p in probs)


def score(
    retrieval_scores: list[float],
    reranker_scores: list[float],
    search_latency_ms: float,
) -> tuple[float, bool]:
    """Returns (anomaly_score, is_anomaly). Returns (0.0, False) on error."""
    model = _load_model()
    if model is None:
        return 0.0, False
    try:
        rs = retrieval_scores or [0.0]
        rr = reranker_scores or [0.0]
        x = np.array([
            float(np.mean(rs)),
            float(np.std(rs)) if len(rs) > 1 else 0.0,
            _entropy(rs),
            float(len(rs)),
            float(np.mean(rr)),
            search_latency_ms,
        ], dtype=np.float64).reshape(1, -1)
        decision = float(model.decision_function(x)[0])
        label    = int(model.predict(x)[0])
        return decision, label == -1
    except Exception as exc:
        logger.warning("IForest score failed (non-fatal): %s", exc)
        return 0.0, False
