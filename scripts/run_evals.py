#!/usr/bin/env python3
"""
RAG Agent Evaluation Runner
----------------------------
Evaluates the Bedrock Agent Lambda endpoint against a fixed test suite.

Metrics:
  keyword_recall   (deterministic) - fraction of expected keywords found in the answer
  citation_recall  (deterministic) - expected source doc appeared in citations (0 or 1)
  latency_ms       (deterministic) - wall-clock time for the Lambda response
  faithfulness     (LLM-as-judge)  - every claim grounded in cited sources (0-1)
  answer_relevance (LLM-as-judge)  - answer fully addresses the question (0-1)
  hhem_score       (ML model)      - hallucination probability 0-1 (lower = better)
  anomaly_score    (ML model)      - IsolationForest anomaly signal (logged, not gated)

Judge model: amazon.nova-pro-v1:0 (separate from the agent's nova-lite-v1:0)
HHEM model:  vectara/hallucination_evaluation_model (local inference)

Pass thresholds (configurable via env vars):
  THRESHOLD_FAITHFULNESS    default 0.70
  THRESHOLD_RELEVANCE       default 0.75
  THRESHOLD_CITATION_RECALL default 0.60
  THRESHOLD_KEYWORD_RECALL  default 0.65
  THRESHOLD_P95_LATENCY_MS  default 8000

Usage:
  LAMBDA_URL=https://... AWS_REGION=us-east-1 python3 scripts/run_evals.py
  LAMBDA_URL=https://... AWS_REGION=us-east-1 python3 scripts/run_evals.py --output eval_results.json

Env vars for ML observability:
  EVAL_IS_BASELINE=true       - tag rows as baseline (used by train_baseline.py)
  S3_MODEL_BUCKET             - S3 bucket for telemetry export
  S3_TELEMETRY_PREFIX         - S3 prefix for telemetry JSONL (default: telemetry/)
  SKIP_HHEM=true              - skip HHEM scoring (saves ~2 GB RAM + download)
  MEMORY_EVAL_ENABLED=true    - run memory-specific cases; otherwise skip them
  MEMORY_EVAL_WAIT_SECONDS=20 - wait between memory summary recall attempts
  MEMORY_EVAL_MAX_ATTEMPTS=6  - retry count for async memory summarization
"""

import argparse
import json
import math
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import boto3
import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LAMBDA_URL = os.environ.get("LAMBDA_URL", "").rstrip("/")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
JUDGE_MODEL = os.environ.get("JUDGE_MODEL_ID", "amazon.nova-pro-v1:0")

THRESHOLDS = {
    "faithfulness":    float(os.environ.get("THRESHOLD_FAITHFULNESS",    "0.70")),
    "answer_relevance": float(os.environ.get("THRESHOLD_RELEVANCE",       "0.75")),
    "citation_recall": float(os.environ.get("THRESHOLD_CITATION_RECALL", "0.60")),
    "keyword_recall":  float(os.environ.get("THRESHOLD_KEYWORD_RECALL",  "0.65")),
    "p95_latency_ms":  float(os.environ.get("THRESHOLD_P95_LATENCY_MS",  "8000")),
}

EVAL_CASES_PATH  = Path(__file__).parent.parent / "tests" / "eval_cases.json"
DEFAULT_OUTPUT   = Path(__file__).parent.parent / "eval_results.json"
EVAL_IS_BASELINE = os.environ.get("EVAL_IS_BASELINE", "false").lower() == "true"
MEMORY_EVAL_ENABLED = os.environ.get("MEMORY_EVAL_ENABLED", "false").lower() == "true"
MEMORY_EVAL_WAIT_SECONDS = int(os.environ.get("MEMORY_EVAL_WAIT_SECONDS", "20"))
MEMORY_EVAL_MAX_ATTEMPTS = int(os.environ.get("MEMORY_EVAL_MAX_ATTEMPTS", "6"))

# ---------------------------------------------------------------------------
# HHEM scoring (lazy load to avoid import penalty when skipped)
# ---------------------------------------------------------------------------

_hhem = None

def _get_hhem():
    global _hhem
    if _hhem is None:
        try:
            sys.path.insert(0, str(Path(__file__).parent.parent))
            from observability.shared.hhem import HHEMScorer
            _hhem = HHEMScorer()
        except Exception as exc:
            print(f"  [HHEM unavailable: {exc}]", file=sys.stderr)
            _hhem = None
    return _hhem

def score_hhem(question: str, answer: str) -> float | None:
    """Return hallucination probability 0-1, or None on error."""
    if os.environ.get("SKIP_HHEM", "false").lower() == "true":
        return None
    scorer = _get_hhem()
    if scorer is None:
        return None
    try:
        return scorer.score(question, answer)
    except Exception:
        return None

