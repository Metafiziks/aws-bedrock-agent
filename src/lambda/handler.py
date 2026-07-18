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

    session_id = body.get("sessionId", str(uuid.uuid4()))

    # ── Invoke Bedrock Agent ─────────────────────────────────────────────────
    search_start = time.monotonic()
    response = bedrock.invoke_agent(
        agentId=AGENT_ID,
        agentAliasId=AGENT_ALIAS_ID,
        sessionId=session_id,
        inputText=message,
    )

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
    )

    # ── Defense-in-depth fallback ─────────────────────────────────────────────
    if not seen_urls and _HEDGE_PATTERNS.search(answer):
        answer = NOT_FOUND_MESSAGE

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"answer": answer, "sessionId": session_id}),
    }
