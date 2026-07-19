import json
import math
import os
import re
import time
import uuid

import boto3
import numpy as np

import cloudwatch_sink
import iforest_scorer

bedrock     = boto3.client("bedrock-agent-runtime", region_name=os.environ.get("AWS_REGION", "us-east-1"))
bedrock_rt  = boto3.client("bedrock-runtime",       region_name=os.environ.get("AWS_REGION", "us-east-1"))

AGENT_ID       = os.environ["AGENT_ID"]
AGENT_ALIAS_ID = os.environ["AGENT_ALIAS_ID"]
RERANK_MODEL   = os.environ.get("BEDROCK_RERANK_MODEL", "amazon.rerank-v1:0")
MEMORY_ENABLED = os.environ.get("MEMORY_ENABLED", "false").lower() == "true"
MEMORY_RETENTION_DAYS = int(os.environ.get("MEMORY_RETENTION_DAYS", "30"))
MEMORY_DEFAULT_ID_MODE = os.environ.get("MEMORY_DEFAULT_ID_MODE", "explicit").lower()

NOT_FOUND_MESSAGE = (
    "I couldn't find information about that in the available documentation. "
    "Please check with your supervisor or try rephrasing your question with "
    "more specific terms (e.g. include the equipment name)."
)

_HEDGE_PATTERNS = re.compile(
    r"(cannot provide|more information|more details|without knowing|"
    r"please provide|clarif|insufficient|not contain|unable to)",
    re.IGNORECASE,
)

_BEDROCK_ID_PATTERN = re.compile(r"^[0-9a-zA-Z._:-]+$")


def _coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def _validate_bedrock_id(value, field_name: str, max_length: int = 100) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    if len(normalized) < 2 or len(normalized) > max_length or not _BEDROCK_ID_PATTERN.match(normalized):
        raise ValueError(
            f"{field_name} must be 2-{max_length} characters and match [0-9a-zA-Z._:-]+"
        )
    return normalized


def _resolve_memory_id(body: dict, session_id: str) -> tuple[str | None, str]:
    explicit_memory_id = _validate_bedrock_id(body.get("memoryId") or body.get("memory_id"), "memoryId")
    if explicit_memory_id:
        return explicit_memory_id, "explicit"

    if MEMORY_DEFAULT_ID_MODE == "user":
        user_id = _validate_bedrock_id(body.get("userId") or body.get("user_id"), "userId", max_length=95)
        if user_id:
            return f"user:{user_id}", "user"
    elif MEMORY_DEFAULT_ID_MODE == "session":
        scoped_session_id = _validate_bedrock_id(session_id, "sessionId", max_length=92)
        if scoped_session_id:
            return f"session:{scoped_session_id}", "session"

    return None, MEMORY_DEFAULT_ID_MODE


def _s3_to_https(uri):
    if not uri or not uri.startswith("s3://"):
        return uri
    parts = uri[5:].split("/", 1)
    return f"https://{parts[0]}.s3.amazonaws.com/{parts[1] if len(parts) > 1 else ''}"


def _rerank(query: str, text_sources: list[str]) -> list[float]:
    """
    Call Amazon Bedrock Rerank API to re-score text sources for a query.
    Returns a list of scores in the same order as text_sources.
    Falls back to uniform scores on error.
    """
    if not text_sources:
        return []
    try:
        response = bedrock_rt.rerank(
            rerankingConfiguration={
                "type": "BEDROCK_RERANKING_MODEL",
                "bedrockRerankingConfiguration": {
                    "modelConfiguration": {"modelArn": f"arn:aws:bedrock:{os.environ.get('AWS_REGION','us-east-1')}::foundation-model/{RERANK_MODEL}"},
                    "numberOfResults": len(text_sources),
                },
            },
            sources=[
                {"type": "INLINE", "inlineDocumentSource": {"type": "TEXT", "textDocument": {"text": t}}}
                for t in text_sources
            ],
            queries=[{"type": "TEXT", "textQuery": {"text": query}}],
        )
        scores = [0.0] * len(text_sources)
        for item in response.get("rerankingResults", []):
            idx = item.get("index", 0)
            if 0 <= idx < len(scores):
                scores[idx] = float(item.get("relevanceScore", 0.0))
        return scores
    except Exception as exc:
        # Rerank API availability varies by region; fall back gracefully
        import logging
        logging.getLogger(__name__).warning("Bedrock Rerank unavailable: %s", exc)
        return [1.0 / (i + 1) for i in range(len(text_sources))]


