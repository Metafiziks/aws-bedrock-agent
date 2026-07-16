#!/usr/bin/env bash
set -euo pipefail

ENV_NAME="${ENV_NAME:-search-agent}"
REGION="${REGION:-us-east-1}"

echo ""
echo "=== Provision: Infrastructure + Knowledge Base Setup ==="
echo ""

# --- Prerequisite check ---
echo "► Checking prerequisites..."
command -v aws       >/dev/null || { echo "ERROR: aws CLI not found. Install from https://aws.amazon.com/cli/"; exit 1; }
command -v terraform >/dev/null || { echo "ERROR: terraform not found. brew install hashicorp/tap/terraform"; exit 1; }
command -v python3   >/dev/null || { echo "ERROR: python3 not found."; exit 1; }
aws sts get-caller-identity --query Account --output text >/dev/null \
  || { echo "ERROR: Not authenticated. Run: aws configure sso OR aws configure"; exit 1; }

echo ""
echo "┌─────────────────────────────────────────────────────────────────┐"
echo ""

TF_VARS="-var=region=${REGION} -var=env_name=${ENV_NAME}"

# --- Stage 1: Create OpenSearch collection + IAM (no KB yet) ---
echo "► Stage 1: Provisioning OpenSearch collection and IAM..."
terraform -chdir=terraform init -upgrade -input=false -reconfigure
terraform -chdir=terraform apply -auto-approve -input=false ${TF_VARS} \
  -target=aws_opensearchserverless_security_policy.encryption \
  -target=aws_opensearchserverless_security_policy.network \
  -target=aws_opensearchserverless_access_policy.kb \
  -target=aws_opensearchserverless_collection.kb \
  -target=aws_iam_role.kb \
  -target=aws_iam_role_policy.kb_s3 \
  -target=time_sleep.wait_for_collection
echo "  ✓ OpenSearch collection ready"
echo ""

# --- Create the k-NN vector index (Bedrock requires it to pre-exist) ---
echo "► Creating OpenSearch k-NN index..."
COLLECTION_ENDPOINT=$(terraform -chdir=terraform output -raw collection_endpoint)
# Use a venv to avoid Homebrew's externally-managed-environment restriction
python3 -m venv /tmp/aoss-venv --clear 2>/dev/null || true
/tmp/aoss-venv/bin/pip install boto3 requests opensearch-py requests-aws4auth -q
COLLECTION_ENDPOINT="${COLLECTION_ENDPOINT}" AWS_REGION="${REGION}" \
  /tmp/aoss-venv/bin/python3 scripts/create_os_index.py
echo ""

# --- Stage 2: Full apply (KB, Agent, Lambda, etc.) ---
echo "► Stage 2: Provisioning Knowledge Base, Agent, and Lambda..."
terraform -chdir=terraform apply -auto-approve -input=false ${TF_VARS}
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
echo "  Bucket        : s3://${BUCKET}"
echo "  Knowledge Base : ${KB_ID}"
echo "  Agent ID      : ${AGENT_ID}"
echo "  Lambda URL    : ${LAMBDA_URL}"
echo ""
echo "► Test your agent:"
echo "  curl -X POST ${LAMBDA_URL} \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -d '{\"message\": \"What documents are available?\"}'"
echo ""

echo "► Running automated evaluations..."
echo ""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="/tmp/aoss-venv"

# Ensure eval dependencies are present in the venv
"${VENV_DIR}/bin/pip" install requests boto3 -q

LAMBDA_URL="${LAMBDA_URL}" \
AWS_REGION="${REGION}" \
"${VENV_DIR}/bin/python3" "${SCRIPT_DIR}/run_evals.py" \
  --output "${SCRIPT_DIR}/../eval_results.json"
echo ""

# Set GitHub Actions repo variables so CI workflows work without manual config
if command -v gh &>/dev/null && gh auth status &>/dev/null 2>&1; then
  echo "► Setting GitHub Actions repo variables..."
  ROLE_ARN=$(terraform -chdir=terraform output -raw github_actions_role_arn)
  KB_ID_OUT=$(terraform -chdir=terraform output -raw knowledge_base_id)
  DS_ID_OUT=$(terraform -chdir=terraform output -raw data_source_id)

  gh variable set AWS_ROLE_ARN        --body "${ROLE_ARN}"
  gh variable set AWS_REGION          --body "${REGION}"
  gh variable set DOCS_BUCKET         --body "${BUCKET}"
  gh variable set KNOWLEDGE_BASE_ID   --body "${KB_ID_OUT}"
  gh variable set DATA_SOURCE_ID      --body "${DS_ID_OUT}"
  gh variable set LAMBDA_URL          --body "${LAMBDA_URL}"
  echo "  ✓ Repo variables set"
  echo ""
  echo "► To activate GitHub Actions workflows, copy them to .github/workflows/:"
  echo "  cp workflows/*.yml .github/workflows/"
  echo "  git add .github/workflows/ && git commit -m 'Activate CI workflows' && git push"
else
  echo "► Skipping GitHub Actions variable setup (gh CLI not authenticated)"
  echo "  To wire up CI manually, run:"
  echo "    gh variable set AWS_ROLE_ARN       --body \"\$(terraform -chdir=terraform output -raw github_actions_role_arn)\""
  echo "    gh variable set AWS_REGION         --body \"${REGION}\""
  echo "    gh variable set DOCS_BUCKET        --body \"${BUCKET}\""
  echo "    gh variable set KNOWLEDGE_BASE_ID  --body \"\$(terraform -chdir=terraform output -raw knowledge_base_id)\""
  echo "    gh variable set DATA_SOURCE_ID     --body \"\$(terraform -chdir=terraform output -raw data_source_id)\""
  echo "    gh variable set LAMBDA_URL         --body \"${LAMBDA_URL}\""
fi
echo ""

