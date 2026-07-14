#!/usr/bin/env bash
set -euo pipefail

ACCOUNT_ID="${ACCOUNT_ID:?Set ACCOUNT_ID env var (your AWS account ID)}"
ENV_NAME="${ENV_NAME:-search-agent}"
REGION="${REGION:-us-east-1}"
GITHUB_REPO="${GITHUB_REPO:?Set GITHUB_REPO (owner/repo)}"
ALERT_EMAIL="${ALERT_EMAIL:?Set ALERT_EMAIL for budget alerts}"

echo ""
echo "=== Provision: Infrastructure + Knowledge Base Setup ==="
echo ""

# --- Prerequisite check ---
echo "► Checking prerequisites..."
command -v aws   >/dev/null || { echo "ERROR: aws CLI not found. Install from https://aws.amazon.com/cli/"; exit 1; }
command -v terraform >/dev/null || { echo "ERROR: terraform not found. brew install hashicorp/tap/terraform"; exit 1; }
aws sts get-caller-identity --query Account --output text >/dev/null || { echo "ERROR: Not authenticated. Run: aws configure sso OR aws configure"; exit 1; }

echo ""
echo "┌─────────────────────────────────────────────────────────────────┐"
echo "│  PREREQUISITE: Enable Bedrock model access in AWS Console        │"
echo "│                                                                  │"
echo "│  1. Open: https://console.aws.amazon.com/bedrock/home#/modelaccess"
echo "│  2. Request access for:                                          │"
echo "│     • Anthropic Claude 3.5 Sonnet                                │"
echo "│     • Amazon Titan Embeddings V2                                 │"
echo "│  3. Wait for 'Access granted' status (usually instant)           │"
echo "└─────────────────────────────────────────────────────────────────┘"
echo ""
read -r -p "  Press Enter once model access is granted to continue..."
echo ""

# --- Terraform ---
echo "► Provisioning infrastructure with Terraform..."
terraform -chdir=terraform init -upgrade -input=false -reconfigure
terraform -chdir=terraform apply -auto-approve \
  -var="account_id=${ACCOUNT_ID}" \
  -var="region=${REGION}" \
  -var="env_name=${ENV_NAME}" \
  -var="github_repo=${GITHUB_REPO}" \
  -var="alert_email=${ALERT_EMAIL}"
echo "  ✓ Infrastructure ready"
echo ""

BUCKET=$(terraform -chdir=terraform output -raw docs_bucket)
KB_ID=$(terraform -chdir=terraform output -raw knowledge_base_id)
DS_ID=$(terraform -chdir=terraform output -raw data_source_id)

# --- Upload documents to S3 ---
echo "► Uploading documents to S3..."
aws s3 sync docs/ "s3://${BUCKET}/docs/" --region "${REGION}"
echo "  ✓ Documents uploaded"
echo ""

# --- Trigger Knowledge Base sync ---
echo "► Syncing Knowledge Base (indexing documents)..."
JOB=$(aws bedrock-agent start-ingestion-job \
  --knowledge-base-id "${KB_ID}" \
  --data-source-id "${DS_ID}" \
  --region "${REGION}" \
  --query "ingestionJob.ingestionJobId" --output text)

echo "  Ingestion job: ${JOB}"
echo "  Waiting for completion..."
while true; do
  STATUS=$(aws bedrock-agent get-ingestion-job \
    --knowledge-base-id "${KB_ID}" \
    --data-source-id "${DS_ID}" \
    --ingestion-job-id "${JOB}" \
    --region "${REGION}" \
    --query "ingestionJob.status" --output text)
  echo "  Status: ${STATUS}"
  [[ "$STATUS" == "COMPLETE" ]] && break
  [[ "$STATUS" == "FAILED" ]]   && { echo "ERROR: Ingestion failed"; exit 1; }
  sleep 10
done
echo "  ✓ Knowledge Base indexed"
echo ""

LAMBDA_URL=$(terraform -chdir=terraform output -raw lambda_url)
AGENT_ID=$(terraform -chdir=terraform output -raw agent_id)
AGENT_ALIAS=$(terraform -chdir=terraform output -raw agent_alias_id)

echo "=== Provision Complete ==="
echo "  Bucket       : s3://${BUCKET}"
echo "  Knowledge Base: ${KB_ID}"
echo "  Agent ID     : ${AGENT_ID}"
echo "  Lambda URL   : ${LAMBDA_URL}"
echo ""
echo "► Test your agent:"
echo "  curl -X POST ${LAMBDA_URL} \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -d '{\"message\": \"What documents are available?\"}'"
echo ""
