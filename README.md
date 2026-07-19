# aws-bedrock-agent

A Terraform template that deploys an **Amazon Bedrock Agent** backed by your own document corpus using **Bedrock Knowledge Bases** (RAG). Ask questions in natural language — the agent synthesizes answers and cites source files with direct S3 links. Includes optional **Bedrock Agents Classic session-summary memory** for scoped user/session continuity, a built-in **automated evaluation suite** that scores every deployment on faithfulness, answer relevance, citation accuracy, latency, and memory recall, plus an **ML observability layer** with Bedrock Rerank, IsolationForest anomaly detection, and HHEM hallucination scoring that auto-trains from each deployment.

```bash
$ curl -X POST https://<lambda-url> \
    -H 'Content-Type: application/json' \
    -d '{"message": "What should I do if a hydraulic press is leaking oil?"}'

{
  "answer": "If a hydraulic press is leaking oil, immediately stop the machine and notify
  maintenance personnel. Do not operate the press while there are active hydraulic leaks.
  Use absorbent pads and follow established spill response procedures.
  - [hydraulic_press_troubleshooting.txt](https://...s3.amazonaws.com/docs/maintenance/hydraulic_press_troubleshooting.txt)",
  "sessionId": "abc-123"
}
```

## What it deploys

| Resource | Purpose |
|---|---|
| S3 Bucket | Stores document corpus (public read for citation links) |
| Bedrock Knowledge Base | RAG — chunks, embeds, and indexes documents; retrieves relevant passages |
| OpenSearch Serverless | Vector store (k-NN index, 1024-dim) backing the Knowledge Base |
| Bedrock Agent (Amazon Nova Lite) | Synthesizes answers from retrieved passages |
| Bedrock Agents Classic Memory (optional) | Session-summary memory scoped by `memoryId` for user preferences and conversation continuity |
| Lambda Function + URL | Public HTTP endpoint for invoking the agent |
| AWS Budgets | Monthly cost alert at $50 |
| GitHub OIDC | Secretless GitHub Actions auth |

## Architecture

```
User question (HTTP POST to Lambda URL)
     │
     ▼
Lambda (handler.py)
     │  bedrock:InvokeAgent
     │  optional memoryId/endSession
     ▼
Bedrock Agent (Amazon Nova Lite)
     │  ├─ optional SESSION_SUMMARY memory for user/session continuity
     │  └─ retrieves source-of-truth procedure facts from Knowledge Base
     ▼
Bedrock Knowledge Base
     │  Titan Embed Text v2 (1024-dim) → OpenSearch Serverless
     │  indexed from
     ▼
S3 Bucket  (docs/ prefix · public read · HTTPS citation links)
```

## Prerequisites

