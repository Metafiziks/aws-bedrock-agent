# aws-bedrock-agent

A Terraform template that deploys an **Amazon Bedrock Agent** backed by your own document corpus using **Bedrock Knowledge Bases** (RAG). Ask questions in natural language — the agent synthesizes answers and cites source files with direct S3 links.

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
     ▼
Bedrock Agent (Amazon Nova Lite)
     │  retrieves from Knowledge Base
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