# ---------------------------------------------------------------------------
# S3 telemetry sink (fire-and-forget per eval case)
# ---------------------------------------------------------------------------

def _log_telemetry_s3(row: dict) -> None:
    """Append a JSONL telemetry row to S3 if S3_MODEL_BUCKET is set."""
    bucket = os.environ.get("S3_MODEL_BUCKET")
    if not bucket:
        return
    try:
        import threading
        s3 = boto3.client("s3", region_name=AWS_REGION)
        prefix = os.environ.get("S3_TELEMETRY_PREFIX", "telemetry/")
        ts = datetime.now(timezone.utc)
        key = f"{prefix}evals/{ts.strftime('%Y/%m/%d')}/eval_{ts.strftime('%H%M%S')}_{row['request_id'][:8]}.jsonl"
        body = json.dumps(row) + "\n"
        def _upload():
            try:
                s3.put_object(Bucket=bucket, Key=key, Body=body.encode())
            except Exception:
                pass
        threading.Thread(target=_upload, daemon=True).start()
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Lambda caller
# ---------------------------------------------------------------------------

def call_agent(
    question: str,
    *,
    session_id: str | None = None,
    memory_id: str | None = None,
    user_id: str | None = None,
    end_session: bool = False,
) -> tuple[str, list[str], float, dict]:
    """POST to the Lambda URL.  Returns (answer, citations, latency_ms)."""
    payload = {"message": question}
    if session_id:
        payload["sessionId"] = session_id
    if memory_id:
        payload["memoryId"] = memory_id
    if user_id:
        payload["userId"] = user_id
    if end_session:
        payload["endSession"] = True

    start = time.monotonic()
    resp = requests.post(LAMBDA_URL, json=payload, timeout=30)
    latency_ms = (time.monotonic() - start) * 1000
    resp.raise_for_status()
    body = resp.json()
    answer = body.get("answer", "")
    citations = re.findall(r"\[([^\]]+\.txt)\]", answer)
    return answer, citations, latency_ms, body

# ---------------------------------------------------------------------------
# Deterministic scorers
# ---------------------------------------------------------------------------

def score_keyword_recall(answer: str, expected_keywords: list[str]) -> float:
    if not expected_keywords:
        return 1.0
    answer_lower = answer.lower()
    hits = sum(1 for kw in expected_keywords if kw.lower() in answer_lower)
    return hits / len(expected_keywords)

def score_citation_recall(citations: list[str], expected_sources: list[str]) -> float:
    if not expected_sources:
        return 1.0
    citations_lower = [c.lower() for c in citations]
    for src in expected_sources:
        if src.lower() in citations_lower:
            return 1.0
    return 0.0

# ---------------------------------------------------------------------------
# LLM-as-judge scorer (Nova Pro)
# ---------------------------------------------------------------------------

JUDGE_PROMPT = """\
You are an expert evaluator for a RAG (Retrieval-Augmented Generation) system \
used in a manufacturing environment.

Question: {question}

Answer given: {answer}

Source documents cited: {citations}

Score the answer on BOTH of the following metrics using an integer from 1 to 5:

faithfulness - Are ALL factual claims in the answer directly supported by the \
cited source documents? No invented or extrapolated information.
  1 = significant fabrications present
  2 = several unsupported claims
  3 = mostly grounded, a few questionable details
  4 = nearly all claims traceable to sources
  5 = every claim is directly traceable to the cited sources

answer_relevance - Does the answer fully and directly address the question asked?
  1 = off-topic or does not address the question
  2 = tangentially related but misses the main point
  3 = partially addresses the question, missing key aspects
  4 = mostly complete, minor gaps
  5 = fully and directly addresses the question

Return ONLY a valid JSON object with exactly these keys - no markdown, no explanation outside the JSON:
{{"faithfulness": <integer 1-5>, "answer_relevance": <integer 1-5>, "reasoning": "<one sentence>"}}"""