- [AWS CLI v2](https://aws.amazon.com/cli/) — `brew install awscli`
- [Terraform](https://developer.hashicorp.com/terraform/install) ≥ 1.5 — `brew install hashicorp/tap/terraform`
- Python 3 — for the OpenSearch index bootstrap script
- AWS account authenticated: `aws configure sso` or `aws configure`

## Quick start

```bash
git clone https://github.com/Metafiziks/aws-bedrock-agent
cd aws-bedrock-agent

# 1. Authenticate
aws configure sso   # or: aws configure

# 2. Fill in your values (gitignored — never committed)
cat > terraform/terraform.tfvars <<EOF
account_id  = "$(aws sts get-caller-identity --query Account --output text)"
github_repo = "your-org/your-repo"
alert_email = "you@example.com"
region      = "us-east-1"
env_name    = "search-agent"

# Optional Bedrock Agents Classic memory
enable_agent_memory   = false
memory_retention_days = 30
memory_default_id_mode = "explicit"
EOF

# 3. Drop your .txt documents into docs/
#    (sample manufacturing docs already included)

# 4. Provision everything (~10-15 min)
bash scripts/provision.sh
```

The script will output the Lambda URL to test with:

```bash
curl -X POST https://<lambda-url> \
  -H 'Content-Type: application/json' \
  -d '{"message": "What documents are available?"}'
```

## Teardown

```bash
bash scripts/teardown.sh
```

Runs `terraform destroy` — removes all AWS resources. The OpenSearch collection takes ~2 min to delete.

## Adding your documents

Drop `.txt` files into `docs/` before running `provision.sh`. Subdirectory structure is preserved as the S3 key prefix:

```
docs/
  safety/       lockout_tagout.txt
  maintenance/  hydraulic_press_guide.txt
  quality/      inspection_standard.txt
```

Re-run `bash scripts/provision.sh` to sync new docs and re-index.

## Memory layer

Memory is optional and disabled by default. When enabled, Terraform configures Bedrock Agents Classic `SESSION_SUMMARY` memory on the agent and Lambda can pass a scoped `memoryId` plus `endSession` to `InvokeAgent`.

RAG and memory stay separate by design:

| Layer | Use it for | Do not use it for |
|---|---|---|
| Knowledge Base / RAG | Manufacturing procedures, safety requirements, quality standards, troubleshooting facts, cited answers | User preferences or cross-session conversation continuity |
| Agent memory | User/session preferences, prior conversation context, summaries under the same `memoryId` | Source-of-truth procedure answers or uncited manufacturing facts |

### Configuration

Terraform variables:

| Variable | Default | Description |
|---|---:|---|
| `enable_agent_memory` | `false` | Adds Bedrock Agents Classic session-summary memory when true. |
| `memory_retention_days` | `30` | Retains summaries for 1-365 days. |
| `memory_default_id_mode` | `explicit` | Controls Lambda behavior when a request omits `memoryId`: `explicit` uses only a supplied `memoryId`; `user` derives `user:<userId>`; `session` derives `session:<sessionId>`. |

The provision script accepts the same values as environment variables:

```bash
ENABLE_AGENT_MEMORY=true \
MEMORY_RETENTION_DAYS=30 \
MEMORY_DEFAULT_ID_MODE=explicit \
bash scripts/provision.sh
```

### Request contract

```bash
curl -X POST https://<lambda-url> \
  -H 'Content-Type: application/json' \
  -d '{
    "message": "For future reference, my preferred handoff format is a short safety-first bullet list.",
    "sessionId": "session-001",
    "memoryId": "user-123",
    "endSession": true
  }'
```

Use the same `memoryId` on a later session to recall user-specific context:

```bash
curl -X POST https://<lambda-url> \
  -H 'Content-Type: application/json' \
  -d '{
    "message": "What handoff format should you use for me?",
    "sessionId": "session-002",
    "memoryId": "user-123"
  }'
```

Bedrock summarizes memory asynchronously after a session is ended or idle timeout elapses. The memory eval waits and retries recall because summaries may not be immediately available.

### Memory eval

`tests/memory_eval_cases.json` includes `memory-shift-handoff-preference`. It is loaded in addition to generated document eval cases, so `scripts/generate_eval_cases.py` can overwrite `tests/eval_cases.json` without dropping memory coverage. Memory cases are skipped by default so existing RAG-only evals remain stable. Enable them for memory deployments:

```bash
MEMORY_EVAL_ENABLED=true bash scripts/eval.sh
```

### Bedrock Agents Classic and AgentCore

This template uses Bedrock Agents Classic because it already deploys `aws_bedrockagent_agent`, Knowledge Bases, and `bedrock-agent-runtime:InvokeAgent`. AWS now describes Bedrock Agents as **Bedrock Agents Classic** and points new forward-looking agent development toward **Amazon Bedrock AgentCore**. Existing Classic customers can continue using Classic memory, but new templates should evaluate AgentCore memory/session primitives when moving off Classic.

## GitHub Actions CI/CD

`provision.sh` sets all required repo variables automatically if `gh` is authenticated. To set them manually:

```bash
gh variable set AWS_ROLE_ARN       --body "$(terraform -chdir=terraform output -raw github_actions_role_arn)"
gh variable set AWS_REGION         --body "us-east-1"
gh variable set DOCS_BUCKET        --body "$(terraform -chdir=terraform output -raw docs_bucket)"
gh variable set KNOWLEDGE_BASE_ID  --body "$(terraform -chdir=terraform output -raw knowledge_base_id)"
gh variable set DATA_SOURCE_ID     --body "$(terraform -chdir=terraform output -raw data_source_id)"
gh variable set LAMBDA_URL         --body "$(terraform -chdir=terraform output -raw lambda_url)"
```

Then activate the workflows by copying them into `.github/workflows/`:

```bash
cp workflows/*.yml .github/workflows/
git add .github/workflows/ && git commit -m "Activate CI workflows" && git push
```

| Workflow | Trigger | What it does |
|---|---|---|
| `ingest-docs.yml` | Push to `docs/` | Syncs docs to S3 and re-indexes the Knowledge Base |
| `deploy-agent.yml` | Push to `src/` | Redeploys the Lambda handler |
| `run-evals.yml` | Push to `src/` or `tests/`, weekly | Runs the 12-case eval suite via Nova Pro judge; fails CI if metrics drop below threshold |

No AWS credentials stored as secrets — all auth via OIDC.

## Running Evaluations Locally

After provisioning:

```bash
bash scripts/eval.sh
```

`eval.sh` resolves the Lambda URL from terraform output automatically. To skip the LLM judge (faster, deterministic metrics only):

```bash
bash scripts/eval.sh --no-judge
```

Results are written to `eval_results.json`. Metrics scored:

| Metric | Method | Pass threshold |
|---|---|---|
| Keyword Recall | Deterministic | ≥ 0.65 |
| Citation Recall | Deterministic | ≥ 0.60 |
| p95 Latency | Deterministic | ≤ 8000ms |
| Faithfulness | Nova Pro judge | ≥ 0.70 |
| Answer Relevance | Nova Pro judge | ≥ 0.75 |

**Keeping evals in sync with your docs:**

When you change files in `docs/`, regenerate the eval cases before re-running:

```bash
AWS_REGION=us-east-1 python3 scripts/generate_eval_cases.py
bash scripts/eval.sh
```

`generate_eval_cases.py` reads every `.txt` file under `docs/`, calls Nova Pro to generate 2 Q&A test cases per document, and writes `tests/eval_cases.json`. Running `bash scripts/provision.sh` does this automatically after each doc sync.

## Comparison across cloud providers

| | This template (AWS) | [GCP](https://github.com/Metafiziks/gcp-search-agent) | [Azure](https://github.com/Metafiziks/azd-foundry-search-agent) |
|---|---|---|---|
| Provision | `bash scripts/provision.sh` | `bash scripts/provision.sh` | `azd provision` |
| LLM | Amazon Nova Lite (Bedrock) | Gemini 2.5 Flash (Vertex AI) | GPT-5 (Azure AI Foundry) |
| Agent SDK | Bedrock Agents (managed) | Google ADK + Cloud Run | AI Foundry hosted agent |
| RAG | Bedrock Knowledge Bases | Vertex AI Search Enterprise | Azure AI Search |
| Vector store | OpenSearch Serverless | Vertex AI Search (built-in) | Azure AI Search (built-in) |
| Auth | GitHub OIDC | Workload Identity Federation | Azure OIDC |
| Teardown | `bash scripts/teardown.sh` | `bash scripts/teardown.sh` | `azd down` |

## Troubleshooting

These are hard-won lessons from live provisioning — save yourself the debugging time.

| Error | Cause | Fix |
|---|---|---|
| `no such index [bedrock-kb-index]` | Bedrock does NOT auto-create the OpenSearch index | `provision.sh` pre-creates it via the OpenSearch HTTP API before the KB is created |
| `Query vector has invalid dimension: 1024. Dimension should be: 1536` | Wrong vector dimension in index mapping | Titan Embed **v2** outputs 1024 dims. 1536 is v1. Index must match. |
| `403 Forbidden` on OpenSearch index creation | Manual SigV4 signing via botocore/http.client has header conflicts | Use `opensearch-py`'s `AWSV4SignerAuth` — it handles signing correctly |
| `externally-managed-environment` (pip) | Python 3.13+ on Homebrew blocks system pip | Script creates `/tmp/aoss-venv` venv automatically |
| `resourceNotFoundException` — model not found | Anthropic Claude models require submitting a use case form on new accounts | Template uses **Amazon Nova Lite** — no form required |
| `on-demand throughput isn't supported` | Newer Claude models require inference profiles (`us.` prefix), not on-demand IDs | Use `us.anthropic.claude-*` or switch to Amazon Nova models |
| KB `403 Forbidden` during creation | KB IAM role missing `aoss:APIAccessAll` on collection ARN | Added to `aws_iam_role_policy.kb_s3` |
| `AccessDeniedException` on Lambda | IAM policy scoped to specific alias ARN; alias changed | Policy now uses wildcard: `agent-alias/{agent-id}/*` |
| OpenSearch collection takes time | Access policies take 60s+ to propagate after collection creation | `time_sleep` resource waits 60s before creating the Knowledge Base |

---

## ML Observability

Beyond pass/fail eval scores, this template ships a production-ready ML observability layer that runs alongside the Bedrock Agent and learns what "healthy" looks like for your specific document corpus.

### Architecture

```
Request
  │
  ▼
Lambda Handler
  │
  ├── Bedrock Agent  ──→  Knowledge Base (RAG)
  │                           │
  │                           ↓ retrieved chunks
  │
  ├── Bedrock Rerank API  ──→  Re-scores chunks with amazon.rerank-v1:0
  │                            (cross-encoder model, better than cosine alone)
  │
  ├── IsolationForest  ──→  Anomaly score from 6-d feature vector
  │   (loaded from S3)       [retrieval_mean, retrieval_std, retrieval_entropy,
  │                           chunk_count, reranker_mean, search_latency_ms]
  │
  └── CloudWatch Sink  ──→  TELEMETRY {json} → stdout
                             → CloudWatch Logs
                             → Kinesis Firehose
                             → S3 (telemetry/year=.../month=.../day=.../...)
```

### ML Models

| Model | Type | Purpose | Location |
|-------|------|---------|----------|
| `amazon.rerank-v1:0` | Cross-encoder reranker | Re-rank retrieved chunks by semantic relevance | Bedrock API |
| `IsolationForest` | Unsupervised anomaly detector | Flag unusual retrieval patterns before users notice | S3 `models/iforest.pkl` |
| `vectara/hallucination_evaluation_model` (HHEM-2.1) | Hallucination classifier | Score answer faithfulness in evals (0=faithful, 1=hallucination) | HuggingFace Hub |

### Auto-training

The `provision.sh` script:
1. Runs the eval suite 5× with `EVAL_IS_BASELINE=true` → 60 baseline rows written to S3 via Firehose
2. Trains IsolationForest on those rows and uploads `models/iforest.pkl` to S3
3. Lambda loads the model on next cold start (module-level singleton)

The GitHub Actions workflow (`.github/workflows/retrain-observability.yml`) retrains weekly on the last 30 days of production traffic.

### Anomaly Detection Schema

Telemetry rows written to S3 (via CloudWatch → Firehose):

| Field | Type | Description |
|-------|------|-------------|
| `request_id` | string | Unique request UUID |
| `retrieval_score_mean` | float | Mean relevance score of retrieved chunks |
| `retrieval_score_std` | float | Std dev of retrieval scores (spread) |
| `retrieval_score_entropy` | float | Entropy of score distribution |
| `chunk_count` | int | Number of chunks retrieved |
| `reranker_score_mean` | float | Mean Bedrock Rerank score |
| `search_latency_ms` | float | End-to-end retrieval latency |
| `memory_enabled` | bool | Whether Lambda memory handling was enabled for the request |
| `memory_id_present` | bool | Whether a `memoryId` was sent to Bedrock |
| `memory_read_count` | int | Request-level count for memory-backed recall attempts |
| `memory_write_count` | int | Request-level count for ended sessions that request memory summarization |
| `memory_summary_count` | int | Request-level count for summary creation requests (`endSession=true`) |
| `memory_latency_ms` | float | End-to-end `InvokeAgent` latency for memory-backed requests; Bedrock does not expose a separate memory latency |
| `anomaly_score` | float | IForest score (negative = more anomalous) |
| `is_anomaly` | bool | True if score below -0.1 threshold |

### CloudWatch Insights Queries

```kql
# Anomaly rate over last 7 days
fields @timestamp, anomaly_score, is_anomaly
| filter @message like /TELEMETRY/
| parse @message "TELEMETRY *" as json_data
| filter is_anomaly = true
| stats count() as anomaly_count by bin(1h)
| sort @timestamp desc
```

### Weekly Retrain

The `.github/workflows/retrain-observability.yml` workflow triggers every Sunday 02:00 UTC:
- Reads last 30 days of JSONL telemetry from S3
- Retrains IsolationForest on production distribution
- Uploads new `models/iforest.pkl` → Lambda picks up on next cold start

To trigger manually:
```bash
gh workflow run retrain-observability.yml
```
