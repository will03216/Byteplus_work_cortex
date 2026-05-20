#!/usr/bin/env bash
# Build the Docker image locally and push it to Byteplus Container Registry.
# After pushing, restart the ECS instance from the Byteplus console to pull
# the new image automatically.
#
# Usage: ./deploy.sh
# Requires: .env.deploy (copy from .env.deploy.example)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env.deploy"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: .env.deploy not found."
  echo "       Copy .env.deploy.example → .env.deploy and fill in values."
  exit 1
fi

# shellcheck source=/dev/null
source "$ENV_FILE"

: "${CR_REGISTRY:?Set CR_REGISTRY in .env.deploy}"
: "${CR_NAMESPACE:?Set CR_NAMESPACE in .env.deploy}"
: "${CR_REPO:?Set CR_REPO in .env.deploy}"
: "${CR_USERNAME:?Set CR_USERNAME in .env.deploy}"
: "${CR_PASSWORD:?Set CR_PASSWORD in .env.deploy}"

IMAGE_BASE="$CR_REGISTRY/$CR_NAMESPACE/$CR_REPO"
TAG="$(date +%Y%m%d-%H%M%S)"   # timestamped tag for rollback
IMAGE_LATEST="$IMAGE_BASE:latest"
IMAGE_TAGGED="$IMAGE_BASE:$TAG"

echo "==> Lead Engine — Deploy to Byteplus CR"
echo "    Registry : $CR_REGISTRY"
echo "    Image    : $IMAGE_LATEST"
echo "    Tag      : $TAG"
echo ""

echo "--> Authenticating to Byteplus CR..."
echo "$CR_PASSWORD" | docker login "$CR_REGISTRY" -u "$CR_USERNAME" --password-stdin

echo "--> Building image..."
docker build --platform linux/amd64 -t "$IMAGE_LATEST" -t "$IMAGE_TAGGED" "$SCRIPT_DIR"

echo "--> Pushing :latest ..."
docker push "$IMAGE_LATEST"

echo "--> Pushing :$TAG (for rollback)..."
docker push "$IMAGE_TAGGED"

echo ""
echo "==> Push complete!"
echo ""
echo "    Next: restart your ECS instance from the Byteplus console."
echo "    The instance will pull :latest automatically on startup."
echo ""
echo "    Console → ECS → your instance → Restart"
echo "    App URL : http://${ECS_IP:-<your-ecs-ip>}:8080"