def call_judge(question: str, answer: str, citations: list[str]) -> dict:
    bedrock = boto3.client("bedrock-runtime", region_name=AWS_REGION)
    citation_str = ", ".join(citations) if citations else "none"
    prompt = JUDGE_PROMPT.format(question=question, answer=answer, citations=citation_str)
    body = json.dumps({
        "messages": [{"role": "user", "content": [{"text": prompt}]}],
        "inferenceConfig": {"maxTokens": 512, "temperature": 0},
    })
    for attempt in range(4):
        try:
            resp = bedrock.invoke_model(modelId=JUDGE_MODEL, body=body)
            raw = json.loads(resp["body"].read())
            text = raw["output"]["message"]["content"][0]["text"].strip()
            break
        except bedrock.exceptions.ThrottlingException:
            wait = 10 * (2 ** attempt)
            print(f" [throttled, retrying in {wait}s]", end="", flush=True)
            time.sleep(wait)
            if attempt == 3:
                raise
    text = re.sub(r"^```json\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    scores = json.loads(text)
    return {
        "faithfulness":    (scores["faithfulness"]    - 1) / 4,
        "answer_relevance": (scores["answer_relevance"] - 1) / 4,
        "reasoning": scores.get("reasoning", ""),
    }

# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------

def percentile(values: list[float], p: int) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    idx = max(0, int(len(sorted_vals) * p / 100) - 1)
    return sorted_vals[idx]

def build_summary(cases: list[dict]) -> dict:
    def mean(key):
        vals = [
            c["scores"][key]
            for c in cases
            if not c.get("skipped") and c["scores"].get(key) is not None
        ]
        return round(sum(vals) / len(vals), 4) if vals else 0.0

    latencies = [c["latency_ms"] for c in cases if not c.get("skipped")]
    return {
        "faithfulness":    mean("faithfulness"),
        "answer_relevance": mean("answer_relevance"),
        "citation_recall": mean("citation_recall"),
        "keyword_recall":  mean("keyword_recall"),
        "hhem_score":      mean("hhem_score"),
        "mean_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0,
        "p95_latency_ms":  round(percentile(latencies, 95), 1),
    }

def check_thresholds(summary: dict, thresholds: dict) -> list[str]:
    failures = []
    for metric, threshold in thresholds.items():
        value = summary.get(metric, 0)
        if metric == "p95_latency_ms":
            if value > threshold:
                failures.append(f"{metric}: {value:.1f}ms > {threshold:.0f}ms threshold")
        else:
            if value < threshold:
                failures.append(f"{metric}: {value:.4f} < {threshold:.4f} threshold")
    return failures

def format_markdown_report(summary: dict, cases: list[dict], failures: list[str]) -> str:
    status = "✅ PASSED" if not failures else "❌ FAILED"
    lines = [
        f"## Eval Results - {status}",
        "",
        "### Summary",
        "",
        "| Metric | Score | Threshold | Status |",
        "|--------|-------|-----------|--------|",
    ]

    def metric_row(name, display_name, fmt="{:.4f}", higher_better=True):
        val = summary.get(name, 0)
        threshold = THRESHOLDS.get(name)
        if threshold is None:
            return
        ok = val >= threshold if higher_better else val <= threshold
        icon = "✅" if ok else "❌"
        lines.append(f"| {display_name} | {fmt.format(val)} | {fmt.format(threshold)} | {icon} |")

    metric_row("faithfulness",    "Faithfulness")
    metric_row("answer_relevance", "Answer Relevance")
    metric_row("citation_recall", "Citation Recall")
    metric_row("keyword_recall",  "Keyword Recall")
    metric_row("p95_latency_ms",  "p95 Latency (ms)", fmt="{:.0f}", higher_better=False)

    hhem_mean = summary.get("hhem_score")
    if hhem_mean:
        lines.append(f"| HHEM (hallucination ↓) | {hhem_mean:.4f} | - | ℹ️ |")

    lines += [
        "",
        "### Per-Case Results",
        "",
        "| Case | Faithful | Relevant | Cite✓ | KW✓ | HHEM↓ | Latency |",
        "|------|----------|----------|-------|-----|-------|---------|",
    ]
    for c in cases:
        s = c["scores"]
        if c.get("skipped"):
            lines.append(f"| {c['id']} | SKIP | SKIP | SKIP | SKIP | SKIP | - |")
        elif c.get("error"):
            lines.append(f"| {c['id']} | ERR | ERR | ERR | ERR | ERR | - |")
        else:
            faithfulness = f"{s['faithfulness']:.2f}" if s.get("faithfulness") is not None else "-"
            relevance = f"{s['answer_relevance']:.2f}" if s.get("answer_relevance") is not None else "-"
            hhem_str = f"{s['hhem_score']:.3f}" if s.get("hhem_score") is not None else "-"
            lines.append(
                f"| {c['id']} "
                f"| {faithfulness} "
                f"| {relevance} "
                f"| {'✅' if s['citation_recall'] == 1.0 else '❌'} "
                f"| {s['keyword_recall']:.2f} "
                f"| {hhem_str} "
                f"| {c['latency_ms']:.0f}ms |"
            )

    if failures:
        lines += ["", "### Failures", ""]
        for f in failures:
            lines.append(f"- {f}")

    return "\n".join(lines)


def is_memory_case(case: dict) -> bool:
    return case.get("type") == "memory" or case.get("category") == "memory"


def _memory_telemetry(case: dict, result: dict, response_body: dict, latency_ms: float, scores: dict) -> None:
    _log_telemetry_s3({
        "request_id":             str(uuid.uuid4()),
        "case_id":                case["id"],
        "is_baseline":            EVAL_IS_BASELINE,
        "source":                 "eval-memory",
        "retrieval_score_mean":   0.0,
        "retrieval_score_std":    0.0,
        "retrieval_score_entropy": 0.0,
        "chunk_count":            len(result.get("citations", [])),
        "reranker_score_mean":    0.0,
        "search_latency_ms":      latency_ms,
        "answer_length":          len(result.get("answer", "")),
        "citation_count":         len(result.get("citations", [])),
        "hhem_score":             0.0,
        "latency_ms":             latency_ms,
        "faithfulness":           None,
        "answer_relevance":       None,
        "keyword_recall":         scores["keyword_recall"],
        "citation_recall":        scores["citation_recall"],
        "memory_enabled":         response_body.get("memoryEnabled", False),
        "memory_id_present":      bool(response_body.get("memoryId")),
        "memory_read_count":      1,
        "memory_write_count":     0,
        "memory_summary_count":   0,
        "memory_latency_ms":      latency_ms,
        "timestamp":              datetime.now(timezone.utc).isoformat(),
    })


def run_memory_case(case: dict) -> dict:
    result = {
        "id": case["id"],
        "category": case["category"],
        "question": case["turns"][-1]["message"],
        "answer": "",
        "citations": [],
        "latency_ms": 0,
        "scores": {
            "keyword_recall": None,
            "citation_recall": None,
            "faithfulness": None,
            "answer_relevance": None,
            "hhem_score": None,
        },
        "memory": {},
    }

    if not MEMORY_EVAL_ENABLED:
        result["skipped"] = True
        result["skip_reason"] = "MEMORY_EVAL_ENABLED is false"
        print("SKIPPED (memory eval disabled)")
        return result

    turns = case["turns"]
    if len(turns) < 2:
        raise ValueError(f"memory case {case['id']} requires at least two turns")

    memory_id = case.get("memory_id") or f"eval:{case['id']}:{uuid.uuid4().hex[:12]}"
    first_session_id = f"eval-{uuid.uuid4().hex[:16]}"
    second_session_id = f"eval-{uuid.uuid4().hex[:16]}"
    first_turn = turns[0]
    recall_turn = turns[-1]
    wait_seconds = int(case.get("memory_wait_seconds", MEMORY_EVAL_WAIT_SECONDS))
    max_attempts = int(case.get("memory_max_attempts", MEMORY_EVAL_MAX_ATTEMPTS))
    expected_keywords = recall_turn.get("expected_keywords", case.get("expected_keywords", []))
    expected_sources = recall_turn.get("expected_sources", case.get("expected_sources", []))

    print(f"ending session for summary, then waiting/retrying recall (memoryId={memory_id}) ... ", end="", flush=True)
    first_answer, _, first_latency_ms, first_body = call_agent(
        first_turn["message"],
        session_id=first_session_id,
        memory_id=memory_id,
        end_session=first_turn.get("end_session", True),
    )

    best = None
    for attempt in range(1, max_attempts + 1):
        if attempt > 1 or wait_seconds > 0:
            time.sleep(wait_seconds)
        answer, citations, latency_ms, body = call_agent(
            recall_turn["message"],
            session_id=second_session_id,
            memory_id=memory_id,
        )
        scores = {
            "keyword_recall":  score_keyword_recall(answer, expected_keywords),
            "citation_recall": score_citation_recall(citations, expected_sources),
            "faithfulness": None,
            "answer_relevance": None,
            "hhem_score": None,
            "judge_reasoning": "Memory recall eval is deterministic; no document citations are required.",
        }
        candidate = (answer, citations, latency_ms, body, scores, attempt)
        if best is None or scores["keyword_recall"] > best[4]["keyword_recall"]:
            best = candidate
        if scores["keyword_recall"] >= float(case.get("pass_keyword_recall", "1.0")):
            break
        if attempt < max_attempts:
            print(f"attempt {attempt} kw={scores['keyword_recall']:.2f}; waiting {wait_seconds}s ... ", end="", flush=True)

    answer, citations, recall_latency_ms, recall_body, scores, attempts = best
    result.update({
        "answer": answer,
        "citations": citations,
        "latency_ms": round(first_latency_ms + recall_latency_ms, 1),
        "scores": scores,
        "memory": {
            "memory_id": memory_id,
            "first_session_id": first_session_id,
            "second_session_id": second_session_id,
            "first_response_memory_id": first_body.get("memoryId"),
            "recall_response_memory_id": recall_body.get("memoryId"),
            "attempts": attempts,
            "summary_wait_seconds": wait_seconds,
            "first_answer": first_answer,
        },
    })
    _memory_telemetry(case, result, recall_body, recall_latency_ms, scores)
    print(f"kw={scores['keyword_recall']:.2f} attempts={attempts} {result['latency_ms']:.0f}ms")
    return result

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Run RAG agent evaluations")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Path for JSON results")
    parser.add_argument("--no-judge", action="store_true", help="Skip LLM judge (deterministic metrics only)")
    args = parser.parse_args()

    if not LAMBDA_URL:
        print("ERROR: LAMBDA_URL environment variable is required", file=sys.stderr)
        print("  export LAMBDA_URL=$(cd terraform && terraform output -raw lambda_url)", file=sys.stderr)
        sys.exit(1)

    eval_cases = json.loads(EVAL_CASES_PATH.read_text())
    print(f"Loaded {len(eval_cases)} eval cases")
    print(f"Agent:    {LAMBDA_URL}")
    print(f"Judge:    {'DISABLED' if args.no_judge else JUDGE_MODEL}")
    print(f"Region:   {AWS_REGION}")
    print(f"Baseline: {EVAL_IS_BASELINE}")
    print(f"HHEM:     {'disabled' if os.environ.get('SKIP_HHEM') == 'true' else 'enabled'}")
    print(f"Memory:   {'enabled' if MEMORY_EVAL_ENABLED else 'disabled'}")
    print()

    results = []

    for i, case in enumerate(eval_cases, 1):
        print(f"[{i:2d}/{len(eval_cases)}] {case['id']} ... ", end="", flush=True)

        if is_memory_case(case):
            try:
                results.append(run_memory_case(case))
            except Exception as e:
                print(f"ERROR: {e}")
                results.append({
                    "id": case["id"],
                    "category": case["category"],
                    "question": case.get("turns", [{}])[-1].get("message", ""),
                    "answer": "",
                    "citations": [],
                    "latency_ms": 0,
                    "error": str(e),
                    "scores": {
                        "keyword_recall": 0.0, "citation_recall": 0.0,
                        "faithfulness": 0.0, "answer_relevance": 0.0, "hhem_score": None,
                    },
                })
            continue

        result = {
            "id": case["id"],
            "category": case["category"],
            "question": case["question"],
            "answer": "",
            "citations": [],
            "latency_ms": 0,
            "scores": {},
        }

        try:
            answer, citations, latency_ms, response_body = call_agent(case["question"])
            result["answer"]     = answer
            result["citations"]  = citations
            result["latency_ms"] = round(latency_ms, 1)
            result["memory"] = {
                "enabled": response_body.get("memoryEnabled", False),
                "memory_id_present": bool(response_body.get("memoryId")),
            }

            scores = {
                "keyword_recall":  score_keyword_recall(answer, case["expected_keywords"]),
                "citation_recall": score_citation_recall(citations, case["expected_sources"]),
            }

            if not args.no_judge:
                judge_scores = call_judge(case["question"], answer, citations)
                scores["faithfulness"]     = round(judge_scores["faithfulness"], 4)
                scores["answer_relevance"] = round(judge_scores["answer_relevance"], 4)
                scores["judge_reasoning"]  = judge_scores["reasoning"]
            else:
                scores["faithfulness"]     = None
                scores["answer_relevance"] = None

            # HHEM hallucination scoring (ML model, non-blocking)
            hhem_score = score_hhem(case["question"], answer)
            scores["hhem_score"] = round(hhem_score, 4) if hhem_score is not None else None

            result["scores"] = scores

            # Build feature vector for telemetry
            n = max(len(citations), 1)
            # Use uniform retrieval score for eval path (scores come from Lambda logs)
            retrieval_mean = 0.5
            request_id = str(uuid.uuid4())
            _log_telemetry_s3({
                "request_id":             request_id,
                "case_id":                case["id"],
                "is_baseline":            EVAL_IS_BASELINE,
                "source":                 "eval",
                "retrieval_score_mean":   retrieval_mean,
                "retrieval_score_std":    0.0,
                "retrieval_score_entropy": math.log(n),
                "chunk_count":            n,
                "reranker_score_mean":    0.0,
                "search_latency_ms":      latency_ms,
                "answer_length":          len(answer),
                "citation_count":         len(citations),
                "hhem_score":             hhem_score or 0.0,
                "latency_ms":             latency_ms,
                "faithfulness":           scores.get("faithfulness"),
                "answer_relevance":       scores.get("answer_relevance"),
                "keyword_recall":         scores["keyword_recall"],
                "citation_recall":        scores["citation_recall"],
                "memory_enabled":         response_body.get("memoryEnabled", False),
                "memory_id_present":      bool(response_body.get("memoryId")),
                "memory_read_count":      1 if response_body.get("memoryId") else 0,
                "memory_write_count":     0,
                "memory_summary_count":   0,
                "memory_latency_ms":      latency_ms if response_body.get("memoryId") else None,
                "timestamp":              datetime.now(timezone.utc).isoformat(),
            })

            kw   = f"kw={scores['keyword_recall']:.2f}"
            cite = f"cite={'✅' if scores['citation_recall'] == 1.0 else '❌'}"
            hhem_str = f"hhem={hhem_score:.3f}" if hhem_score is not None else ""
            if not args.no_judge:
                faith = f"faith={scores['faithfulness']:.2f}"
                rel   = f"rel={scores['answer_relevance']:.2f}"
                print(f"{kw} {cite} {faith} {rel} {hhem_str} {latency_ms:.0f}ms")
            else:
                print(f"{kw} {cite} {hhem_str} {latency_ms:.0f}ms")

        except Exception as e:
            result["error"] = str(e)
            result["scores"] = {
                "keyword_recall": 0.0, "citation_recall": 0.0,
                "faithfulness": 0.0, "answer_relevance": 0.0, "hhem_score": None,
            }
            print(f"ERROR: {e}")

        results.append(result)

    # Aggregate
    summary  = build_summary(results)
    failures = check_thresholds(summary, THRESHOLDS) if not args.no_judge else []
    passed   = len(failures) == 0

    output = {
        "run_timestamp": datetime.now(timezone.utc).isoformat(),
        "lambda_url":    LAMBDA_URL,
        "judge_model":   JUDGE_MODEL if not args.no_judge else None,
        "memory_eval_enabled": MEMORY_EVAL_ENABLED,
        "thresholds":    THRESHOLDS,
        "passed":        passed,
        "summary":       summary,
        "cases":         results,
    }
    Path(args.output).write_text(json.dumps(output, indent=2))
    print(f"\nResults saved → {args.output}")

    report = format_markdown_report(summary, results, failures)
    print("\n" + report)

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a") as f:
            f.write(report + "\n")

    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import boto3
