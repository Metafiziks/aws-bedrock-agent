#!/usr/bin/env bash
# Protected mode provisioning (KB_ACCESS_MODE=protected). Called from provision.sh.
# Creates a private bucket, an ACL-enabled Bedrock Managed Knowledge Base, a Cognito user pool
# and least-privilege IAM. It never uploads the public sample corpus, creates a public bucket,
# a Lambda URL, an OpenSearch collection or any static key. The backend is added by deploy.sh.
set -euo pipefail

ENV_NAME="${ENV_NAME:-search-agent}"
REGION="${REGION:-us-east-1}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TF_DIR="${SCRIPT_DIR}/../terraform/protected"
STATE_FILE="${PROTECTED_STATE_FILE:-${SCRIPT_DIR}/../.protected-state}"
NAME="${ENV_NAME}-protected"
KB_NAME="${NAME}-kb"
DS_NAME="${NAME}-s3"

echo ""
echo "=== Provision (protected mode): private corpus + ACL-aware Managed Knowledge Base ==="
echo ""

command -v aws       >/dev/null || { echo "ERROR: aws CLI not found." >&2; exit 1; }
command -v terraform >/dev/null || { echo "ERROR: terraform not found." >&2; exit 1; }
command -v python3   >/dev/null || { echo "ERROR: python3 not found." >&2; exit 1; }
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
[[ "${ACCOUNT_ID}" =~ ^[0-9]{12}$ ]] || { echo "ERROR: not authenticated to AWS." >&2; exit 1; }
echo "  Region: ${REGION}   Environment: ${NAME}"
echo ""

tf_vars=(-var="region=${REGION}" -var="env_name=${ENV_NAME}" -var="account_id=${ACCOUNT_ID}"
         -var="bedrock_model_id=${BEDROCK_MODEL_ID:-amazon.nova-lite-v1:0}"
         -var="id_token_minutes=${ID_TOKEN_MINUTES:-60}")

# Value of KEY from the state file ("" if absent).
state_get() { { sed -n "s/^$1=//p" "${STATE_FILE}" 2>/dev/null || true; } | head -n1; }

echo "► Stage 1: private bucket, service roles and Cognito user pool (Terraform, own state)..."
terraform -chdir="${TF_DIR}" init -upgrade -input=false -reconfigure
terraform -chdir="${TF_DIR}" apply -auto-approve -input=false "${tf_vars[@]}" \
  -var="knowledge_base_id=$(state_get KNOWLEDGE_BASE_ID)" -var="data_source_id=$(state_get DATA_SOURCE_ID)"
echo "  ✓ Stage 1 ready"
echo ""

BUCKET="$(terraform -chdir="${TF_DIR}" output -raw docs_bucket)"
KB_ROLE_ARN="$(terraform -chdir="${TF_DIR}" output -raw kb_role_arn)"

echo "► Creating the managed knowledge base (service-managed embeddings, no vector store to run)..."
KB_ID="$(state_get KNOWLEDGE_BASE_ID)"
if [ -z "${KB_ID}" ]; then
  KB_ID="$(aws bedrock-agent list-knowledge-bases --region "${REGION}" \
    --query "knowledgeBaseSummaries[?name=='${KB_NAME}'].knowledgeBaseId | [0]" --output text 2>/dev/null || true)"
  [ "${KB_ID}" = "None" ] && KB_ID=""
fi
if [ -z "${KB_ID}" ]; then
  # A new IAM role can take a while to become usable by Bedrock: retry instead of assuming.
  ERRFILE="$(mktemp)"
  for attempt in $(seq 1 "${PROTECTED_RETRIES:-12}"); do
    if KB_ID="$(aws bedrock-agent create-knowledge-base --region "${REGION}" --name "${KB_NAME}" \
        --description "Protected, ACL-aware knowledge base (${NAME})" --role-arn "${KB_ROLE_ARN}" \
        --knowledge-base-configuration '{"type":"MANAGED","managedKnowledgeBaseConfiguration":{"embeddingModelType":"MANAGED"}}' \
        --tags "created-by=aws-bedrock-agent-protected,env=${ENV_NAME}" \
        --query knowledgeBase.knowledgeBaseId --output text 2>"${ERRFILE}")"; then
      break
    fi
    KB_ID=""
    echo "  create-knowledge-base not accepted yet (attempt ${attempt}); waiting for IAM to propagate..."
    sleep 15
  done
  [ -n "${KB_ID}" ] || { echo "ERROR: could not create the knowledge base:" >&2; cat "${ERRFILE}" >&2; exit 1; }
  rm -f "${ERRFILE}"
