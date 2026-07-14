#!/usr/bin/env bash
set -euo pipefail

ENV_NAME="${ENV_NAME:-search-agent}"
REGION="${REGION:-us-east-1}"

echo ""
echo "=== Teardown: Removing all resources ==="
echo ""
read -r -p "  ⚠️  This will delete all resources for ENV_NAME='${ENV_NAME}'. Press Enter to continue or Ctrl+C to cancel..."
echo ""

echo "► Destroying infrastructure with Terraform..."
terraform -chdir=terraform destroy -auto-approve \
  -var="region=${REGION}" \
  -var="env_name=${ENV_NAME}"
echo "  ✓ Infrastructure destroyed"
echo ""

echo "=== Teardown Complete ==="
echo ""