def lambda_handler(event, context):
    body       = json.loads(event.get("body") or "{}")
    message    = body.get("message", "")
    request_id = str(uuid.uuid4())

    if not message:
        return {"statusCode": 400, "body": json.dumps({"error": "message field required"})}

    try:
        session_id = _validate_bedrock_id(body.get("sessionId", str(uuid.uuid4())), "sessionId")
        memory_id, memory_id_mode = _resolve_memory_id(body, session_id) if MEMORY_ENABLED else (None, "disabled")
    except ValueError as exc:
        return {"statusCode": 400, "body": json.dumps({"error": str(exc)})}

    end_session = _coerce_bool(body.get("endSession", False))
    if MEMORY_ENABLED and end_session and not memory_id:
        return {
            "statusCode": 400,
            "body": json.dumps({"error": "endSession requires memoryId, userId, or MEMORY_DEFAULT_ID_MODE=session"}),
        }

    # ── Invoke Bedrock Agent ─────────────────────────────────────────────────
    search_start = time.monotonic()
    invoke_args = {
        "agentId": AGENT_ID,
        "agentAliasId": AGENT_ALIAS_ID,
        "sessionId": session_id,
        "inputText": message,
    }
    if MEMORY_ENABLED and memory_id:
        invoke_args["memoryId"] = memory_id
        invoke_args["endSession"] = end_session

    response = bedrock.invoke_agent(**invoke_args)

    answer       = ""
    seen_urls    = []
    raw_chunks   = []   # (text, url, title)
    raw_scores   = []   # retrieval relevance from citation metadata

    for evt in response["completion"]:
        if "chunk" not in evt:
            continue
        chunk = evt["chunk"]
        answer += chunk["bytes"].decode("utf-8")

        for citation in chunk.get("attribution", {}).get("citations", []):
            for ref in citation.get("retrievedReferences", []):
                loc     = ref.get("location", {})
                uri     = loc.get("s3Location", {}).get("uri") or loc.get("uri", "")
                url     = _s3_to_https(uri)
                title   = ref.get("metadata", {}).get("title") or uri.rsplit("/", 1)[-1]
                score   = float(ref.get("score", 0.0))
                content = ref.get("content", {}).get("text", "")
                if url and url not in seen_urls:
                    seen_urls.append(url)
                    answer += f"\n- [{title}]({url})"
                raw_chunks.append(content or title)
                raw_scores.append(score)

    search_latency_ms = (time.monotonic() - search_start) * 1000
    response_memory_id = (
        response.get("memoryId")
        or response.get("ResponseMetadata", {}).get("HTTPHeaders", {}).get("x-amz-bedrock-agent-memory-id")
        or memory_id
    )

    # ── Bedrock Rerank: score the retrieved chunks ────────────────────────────
    reranker_scores = _rerank(message, raw_chunks) if raw_chunks else []

    # ── IsolationForest anomaly detection ────────────────────────────────────
    anomaly_score, is_anomaly = iforest_scorer.score(
        retrieval_scores=raw_scores or [0.0],
        reranker_scores=reranker_scores or [0.0],
        search_latency_ms=search_latency_ms,
    )

    # ── Telemetry ─────────────────────────────────────────────────────────────
    rs = raw_scores or [0.0]
    n = len(rs)
    mean_r  = sum(rs) / n
    std_r   = math.sqrt(sum((s - mean_r) ** 2 for s in rs) / n)
    total   = sum(rs) or 1e-10
    ent_r   = -sum((s / total) * math.log(s / total + 1e-10) for s in rs)

    cloudwatch_sink.log_async(
        request_id=request_id,
        query=message,
        source="runtime",
        search_latency_ms=search_latency_ms,
        retrieval_score_mean=mean_r,
        retrieval_score_std=std_r,
        retrieval_score_entropy=ent_r,
        chunk_count=n,
        reranker_score_mean=sum(reranker_scores) / len(reranker_scores) if reranker_scores else 0.0,
        anomaly_score=anomaly_score,
        is_anomaly=is_anomaly,
        memory_enabled=MEMORY_ENABLED,
        memory_id_present=bool(memory_id),
        memory_id_mode=memory_id_mode,
        memory_session_ended=end_session if memory_id else False,
        memory_retention_days=MEMORY_RETENTION_DAYS if MEMORY_ENABLED else None,
        memory_read_count=1 if memory_id and not end_session else 0,
        memory_write_count=1 if memory_id and end_session else 0,
        memory_summary_count=1 if memory_id and end_session else 0,
        memory_latency_ms=search_latency_ms if memory_id else None,
    )

    # ── Defense-in-depth fallback ─────────────────────────────────────────────
    if not seen_urls and _HEDGE_PATTERNS.search(answer):
        answer = NOT_FOUND_MESSAGE

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({
            "answer": answer,
            "sessionId": session_id,
            "memoryEnabled": MEMORY_ENABLED,
            "memoryId": response_memory_id if MEMORY_ENABLED else None,
            "endSession": end_session if MEMORY_ENABLED else False,
        }),
    }
