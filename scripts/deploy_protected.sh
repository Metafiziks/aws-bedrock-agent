#!/usr/bin/env bash
# Protected mode deploy (KB_ACCESS_MODE=protected). Called from deploy.sh.
# Deploys the stateless Lambda behind an API Gateway JWT authorizer, then locks the knowledge
# base so only that Lambda's role can call Retrieve. No Lambda URL, no sample evals or telemetry.
set -euo pipefail

ENV_NAME="${ENV_NAME:-search-agent}"
REGION="${REGION:-us-east-1}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TF_DIR="${SCRIPT_DIR}/../terraform/protected"
STATE_FILE="${PROTECTED_STATE_FILE:-${SCRIPT_DIR}/../.protected-state}"
state_get() { { sed -n "s/^$1=//p" "${STATE_FILE}" 2>/dev/null || true; } | head -n1; }
KB_ID="$(state_get KNOWLEDGE_BASE_ID)"
DS_ID="$(state_get DATA_SOURCE_ID)"
[ -n "${KB_ID}" ] && [ -n "${DS_ID}" ] || { echo "ERROR: run provision.sh first (no ${STATE_FILE})." >&2; exit 1; }
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

echo ""
echo "=== Deploy (protected mode): stateless Lambda + JWT-authorized API ==="
echo ""

echo "► Building the Lambda package (own boto3, no sample code or models)..."
BUILD="${TF_DIR}/lambda_build"
rm -rf "${BUILD}" && mkdir -p "${BUILD}"
python3 -m pip install --target "${BUILD}" --no-cache-dir -q -r "${SCRIPT_DIR}/../src/protected/requirements.txt"
find "${BUILD}" -type d \( -name tests -o -name test -o -name __pycache__ \) -prune -exec rm -rf {} +
cp "${SCRIPT_DIR}/../src/protected/handler.py" "${SCRIPT_DIR}/../src/protected/protected_search.py" "${BUILD}/"
echo "  ✓ Package ready"

echo "► Stage 2: Lambda, API Gateway and JWT authorizer (Terraform)..."
terraform -chdir="${TF_DIR}" init -upgrade -input=false -reconfigure
terraform -chdir="${TF_DIR}" apply -auto-approve -input=false \
  -var="region=${REGION}" -var="env_name=${ENV_NAME}" -var="account_id=${ACCOUNT_ID}" \
  -var="bedrock_model_id=${BEDROCK_MODEL_ID:-amazon.nova-lite-v1:0}" \
  -var="id_token_minutes=${ID_TOKEN_MINUTES:-60}" \
  -var="knowledge_base_id=${KB_ID}" -var="data_source_id=${DS_ID}"
echo "  ✓ Backend deployed"
echo ""

LAMBDA_ROLE_ARN="$(terraform -chdir="${TF_DIR}" output -raw lambda_role_arn)"
FUNCTION="$(terraform -chdir="${TF_DIR}" output -raw function_name)"
API_URL="$(terraform -chdir="${TF_DIR}" output -raw api_url)"
BUCKET="$(terraform -chdir="${TF_DIR}" output -raw docs_bucket)"
POOL_ID="$(terraform -chdir="${TF_DIR}" output -raw user_pool_id)"
CLIENT_ID="$(terraform -chdir="${TF_DIR}" output -raw user_pool_client_id)"
KB_ARN="arn:aws:bedrock:${REGION}:${ACCOUNT_ID}:knowledge-base/${KB_ID}"

echo "► Locking the knowledge base: only the Lambda role may Retrieve (resource policy)..."
# Bedrock trusts userContext, so no other principal may call Retrieve with an arbitrary identity.
POLICY="$(python3 -c 'import json,sys; print(json.dumps({"Version":"2012-10-17","Statement":[{"Sid":"DenyRetrieveExceptProtectedFunction","Effect":"Deny","Principal":"*","Action":["bedrock:Retrieve","bedrock:GetDocumentContent"],"Resource":sys.argv[1],"Condition":{"ArnNotEquals":{"aws:PrincipalArn":sys.argv[2]}}}]}))' "${KB_ARN}" "${LAMBDA_ROLE_ARN}")"
aws bedrock-agent put-resource-policy --region "${REGION}" --resource-arn "${KB_ARN}" --policy "${POLICY}" >/dev/null
aws bedrock-agent get-resource-policy --region "${REGION}" --resource-arn "${KB_ARN}" --query policy --output text \
  | grep -q "DenyRetrieveExceptProtectedFunction" || { echo "ERROR: the knowledge base resource policy was not applied." >&2; exit 1; }
echo "  ✓ Resource policy applied"

# Refuse to leave the backend reachable without a validated user.
API_ID="${API_URL#https://}"; API_ID="${API_ID%%.*}"
ROUTES="$(aws apigatewayv2 get-routes --region "${REGION}" --api-id "${API_ID}" --query 'Items[].AuthorizationType' --output text)"
if [ -z "${ROUTES}" ] || echo "${ROUTES}" | tr '\t' '\n' | grep -vqx 'JWT'; then
  echo "ERROR: ${API_ID} has a route without the JWT authorizer; refusing to continue." >&2
  exit 1
fi
URLS="$(aws lambda list-function-url-configs --region "${REGION}" --function-name "${FUNCTION}" --query 'FunctionUrlConfigs[].FunctionUrl' --output text)"
if [ -n "${URLS}" ] && [ "${URLS}" != "None" ]; then
  echo "ERROR: ${FUNCTION} has a function URL; remove it before use." >&2
  exit 1
fi

echo "=== Deploy Complete (protected) ==="
echo "  API URL : ${API_URL}/ask   (POST; Authorization: Bearer <Cognito ID token>)"
echo "  Pool/client: ${POOL_ID} / ${CLIENT_ID}"
echo ""

if [ -n "${PROTECTED_ACCEPTANCE_CONFIG:-}" ]; then
  echo "► Running the protected acceptance gate (fails the deploy if it fails)..."
  VENV="${PROTECTED_VENV:-/tmp/aws-protected-venv}"
  [ -x "${VENV}/bin/python3" ] || python3 -m venv "${VENV}"
  "${VENV}/bin/pip" install -q httpx boto3
  # Non-interactive: PROTECTED_ACCEPTANCE_ARGS="--mint-users" creates two synthetic Cognito users, mints
  # tokens locally (never printed) and deletes the users afterwards. Without it, tokens are prompted.
  "${VENV}/bin/python3" "${SCRIPT_DIR}/test_protected_live.py" \
    --api-url "${API_URL}" --bucket "${BUCKET}" --region "${REGION}" \
    --knowledge-base-id "${KB_ID}" --data-source-id "${DS_ID}" --user-pool-id "${POOL_ID}" --client-id "${CLIENT_ID}" \
    --function-name "${FUNCTION}" --config "${PROTECTED_ACCEPTANCE_CONFIG}" \
    ${PROTECTED_ACCEPTANCE_ARGS:-}
else
  echo "  No PROTECTED_ACCEPTANCE_CONFIG set: deployed without the live acceptance gate."
  echo "  Run scripts/test_protected_live.py (see guides/protected-mode.md) before accepting traffic."
fi
echo ""
