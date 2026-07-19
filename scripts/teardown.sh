#!/usr/bin/env bash
set -euo pipefail

ENV_NAME="${ENV_NAME:-search-agent}"
REGION="${REGION:-us-east-1}"
ENABLE_AGENT_MEMORY="${ENABLE_AGENT_MEMORY:-false}"
MEMORY_RETENTION_DAYS="${MEMORY_RETENTION_DAYS:-30}"
MEMORY_DEFAULT_ID_MODE="${MEMORY_DEFAULT_ID_MODE:-explicit}"

echo ""
echo "=== Teardown: Removing all resources ==="
echo ""
read -r -p "  ⚠️  This will delete all resources for ENV_NAME='${ENV_NAME}'. Press Enter to continue or Ctrl+C to cancel..."
echo ""

command -v aws >/dev/null || { echo "ERROR: aws CLI not found. Install from https://aws.amazon.com/cli/"; exit 1; }
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

echo "► Destroying infrastructure with Terraform..."
terraform -chdir=terraform destroy -auto-approve \
  -var="region=${REGION}" \
  -var="env_name=${ENV_NAME}" \
  -var="account_id=${ACCOUNT_ID}" \
  -var="github_repo=${GITHUB_REPO:-Metafiziks/aws-bedrock-agent}" \
  -var="alert_email=${ALERT_EMAIL:-}" \
  -var="enable_agent_memory=${ENABLE_AGENT_MEMORY}" \
  -var="memory_retention_days=${MEMORY_RETENTION_DAYS}" \
  -var="memory_default_id_mode=${MEMORY_DEFAULT_ID_MODE}"
echo "  ✓ Infrastructure destroyed"
echo ""

echo "=== Teardown Complete ==="
echo ""
