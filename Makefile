.PHONY: help generate-user-data push deploy logs dev dev-stop

# Load deploy + production config
-include .env.deploy
-include .env.production

APP_DIR ?= /opt/lead-engine

IMAGE_BASE = $(CR_REGISTRY)/$(CR_NAMESPACE)/$(CR_REPO)
IMAGE      = $(IMAGE_BASE):latest

# ── Help ──────────────────────────────────────────────────────────────────────

help:
	@echo ""
	@echo "  Lead Generation Engine — Deployment Commands"
	@echo ""
	@echo "  First time (run in order):"
	@echo "    make generate-user-data   Generate ECS User Data script from your env files"
	@echo "    make push                 Build image and push to Byteplus CR"
	@echo "    [paste user-data.sh into Byteplus Console → ECS → User Data, then start instance]"
	@echo ""
	@echo "  Every deploy:"
	@echo "    make deploy               Build + push new image to CR"
	@echo "    [restart ECS from Byteplus Console → instance pulls new image automatically]"
	@echo ""
	@echo "  Local dev:"
	@echo "    make dev                  Run locally with docker-compose (app + local postgres)"
	@echo "    make dev-stop             Stop local containers"
	@echo ""

# ── First-time setup ──────────────────────────────────────────────────────────

generate-user-data: _require-env-deploy _require-env-production
	@bash scripts/generate-user-data.sh

# ── Build & Push ──────────────────────────────────────────────────────────────

push: _require-env-deploy
	@./deploy.sh

# Alias: `make deploy` does the same as `make push`
deploy: push

# ── Local dev ─────────────────────────────────────────────────────────────────

dev:
	@docker compose up --build

dev-stop:
	@docker compose down

# ── Guards ────────────────────────────────────────────────────────────────────

_require-env-deploy:
	@test -f .env.deploy || (echo "ERROR: .env.deploy not found. Copy .env.deploy.example and fill in values."; exit 1)

_require-env-production:
	@test -f .env.production || (echo "ERROR: .env.production not found. Copy .env.production.example and fill in values."; exit 1)
