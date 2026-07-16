#!/usr/bin/env python3
"""
Auto-generate eval cases from the docs/ directory.
--------------------------------------------------
Reads every .txt file under docs/, uses Amazon Nova Pro to generate 2 Q&A
test cases per document, and writes tests/eval_cases.json in the standard
eval format.

Run this whenever docs change to keep the eval suite in sync:

  python3 scripts/generate_eval_cases.py            # uses AWS_REGION env
  python3 scripts/generate_eval_cases.py --dry-run  # prints cases, doesn't write

Output schema (matches tests/eval_cases.json):
  id               — kebab-case identifier
  category         — docs subdirectory name (maintenance, safety, quality, ...)
  question         — a question a worker on the floor might actually ask
  expected_keywords — 4-6 key phrases that must appear in a correct answer
  expected_sources  — [filename.txt] used to score citation recall
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import boto3

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

AWS_REGION  = os.environ.get("AWS_REGION", "us-east-1")
MODEL       = os.environ.get("GENERATOR_MODEL_ID", "amazon.nova-pro-v1:0")
DOCS_DIR    = Path(__file__).parent.parent / "docs"
OUTPUT_PATH = Path(__file__).parent.parent / "tests" / "eval_cases.json"

GENERATOR_PROMPT = """\
You are writing test cases for a RAG evaluation suite.

Below is a manufacturing procedure document named "{filename}":

---
{content}
---

Generate exactly 2 test cases based on this document. Each test case must be a
question that a manufacturing floor worker or supervisor might realistically ask.

Rules:
- Questions must be specific enough that the document above is the clear source
  (e.g. "hydraulic press" not just "press", "lockout tagout" not just "safety")
- Questions must be answerable entirely from this document — no general knowledge
- expected_keywords must be 4-6 short phrases (2-5 words each) that would appear
  in a correct, complete answer. Use exact phrasing from the document where possible.
- expected_sources must be exactly ["{filename}"]

Return ONLY a valid JSON array — no markdown, no explanation:
[
  {{
    "id": "<kebab-case-id>",
    "category": "{category}",
    "question": "<question text>",
    "expected_keywords": ["<phrase1>", "<phrase2>", "<phrase3>", "<phrase4>"],
    "expected_sources": ["{filename}"]
  }},
  {{
    "id": "<kebab-case-id-2>",
    "category": "{category}",
    "question": "<question text>",
    "expected_keywords": ["<phrase1>", "<phrase2>", "<phrase3>", "<phrase4>"],
    "expected_sources": ["{filename}"]
  }}
]"""

# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

def generate_cases_for_doc(bedrock, doc_path: Path) -> list[dict]:
    """Generate 2 eval cases for a single document using Nova Pro."""
    content  = doc_path.read_text()
    filename = doc_path.name
    category = doc_path.parent.name

    prompt = GENERATOR_PROMPT.format(
        filename=filename,
        category=category,
        content=content,
    )

    body = json.dumps({
        "messages": [{"role": "user", "content": [{"text": prompt}]}],
        "inferenceConfig": {"maxTokens": 1024, "temperature": 0.3},
    })

    for attempt in range(4):
        try:
            resp = bedrock.invoke_model(modelId=MODEL, body=body)
            raw  = json.loads(resp["body"].read())
            text = raw["output"]["message"]["content"][0]["text"].strip()

            # Strip accidental markdown fences
            text = re.sub(r"^```json\s*", "", text)
            text = re.sub(r"\s*```$", "", text)

            cases = json.loads(text)
            for c in cases:
                assert "id" in c and "question" in c and "expected_keywords" in c
            return cases
        except bedrock.exceptions.ThrottlingException:
            wait = 15 * (2 ** attempt)
            print(f"    Throttled — waiting {wait}s...", flush=True)
            time.sleep(wait)
        except Exception as exc:
            print(f"    ERROR for {filename}: {exc}", file=sys.stderr)
            return []

    print(f"    Giving up on {filename} after 4 attempts", file=sys.stderr)
    return []


def deduplicate_ids(cases: list[dict]) -> list[dict]:
    seen = {}
    result = []
    for c in cases:
        base = c["id"]
        if base in seen:
            seen[base] += 1
            c["id"] = f"{base}-{seen[base]}"
        else:
            seen[base] = 0
        result.append(c)
    return result

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Generate eval cases from docs/")
    parser.add_argument("--dry-run", action="store_true", help="Print cases, don't write")
    args = parser.parse_args()

    if not DOCS_DIR.exists():
        print(f"ERROR: docs/ directory not found at {DOCS_DIR}", file=sys.stderr)
        sys.exit(1)

    doc_files = sorted(DOCS_DIR.rglob("*.txt"))
    if not doc_files:
        print("No .txt files found under docs/", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(doc_files)} document(s) — generating 2 cases each...")
    print(f"Model: {MODEL}  Region: {AWS_REGION}")
    print()

    bedrock = boto3.client("bedrock-runtime", region_name=AWS_REGION)

    all_cases = []
    for doc_path in doc_files:
        rel = doc_path.relative_to(DOCS_DIR.parent)
        print(f"  {rel} ...", end=" ", flush=True)
        cases = generate_cases_for_doc(bedrock, doc_path)
        print(f"{len(cases)} cases generated")
        all_cases.extend(cases)
        time.sleep(1)

    all_cases = deduplicate_ids(all_cases)

    print(f"\nTotal: {len(all_cases)} eval cases")

    if args.dry_run:
        print(json.dumps(all_cases, indent=2))
        return

    OUTPUT_PATH.parent.mkdir(exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(all_cases, indent=2))
    print(f"Written → {OUTPUT_PATH}")
    print()
    print("Run evals with:")
    print("  bash scripts/eval.sh")


if __name__ == "__main__":
    main()
