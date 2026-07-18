"""
CloudWatch structured telemetry sink for the Lambda agent.

Emits a JSON log line to CloudWatch Logs (via the Lambda logging sink —
Lambda automatically ships all stdout/stderr to CloudWatch). A separate
CloudWatch subscription filter + Kinesis Firehose → S3 export collects
these rows for offline IsolationForest training.

This is intentionally simple: just json.dumps to stdout with a known prefix
so the subscription filter can select only telemetry lines.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import threading
from typing import Optional

logger = logging.getLogger(__name__)

LOG_PREFIX = "TELEMETRY"  # subscription filter matches this prefix


def _emit(row: dict) -> None:
    """Write a JSON telemetry line to stdout (CloudWatch captures it)."""
    try:
        print(f"{LOG_PREFIX} {json.dumps(row)}", flush=True)
    except Exception as exc:
        logger.warning("Telemetry emit failed (non-fatal): %s", exc)


def log_async(
    *,
    request_id: str,
    query: str,
    source: str = "runtime",
    search_latency_ms: float,
    retrieval_score_mean: float,
    retrieval_score_std: float,
    retrieval_score_entropy: float,
    chunk_count: int,
    reranker_score_mean: float,
    anomaly_score: float,
    is_anomaly: bool,
    is_baseline: bool = False,
    answer_length: Optional[int] = None,
    citation_count: Optional[int] = None,
    hhem_score: Optional[float] = None,
    latency_ms: Optional[float] = None,
) -> None:
    """
    Emit a telemetry row. In Lambda, CloudWatch captures all print() output,
    so we log synchronously (no daemon thread needed — Lambda is single-threaded).
    """
    row = {
        "timestamp":               datetime.datetime.utcnow().isoformat() + "Z",
        "request_id":              request_id,
        "query":                   query[:1024],
        "source":                  source,
        "search_latency_ms":       round(search_latency_ms, 2),
        "retrieval_score_mean":    round(retrieval_score_mean, 6),
        "retrieval_score_std":     round(retrieval_score_std, 6),
        "retrieval_score_entropy": round(retrieval_score_entropy, 6),
        "chunk_count":             chunk_count,
        "reranker_score_mean":     round(reranker_score_mean, 6),
        "anomaly_score":           round(anomaly_score, 6),
        "is_anomaly":              is_anomaly,
        "is_baseline":             is_baseline,
        "answer_length":           answer_length,
        "citation_count":          citation_count,
        "hhem_score":              round(hhem_score, 6) if hhem_score is not None else None,
        "latency_ms":              round(latency_ms, 2) if latency_ms is not None else None,
    }
    _emit(row)