import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LAMBDA_URL = os.environ.get("LAMBDA_URL", "").rstrip("/")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
JUDGE_MODEL = os.environ.get("JUDGE_MODEL_ID", "amazon.nova-pro-v1:0")

THRESHOLDS = {
    "faithfulness":    float(os.environ.get("THRESHOLD_FAITHFULNESS",    "0.70")),
    "answer_relevance": float(os.environ.get("THRESHOLD_RELEVANCE",       "0.75")),
    "citation_recall": float(os.environ.get("THRESHOLD_CITATION_RECALL", "0.60")),
    "keyword_recall":  float(os.environ.get("THRESHOLD_KEYWORD_RECALL",  "0.65")),
    "p95_latency_ms":  float(os.environ.get("THRESHOLD_P95_LATENCY_MS",  "8000")),
}

EVAL_CASES_PATH = Path(__file__).parent.parent / "tests" / "eval_cases.json"
DEFAULT_OUTPUT  = Path(__file__).parent.parent / "eval_results.json"

# ---------------------------------------------------------------------------
# Lambda caller
# ---------------------------------------------------------------------------

def call_agent(question: str) -> tuple[str, list[str], float]:
    """
    POST to the Lambda URL.  Returns (answer, citations, latency_ms).
    Citations are filenames extracted from the markdown links in the answer.
    """
    start = time.monotonic()
    resp = requests.post(
        LAMBDA_URL,
        json={"message": question},
        timeout=30,
    )
    latency_ms = (time.monotonic() - start) * 1000

    resp.raise_for_status()
    body = resp.json()
    answer = body.get("answer", "")

    # Extract filenames from Markdown citation links: [filename.txt](https://...)
    citations = re.findall(r"\[([^\]]+\.txt)\]", answer)

    return answer, citations, latency_ms

