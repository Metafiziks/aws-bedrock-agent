#!/usr/bin/env bash
set -euo pipefail

# Protected mode (KB_ACCESS_MODE=protected): document-level access control, see guides/protected-mode.md
if [ "${KB_ACCESS_MODE:-sample}" = "protected" ]; then
  exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/deploy_protected.sh"
fi

# The sample deployment builds and deploys its Lambda as part of provision.sh.
echo "Sample mode deploys with provision.sh (re-run it to update the Lambda). For protected mode set KB_ACCESS_MODE=protected." >&2
exit 1
