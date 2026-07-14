#!/usr/bin/env bash
set -euo pipefail

ACCOUNT_ID="${ACCOUNT_ID:?Set ACCOUNT_ID env var}"
ENV_NAME="${ENV_NAME:-search-agent}"
REGION="${REGION:-us-east-1}"

echo ""
echo "=== Teardown: Removing all resources ==="
echo ""
read -r -p "  ⚠️  This will delete all resources for ENV_NAME='${ENV_NAME}'. Press Enter to continue or Ctrl+C to cancel..."
echo ""

echo "► Destroying infrastructure with Terraform..."
terraform -chdir=terraform destroy -auto-approve \
  -var="account_id=${ACCOUNT_ID}" \
  -var="region=${REGION}" \
  -var="env_name=${ENV_NAME}" \
  -var="github_repo=${GITHUB_REPO:-placeholder/placeholder}" \
  -var="alert_email=${ALERT_EMAIL:-noop@example.com}"
echo "  ✓ Infrastructure destroyed"
echo ""

echo "=== Teardown Complete ==="
echo ""