# ---------------------------------------------------------------------------
# Deterministic scorers
# ---------------------------------------------------------------------------

def score_keyword_recall(answer: str, expected_keywords: list[str]) -> float:
    """Fraction of expected keywords (case-insensitive) present in the answer."""
    if not expected_keywords:
        return 1.0
    answer_lower = answer.lower()
    hits = sum(1 for kw in expected_keywords if kw.lower() in answer_lower)
    return hits / len(expected_keywords)

def score_citation_recall(citations: list[str], expected_sources: list[str]) -> float:
    """1.0 if at least one expected source filename appears in citations, else 0.0."""
    if not expected_sources:
        return 1.0
    citations_lower = [c.lower() for c in citations]
    for src in expected_sources:
        if src.lower() in citations_lower:
            return 1.0
    return 0.0

# ---------------------------------------------------------------------------
# LLM-as-judge scorer (Nova Pro)
# ---------------------------------------------------------------------------

JUDGE_PROMPT = """\
You are an expert evaluator for a RAG (Retrieval-Augmented Generation) system \
used in a manufacturing environment.

Question: {question}

Answer given: {answer}

Source documents cited: {citations}

Score the answer on BOTH of the following metrics using an integer from 1 to 5:

faithfulness - Are ALL factual claims in the answer directly supported by the \
cited source documents? No invented or extrapolated information.
  1 = significant fabrications present
  2 = several unsupported claims
  3 = mostly grounded, a few questionable details
  4 = nearly all claims traceable to sources
  5 = every claim is directly traceable to the cited sources

answer_relevance - Does the answer fully and directly address the question asked?
  1 = off-topic or does not address the question
  2 = tangentially related but misses the main point
  3 = partially addresses the question, missing key aspects
  4 = mostly complete, minor gaps
  5 = fully and directly addresses the question

Return ONLY a valid JSON object with exactly these keys - no markdown, no explanation outside the JSON:
{{"faithfulness": <integer 1-5>, "answer_relevance": <integer 1-5>, "reasoning": "<one sentence>"}}"""