fi
[[ "${KB_ID}" =~ ^[0-9A-Za-z]{10}$ ]] || { echo "ERROR: unexpected knowledge base id." >&2; exit 1; }
printf 'KNOWLEDGE_BASE_ID=%s\nDATA_SOURCE_ID=%s\n' "${KB_ID}" "$(state_get DATA_SOURCE_ID)" > "${STATE_FILE}.new" && mv "${STATE_FILE}.new" "${STATE_FILE}"
for _ in $(seq 1 "${PROTECTED_TRIES:-60}"); do
  STATUS="$(aws bedrock-agent get-knowledge-base --region "${REGION}" --knowledge-base-id "${KB_ID}" \
    --query knowledgeBase.status --output text)"
  [ "${STATUS}" = "ACTIVE" ] && break
  case "${STATUS}" in FAILED|DELETING|DELETE_UNSUCCESSFUL) echo "ERROR: knowledge base is ${STATUS}" >&2; exit 1;; esac
  sleep 10
done
[ "${STATUS:-}" = "ACTIVE" ] || { echo "ERROR: knowledge base did not become ACTIVE in time." >&2; exit 1; }
echo "  ✓ Knowledge base ${KB_ID} is ACTIVE"

echo "► Creating the ACL-enabled S3 data source (per-document ACL files; documents without one are not ingested)..."
DS_ID="$(state_get DATA_SOURCE_ID)"
if [ -z "${DS_ID}" ]; then
  DS_ID="$(aws bedrock-agent list-data-sources --region "${REGION}" --knowledge-base-id "${KB_ID}" \
    --query "dataSourceSummaries[?name=='${DS_NAME}'].dataSourceId | [0]" --output text 2>/dev/null || true)"
  [ "${DS_ID}" = "None" ] && DS_ID=""
fi
if [ -z "${DS_ID}" ]; then
  DS_CONFIG="$(python3 -c 'import json,sys; print(json.dumps({"type":"MANAGED_KNOWLEDGE_BASE_CONNECTOR","managedKnowledgeBaseConnectorConfiguration":{"connectorParameters":{"type":"S3","version":"1","aclEnabled":True,"connectionConfiguration":{"bucketName":sys.argv[1],"bucketOwnerAccountId":sys.argv[2]}}}}))' "${BUCKET}" "${ACCOUNT_ID}")"
  DS_ID="$(aws bedrock-agent create-data-source --region "${REGION}" --knowledge-base-id "${KB_ID}" --name "${DS_NAME}" \
    --data-deletion-policy DELETE --data-source-configuration "${DS_CONFIG}" \
    --query dataSource.dataSourceId --output text)"
fi
[[ "${DS_ID}" =~ ^[0-9A-Za-z]{10}$ ]] || { echo "ERROR: unexpected data source id." >&2; exit 1; }
printf 'KNOWLEDGE_BASE_ID=%s\nDATA_SOURCE_ID=%s\n' "${KB_ID}" "${DS_ID}" > "${STATE_FILE}"
for _ in $(seq 1 "${PROTECTED_TRIES:-60}"); do
  STATUS="$(aws bedrock-agent get-data-source --region "${REGION}" --knowledge-base-id "${KB_ID}" \
    --data-source-id "${DS_ID}" --query dataSource.status --output text)"
  [ "${STATUS}" = "AVAILABLE" ] && break
  [ "${STATUS}" = "FAILED" ] && { echo "ERROR: data source FAILED" >&2; exit 1; }
  sleep 10
done
[ "${STATUS:-}" = "AVAILABLE" ] || { echo "ERROR: data source did not become AVAILABLE in time." >&2; exit 1; }
echo "  ✓ Data source ${DS_ID} is AVAILABLE (aclEnabled)"
echo ""

if [ -n "${PROTECTED_CORPUS_DIR:-}" ]; then
  echo "► Ingesting the approved corpus from ${PROTECTED_CORPUS_DIR} ..."
  VENV="${PROTECTED_VENV:-/tmp/aws-protected-venv}"
  [ -x "${VENV}/bin/python3" ] || python3 -m venv "${VENV}"
  "${VENV}/bin/pip" install -q boto3
  "${VENV}/bin/python3" "${SCRIPT_DIR}/ingest_protected.py" \
    --bucket "${BUCKET}" --knowledge-base-id "${KB_ID}" --data-source-id "${DS_ID}" \
    --directory "${PROTECTED_CORPUS_DIR}" --region "${REGION}"
  echo ""
fi

echo "=== Provision Complete (protected) ==="
echo "  Private bucket : s3://${BUCKET}"
echo "  Knowledge base : ${KB_ID} (managed, ACL-enabled data source ${DS_ID})"
echo "  User pool      : $(terraform -chdir="${TF_DIR}" output -raw user_pool_id)"
echo ""
echo "  Next: KB_ACCESS_MODE=protected bash scripts/deploy.sh"
echo "  Ingest corpus : python3 scripts/ingest_protected.py --bucket ${BUCKET} --knowledge-base-id ${KB_ID} \\"
echo "                    --data-source-id ${DS_ID} --directory /path/outside/repo --region ${REGION}"
echo ""
