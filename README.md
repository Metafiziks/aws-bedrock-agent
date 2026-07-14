# aws-bedrock-agent

A Terraform template that deploys an **Amazon Bedrock Agent** backed by your own document corpus using **Bedrock Knowledge Bases** (RAG). Ask questions in natural language — the agent synthesizes answers and cites source files with direct S3 links.

```bash
$ curl -X POST https://<lambda-url> \
    -H 'Content-Type: application/json' \
    -d '{"message": "What should I do if a hydraulic press is leaking oil?"}'

{
  "answer": "If a hydraulic press is leaking oil, immediately stop the machine and notify
  maintenance personnel. Do not operate the press while there are active hydraulic leaks.
  Use absorbent pads and follow established spill response procedures.\n\n
  Sources:\n- [hydraulic_press_troubleshooting](https://s3.amazonaws.com/<bucket>/docs/maintenance/hydraulic_press_troubleshooting.txt)",
  "sessionId": "abc-123"
}
```

## What it deploys

| Resource | Purpose |
|---|---|
| S3 Bucket | Stores document corpus (public read for citation links) |
| Bedrock Knowledge Base | RAG — chunks, embeds, and indexes documents; retrieves relevant passages |
| OpenSearch Serverless | Vector store backing the Knowledge Base |
| Bedrock Agent (Claude 3.5 Sonnet) | Synthesizes answers from retrieved passages |
| Lambda Function + URL | HTTP invoke endpoint for the agent |
| AWS Budgets | Monthly cost alert at $50 |
| GitHub OIDC | Secretless GitHub Actions auth |

## Architecture

```
User question (HTTP POST to Lambda URL)
     │
     ▼
Lambda (handler.py)
     │  calls bedrock:InvokeAgent
     ▼
Bedrock Agent (Claude 3.5 Sonnet)
     │  retrieves from Knowledge Base
     ▼
Bedrock Knowledge Base (Titan Embeddings V2 + OpenSearch Serverless)
     │  indexed from
     ▼
S3 Bucket (docs/ prefix, public read, HTTPS citation links)
```

## Prerequisites

- [AWS CLI](https://aws.amazon.com/cli/) (`brew install awscli`)
- [Terraform](https://developer.hashicorp.com/terraform/install) ≥ 1.5 — `brew install hashicorp/tap/terraform`
- AWS account with billing enabled

> **One-time console step:** Before running `provision.sh`, visit  
> `https://console.aws.amazon.com/bedrock/home#/modelaccess`  
> and request access for **Anthropic Claude 3.5 Sonnet** and **Amazon Titan Embeddings V2**.  
> The script will pause and prompt you for this.

## Quick start

```bash
git clone https://github.com/Metafiziks/aws-bedrock-agent
cd aws-bedrock-agent

# Auth
aws configure sso   # or: aws configure

# Set required env vars
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export GITHUB_REPO=your-org/your-repo
export ALERT_EMAIL=you@example.com
export ENV_NAME=search-agent
export REGION=us-east-1

# Provision everything + index documents (~10-15 min)
bash scripts/provision.sh

# Tear everything down when done
bash scripts/teardown.sh
```

## Adding your documents

Drop `.txt` files into `docs/` before running `provision.sh`:

```
docs/
  safety/       lockout_tagout.txt
  maintenance/  hydraulic_press_guide.txt
  quality/      inspection_standard.txt
```

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `ACCOUNT_ID` | ✅ | — | AWS account ID |
| `GITHUB_REPO` | ✅ | — | `owner/repo` for OIDC |
| `ALERT_EMAIL` | ✅ | — | Email for budget alerts |
| `ENV_NAME` | — | `search-agent` | Prefix for all resource names |
| `REGION` | — | `us-east-1` | AWS region |

## GitHub Actions CI/CD

After provisioning, set these repo variables:

```bash
gh variable set AWS_ROLE_ARN --body "$(terraform -chdir=terraform output -raw github_actions_role_arn)"
gh variable set AWS_REGION --body "$REGION"
gh variable set DOCS_BUCKET --body "$(terraform -chdir=terraform output -raw docs_bucket)"
gh variable set KNOWLEDGE_BASE_ID --body "$(terraform -chdir=terraform output -raw knowledge_base_id)"
gh variable set DATA_SOURCE_ID --body "$(terraform -chdir=terraform output -raw data_source_id)"
gh variable set ENV_NAME --body "$ENV_NAME"
```

Every push to `main` syncs new documents and reindexes the Knowledge Base.

## Teardown

```bash
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export ENV_NAME=search-agent
export REGION=us-east-1

bash scripts/teardown.sh
```

## Comparison across cloud providers

| | This template (AWS) | [GCP](https://github.com/Metafiziks/gcp-search-agent) | [Azure](https://github.com/Metafiziks/azd-foundry-search-agent) |
|---|---|---|---|
| Provision | `terraform apply` | `terraform apply` | `azd provision` (Bicep) |
| Deploy | built into provision | `adk deploy cloud_run` | `azd deploy` |
| LLM | Claude 3.5 Sonnet (Bedrock) | Gemini 2.5 Flash (Agent Platform) | GPT-5 (AI Foundry) |
| RAG | Bedrock Knowledge Bases | Vertex AI Search Enterprise | Azure AI Search |
| Agent runtime | Bedrock Agents (managed) | Google ADK + Cloud Run | AI Foundry hosted agent |
| Auth | GitHub OIDC | Workload Identity Federation | Azure OIDC |
| Teardown | `bash scripts/teardown.sh` | `bash scripts/teardown.sh` | `azd down` |

## Troubleshooting

| Error | Fix |
|---|---|
| `AccessDeniedException` on Bedrock | Request model access at console.aws.amazon.com/bedrock/home#/modelaccess |
| `ResourceNotFoundException` on ingestion | Knowledge Base may still be creating — wait 2-3 min and retry |
| Lambda timeout | Increase `timeout` in `lambda.tf` (Bedrock can take 30-60s for complex queries) |
| OpenSearch collection not ready | Collection creation takes ~5 min — `terraform apply` will wait automatically |