def call_judge(question: str, answer: str, citations: list[str]) -> dict:
    """
    Call Nova Pro to score faithfulness and answer_relevance.
    Returns {"faithfulness": float 0-1, "answer_relevance": float 0-1, "reasoning": str}.
    """
    bedrock = boto3.client("bedrock-runtime", region_name=AWS_REGION)

    citation_str = ", ".join(citations) if citations else "none"
    prompt = JUDGE_PROMPT.format(
        question=question,
        answer=answer,
        citations=citation_str,
    )

    body = json.dumps({
        "messages": [{"role": "user", "content": [{"text": prompt}]}],
        "inferenceConfig": {"maxTokens": 512, "temperature": 0},
    })

    for attempt in range(4):
        try:
            resp = bedrock.invoke_model(modelId=JUDGE_MODEL, body=body)
            raw = json.loads(resp["body"].read())
            text = raw["output"]["message"]["content"][0]["text"].strip()
            break
        except bedrock.exceptions.ThrottlingException:
            wait = 10 * (2 ** attempt)
            print(f" [throttled, retrying in {wait}s]", end="", flush=True)
            time.sleep(wait)
            if attempt == 3:
                raise

    # Strip accidental markdown fences
    text = re.sub(r"^```json\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    scores = json.loads(text)
    return {
        "faithfulness":    (scores["faithfulness"]    - 1) / 4,  # 1-5 → 0-1
        "answer_relevance": (scores["answer_relevance"] - 1) / 4,
        "reasoning": scores.get("reasoning", ""),
    }

# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------

def percentile(values: list[float], p: int) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    idx = max(0, int(len(sorted_vals) * p / 100) - 1)
    return sorted_vals[idx]

def build_summary(cases: list[dict]) -> dict:
    def mean(key):
        vals = [c["scores"][key] for c in cases if key in c["scores"]]
        return round(sum(vals) / len(vals), 4) if vals else 0.0

    latencies = [c["latency_ms"] for c in cases]
    return {
        "faithfulness":    mean("faithfulness"),
        "answer_relevance": mean("answer_relevance"),
        "citation_recall": mean("citation_recall"),
        "keyword_recall":  mean("keyword_recall"),
        "mean_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0,
        "p95_latency_ms":  round(percentile(latencies, 95), 1),
    }

def check_thresholds(summary: dict, thresholds: dict) -> list[str]:
    failures = []
    for metric, threshold in thresholds.items():
        value = summary.get(metric, 0)
        if metric == "p95_latency_ms":
            if value > threshold:
                failures.append(f"{metric}: {value:.1f}ms > {threshold:.0f}ms threshold")
        else:
            if value < threshold:
                failures.append(f"{metric}: {value:.4f} < {threshold:.4f} threshold")
    return failures

def format_markdown_report(summary: dict, cases: list[dict], failures: list[str]) -> str:
    status = "✅ PASSED" if not failures else "❌ FAILED"
    lines = [
        f"## Eval Results - {status}",
        "",
        "### Summary",
        "",
        "| Metric | Score | Threshold | Status |",
        "|--------|-------|-----------|--------|",
    ]

    def metric_row(name, display_name, fmt="{:.4f}", higher_better=True):
        val = summary.get(name, 0)
        threshold = THRESHOLDS.get(name)
        if threshold is None:
            return
        if higher_better:
            ok = val >= threshold
        else:
            ok = val <= threshold
        icon = "✅" if ok else "❌"
        lines.append(f"| {display_name} | {fmt.format(val)} | {fmt.format(threshold)} | {icon} |")

    metric_row("faithfulness",    "Faithfulness")
    metric_row("answer_relevance", "Answer Relevance")
    metric_row("citation_recall", "Citation Recall")
    metric_row("keyword_recall",  "Keyword Recall")
    metric_row("p95_latency_ms",  "p95 Latency (ms)", fmt="{:.0f}", higher_better=False)

    lines += [
        "",
        "### Per-Case Results",
        "",
        "| Case | Faithful | Relevant | Cite✓ | KW✓ | Latency |",
        "|------|----------|----------|-------|-----|---------|",
    ]
    for c in cases:
        s = c["scores"]
        error = c.get("error", "")
        if error:
            lines.append(f"| {c['id']} | ERR | ERR | ERR | ERR | - |")
        else:
            lines.append(
                f"| {c['id']} "
                f"| {s['faithfulness']:.2f} "
                f"| {s['answer_relevance']:.2f} "
                f"| {'✅' if s['citation_recall'] == 1.0 else '❌'} "
                f"| {s['keyword_recall']:.2f} "
                f"| {c['latency_ms']:.0f}ms |"
            )

    if failures:
        lines += ["", "### Failures", ""]
        for f in failures:
            lines.append(f"- {f}")

    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Run RAG agent evaluations")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Path for JSON results")
    parser.add_argument("--no-judge", action="store_true", help="Skip LLM judge (deterministic metrics only)")
    args = parser.parse_args()

    if not LAMBDA_URL:
        print("ERROR: LAMBDA_URL environment variable is required", file=sys.stderr)
        print("  Set it to the Lambda function URL from terraform output:", file=sys.stderr)
        print("  export LAMBDA_URL=$(cd terraform && terraform output -raw lambda_url)", file=sys.stderr)
        sys.exit(1)

    eval_cases = json.loads(EVAL_CASES_PATH.read_text())
    print(f"Loaded {len(eval_cases)} eval cases")
    print(f"Agent:  {LAMBDA_URL}")
    print(f"Judge:  {'DISABLED' if args.no_judge else JUDGE_MODEL}")
    print(f"Region: {AWS_REGION}")
    print()

    results = []

    for i, case in enumerate(eval_cases, 1):
        print(f"[{i:2d}/{len(eval_cases)}] {case['id']} ... ", end="", flush=True)

        result = {
            "id": case["id"],
            "category": case["category"],
            "question": case["question"],
            "answer": "",
            "citations": [],
            "latency_ms": 0,
            "scores": {},
        }

        try:
            answer, citations, latency_ms = call_agent(case["question"])
            result["answer"]     = answer
            result["citations"]  = citations
            result["latency_ms"] = round(latency_ms, 1)

            scores = {
                "keyword_recall":  score_keyword_recall(answer, case["expected_keywords"]),
                "citation_recall": score_citation_recall(citations, case["expected_sources"]),
            }

            if not args.no_judge:
                judge_scores = call_judge(case["question"], answer, citations)
                scores["faithfulness"]     = round(judge_scores["faithfulness"], 4)
                scores["answer_relevance"] = round(judge_scores["answer_relevance"], 4)
                scores["judge_reasoning"]  = judge_scores["reasoning"]
            else:
                scores["faithfulness"]     = None
                scores["answer_relevance"] = None

            result["scores"] = scores

            kw   = f"kw={scores['keyword_recall']:.2f}"
            cite = f"cite={'✅' if scores['citation_recall'] == 1.0 else '❌'}"
            if not args.no_judge:
                faith = f"faith={scores['faithfulness']:.2f}"
                rel   = f"rel={scores['answer_relevance']:.2f}"
                print(f"{kw} {cite} {faith} {rel} {latency_ms:.0f}ms")
            else:
                print(f"{kw} {cite} {latency_ms:.0f}ms")

        except Exception as e:
            result["error"] = str(e)
            result["scores"] = {
                "keyword_recall": 0.0, "citation_recall": 0.0,
                "faithfulness": 0.0, "answer_relevance": 0.0,
            }
            print(f"ERROR: {e}")

        results.append(result)

    # Aggregate
    summary = build_summary(results)
    failures = check_thresholds(summary, THRESHOLDS) if not args.no_judge else []
    passed   = len(failures) == 0

    # Save JSON
    output = {
        "run_timestamp": datetime.now(timezone.utc).isoformat(),
        "lambda_url":    LAMBDA_URL,
        "judge_model":   JUDGE_MODEL if not args.no_judge else None,
        "thresholds":    THRESHOLDS,
        "passed":        passed,
        "summary":       summary,
        "cases":         results,
    }
    Path(args.output).write_text(json.dumps(output, indent=2))
    print(f"\nResults saved → {args.output}")

    # Markdown report (also written to GITHUB_STEP_SUMMARY if in CI)
    report = format_markdown_report(summary, results, failures)
    print("\n" + report)

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a") as f:
            f.write(report + "\n")

    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
