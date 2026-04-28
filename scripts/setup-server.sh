#!/usr/bin/env bash
# One-time bootstrap for a fresh Byteplus ECS instance (Ubuntu 22.04).
# Run via: make setup
# Installs Docker and creates the app directory. No git needed.
set -euo pipefail

: "${APP_DIR:?APP_DIR must be set (e.g. /opt/lead-engine)}"

echo "==> [1/3] Installing Docker..."
apt-get update -qq
apt-get install -y --no-install-recommends ca-certificates curl

install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
  https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | tee /etc/apt/sources.list.d/docker.list > /dev/null

apt-get update -qq
apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

systemctl enable --now docker
echo "    Docker $(docker --version) installed."

echo "==> [2/3] Creating app directory at $APP_DIR..."
mkdir -p "$APP_DIR/output/runs"
echo "    Directory created."

echo "==> [3/3] Verifying Docker Compose..."
docker compose version

echo ""
echo "==> Server setup complete!"
echo ""
echo "    Next steps:"
echo "    1. Run:  make deploy"
echo "       (rsyncs your local code + uploads .env.production automatically)"
