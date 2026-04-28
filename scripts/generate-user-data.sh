#!/usr/bin/env bash
# Generates scripts/user-data.sh by injecting real values from .env.deploy
# and .env.production into the user-data.tpl.sh template.
#
# Usage: make generate-user-data
# Output: scripts/user-data.sh  ← paste this into Byteplus ECS User Data
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$SCRIPT_DIR/.."

DEPLOY_ENV="$ROOT/.env.deploy"
PROD_ENV="$ROOT/.env.production"
TEMPLATE="$SCRIPT_DIR/user-data.tpl.sh"
OUTPUT="$SCRIPT_DIR/user-data.sh"

for f in "$DEPLOY_ENV" "$PROD_ENV" "$TEMPLATE"; do
  [[ -f "$f" ]] || { echo "ERROR: missing $f"; exit 1; }
done

# shellcheck source=/dev/null
source "$DEPLOY_ENV"
# shellcheck source=/dev/null
source "$PROD_ENV"

: "${CR_REGISTRY:?}" "${CR_NAMESPACE:?}" "${CR_REPO:?}"
: "${CR_USERNAME:?}" "${CR_PASSWORD:?}"
: "${APP_DIR:?}"
: "${DATABASE_URL:?}" "${LLM_API_KEY:?}" "${LLM_MODEL:?}"

IMAGE="$CR_REGISTRY/$CR_NAMESPACE/$CR_REPO:latest"

sed \
  -e "s|__CR_REGISTRY__|$CR_REGISTRY|g" \
  -e "s|__CR_USERNAME__|$CR_USERNAME|g" \
  -e "s|__CR_PASSWORD__|$CR_PASSWORD|g" \
  -e "s|__IMAGE__|$IMAGE|g" \
  -e "s|__APP_DIR__|$APP_DIR|g" \
  -e "s|__DATABASE_URL__|$DATABASE_URL|g" \
  -e "s|__LLM_MODEL__|$LLM_MODEL|g" \
  -e "s|__LLM_API_KEY__|$LLM_API_KEY|g" \
  -e "s|__LLM_PROVIDER__|${LLM_PROVIDER:-}|g" \
  -e "s|__LLM_API_BASE__|${LLM_API_BASE:-}|g" \
  -e "s|__LLM_TIMEOUT_SECONDS__|${LLM_TIMEOUT_SECONDS:-30}|g" \
  -e "s|__OUTPUT_DIR__|${OUTPUT_DIR:-./output}|g" \
  "$TEMPLATE" > "$OUTPUT"

chmod +x "$OUTPUT"

echo "==> Generated: scripts/user-data.sh"
echo ""
echo "    Next steps:"
echo "    1. Open Byteplus Console → ECS → your instance → Stop the instance"
echo "    2. Go to Instance Settings → User Data"
echo "    3. Paste the contents of scripts/user-data.sh"
echo "    4. Start the instance — it will self-configure on boot"
echo ""
echo "    IMPORTANT: user-data.sh contains secrets. Do not commit it to git."
