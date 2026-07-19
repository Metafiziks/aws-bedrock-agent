#!/usr/bin/env bash
# Run evaluations against the deployed agent.
# Usage: bash scripts/eval.sh [--no-judge] [--output path/to/results.json]
#
# Reads LAMBDA_URL from terraform output if not already set.
# Uses the venv at /tmp/aoss-venv (created by provision.sh).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="/tmp/aoss-venv"
REGION="${AWS_REGION:-us-east-1}"
MEMORY_EVAL_ENABLED="${MEMORY_EVAL_ENABLED:-false}"

# Resolve LAMBDA_URL from terraform output if not provided
if [[ -z "${LAMBDA_URL:-}" ]]; then
  echo "► Resolving Lambda URL from terraform output..."
  LAMBDA_URL=$(terraform -chdir="${REPO_ROOT}/terraform" output -raw lambda_url 2>/dev/null || true)
  if [[ -z "${LAMBDA_URL}" ]]; then
    echo "ERROR: Could not resolve LAMBDA_URL. Is the infrastructure provisioned?"
    echo "  Run: bash scripts/provision.sh"
    exit 1
  fi
  export LAMBDA_URL
fi

# Ensure venv and dependencies exist
if [[ ! -d "${VENV_DIR}" ]]; then
  echo "► Creating Python venv..."
  python3 -m venv "${VENV_DIR}"
fi
"${VENV_DIR}/bin/pip" install requests boto3 -q

echo "► Running evaluations against: ${LAMBDA_URL}"
echo ""

LAMBDA_URL="${LAMBDA_URL}" \
AWS_REGION="${REGION}" \
MEMORY_EVAL_ENABLED="${MEMORY_EVAL_ENABLED}" \
"${VENV_DIR}/bin/python3" "${SCRIPT_DIR}/run_evals.py" \
  --output "${REPO_ROOT}/eval_results.json" \
  "$@"
