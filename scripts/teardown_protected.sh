#!/usr/bin/env bash
# Protected mode teardown (KB_ACCESS_MODE=protected). Called from teardown.sh.
# Deletes the knowledge base (AWS CLI) and everything Terraform created, then verifies nothing is left.
set -euo pipefail

ENV_NAME="${ENV_NAME:-search-agent}"
REGION="${REGION:-us-east-1}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TF_DIR="${SCRIPT_DIR}/../terraform/protected"
STATE_FILE="${PROTECTED_STATE_FILE:-${SCRIPT_DIR}/../.protected-state}"
NAME="${ENV_NAME}-protected"
state_get() { { sed -n "s/^$1=//p" "${STATE_FILE}" 2>/dev/null || true; } | head -n1; }
KB_ID="$(state_get KNOWLEDGE_BASE_ID)"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

echo ""
echo "=== Teardown (protected): removing the protected environment '${NAME}' ==="
if [ "${ASSUME_YES:-0}" != "1" ]; then
  read -r -p "  ⚠️  Deletes the knowledge base, private bucket (and its documents), user pool, Lambda, API and IAM. Enter to continue, Ctrl+C to cancel..."
fi
echo ""

# Fall back to the name when the state file is gone.
if [ -z "${KB_ID}" ]; then
  KB_ID="$(aws bedrock-agent list-knowledge-bases --region "${REGION}" \
    --query "knowledgeBaseSummaries[?name=='${NAME}-kb'].knowledgeBaseId | [0]" --output text 2>/dev/null || true)"
  [ "${KB_ID}" = "None" ] && KB_ID=""
fi

# 0 = exists, 1 = not found; anything else aborts instead of being mistaken for "gone".
kb_exists() {
  local err
  if err="$(aws bedrock-agent get-knowledge-base --region "${REGION}" --knowledge-base-id "$1" 2>&1 >/dev/null)"; then return 0; fi
  echo "${err}" | grep -q "ResourceNotFoundException" && return 1
  echo "ERROR: could not check knowledge base $1: ${err}" >&2; exit 1
}

if [ -n "${KB_ID}" ] && kb_exists "${KB_ID}"; then
  echo "► Deleting the knowledge base ${KB_ID} and its data sources..."
  for DS in $(aws bedrock-agent list-data-sources --region "${REGION}" --knowledge-base-id "${KB_ID}" \
      --query 'dataSourceSummaries[].dataSourceId' --output text 2>/dev/null | tr '\t' ' '); do
    aws bedrock-agent delete-data-source --region "${REGION}" --knowledge-base-id "${KB_ID}" --data-source-id "${DS}" >/dev/null
  done
  for _ in $(seq 1 "${PROTECTED_TRIES:-60}"); do
    [ -z "$(aws bedrock-agent list-data-sources --region "${REGION}" --knowledge-base-id "${KB_ID}" \
      --query 'dataSourceSummaries[].dataSourceId' --output text 2>/dev/null)" ] && break
    sleep 10
  done
  aws bedrock-agent delete-knowledge-base --region "${REGION}" --knowledge-base-id "${KB_ID}" >/dev/null
  for _ in $(seq 1 "${PROTECTED_TRIES:-60}"); do
    kb_exists "${KB_ID}" || break
    sleep 10
  done
  ! kb_exists "${KB_ID}" || { echo "ERROR: knowledge base ${KB_ID} still exists." >&2; exit 1; }
  echo "  ✓ Knowledge base deleted"
else
  echo "  ℹ  No knowledge base found (already deleted or never created; skipping)"
fi

echo "► Destroying protected infrastructure with Terraform..."
terraform -chdir="${TF_DIR}" init -input=false -reconfigure >/dev/null
# The archive_file data source reads this directory even on destroy (observed live): it must exist and not be empty.
mkdir -p "${TF_DIR}/lambda_build" && echo "placeholder for terraform destroy" > "${TF_DIR}/lambda_build/placeholder.txt"
terraform -chdir="${TF_DIR}" destroy -auto-approve -input=false \
  -var="region=${REGION}" -var="env_name=${ENV_NAME}" -var="account_id=${ACCOUNT_ID}" \
  -var="knowledge_base_id=$(state_get KNOWLEDGE_BASE_ID)" -var="data_source_id=$(state_get DATA_SOURCE_ID)"
rm -rf "${TF_DIR}/lambda_build" "${TF_DIR}/lambda.zip"
echo "  ✓ Infrastructure destroyed"
echo ""

echo "► Verifying nothing created by this tool is left..."
LEFT=""
BUCKET="${ACCOUNT_ID}-${NAME}-docs"
aws s3api head-bucket --bucket "${BUCKET}" >/dev/null 2>&1 && LEFT+=" bucket:${BUCKET}"
[ -z "$(aws bedrock-agent list-knowledge-bases --region "${REGION}" --query "knowledgeBaseSummaries[?name=='${NAME}-kb'].knowledgeBaseId" --output text 2>/dev/null)" ] || LEFT+=" knowledge-base:${NAME}-kb"
aws lambda get-function --region "${REGION}" --function-name "${NAME}-ask" >/dev/null 2>&1 && LEFT+=" lambda:${NAME}-ask"
for ROLE in "${NAME}-kb-role" "${NAME}-lambda-role"; do
  aws iam get-role --role-name "${ROLE}" >/dev/null 2>&1 && LEFT+=" role:${ROLE}"
done
[ -z "$(aws cognito-idp list-user-pools --region "${REGION}" --max-results 60 --query "UserPools[?Name=='${NAME}-users'].Id" --output text 2>/dev/null)" ] || LEFT+=" user-pool:${NAME}-users"
[ -z "$(aws apigatewayv2 get-apis --region "${REGION}" --query "Items[?Name=='${NAME}-api'].ApiId" --output text 2>/dev/null)" ] || LEFT+=" api:${NAME}-api"
[ -z "$(aws logs describe-log-groups --region "${REGION}" --log-group-name-prefix "/aws/lambda/${NAME}-ask" --query 'logGroups[].logGroupName' --output text 2>/dev/null)" ] || LEFT+=" log-group:/aws/lambda/${NAME}-ask"
if [ -n "${LEFT}" ]; then
  echo "ERROR: these resources are still present:${LEFT}" >&2
  exit 1
fi
rm -f "${STATE_FILE}"
echo "  ✓ Verified: no protected resources remain"

echo "=== Teardown Complete (protected) ==="
echo "  Check separately created identities and tokens yourself; this script only removes what it created."
echo ""
