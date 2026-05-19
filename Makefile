.PHONY: help generate-user-data push deploy logs dev dev-stop \
        k8s-apply k8s-rollout k8s-status k8s-logs k8s-delete k8s-cr-secret k8s-secrets

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
	@echo "  Kubernetes (BytePlus VKE):"
	@echo "    make k8s-cr-secret        Push CR pull secret into the cluster"
	@echo "    make k8s-secrets          Push app secrets from .env.production into cluster"
	@echo "    make k8s-apply            Apply all k8s manifests"
	@echo "    make k8s-rollout          Rolling restart to pick up a new image"
	@echo "    make k8s-status           Show pods / svc / ingress"
	@echo "    make k8s-logs             Tail logs across all replicas"
	@echo "    make k8s-delete           Remove ALL k8s resources (destructive)"
	@echo "    NAMESPACE=lead-engine-staging make k8s-apply   Target staging namespace"
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

# ── Kubernetes (BytePlus VKE) ─────────────────────────────────────────────────
# Set NAMESPACE to override (default: lead-engine)
NAMESPACE ?= lead-engine

# Push the CR pull secret into the cluster so pods can pull the image
k8s-cr-secret: _require-env-deploy
	kubectl create secret docker-registry byteplus-cr-secret \
	  --namespace $(NAMESPACE) \
	  --docker-server=$(CR_REGISTRY) \
	  --docker-username=$(CR_USERNAME) \
	  --docker-password=$(CR_PASSWORD) \
	  --dry-run=client -o yaml | kubectl apply --validate=false -f -

# Push app secrets from .env.production into the cluster
k8s-secrets: _require-env-production
	@set -a && . ./.env.production && set +a && \
	kubectl create secret generic lead-engine-secrets \
	  --namespace $(NAMESPACE) \
	  --from-literal=LLM_API_KEY="$$LLM_API_KEY" \
	  --from-literal=APOLLO_API_KEY="$$APOLLO_API_KEY" \
	  --from-literal=APP_PASSWORD="$$APP_PASSWORD" \
	  --from-literal=SESSION_SECRET="$$SESSION_SECRET" \
	  --from-literal=INSTANTLY_API_KEY="$$INSTANTLY_API_KEY" \
	  --from-literal=INSTANTLY_CAMPAIGN_ID="$$INSTANTLY_CAMPAIGN_ID" \
	  --from-literal=INSTANTLY_SCORE_THRESHOLD="$$INSTANTLY_SCORE_THRESHOLD" \
	  --from-literal=PLACEHOLDER_EMAIL="$$PLACEHOLDER_EMAIL" \
	  --from-literal=DATABASE_URL="$$DATABASE_URL" \
	  --dry-run=client -o yaml | kubectl apply --validate=false -f -

# Apply all k8s manifests
k8s-apply:
	kubectl apply --validate=false -f k8s/

# Trigger a rolling restart (pick up new image)
k8s-rollout:
	kubectl rollout restart deployment/lead-engine --namespace $(NAMESPACE)
	kubectl rollout status  deployment/lead-engine --namespace $(NAMESPACE) --timeout=300s

# Watch pod status
k8s-status:
	kubectl get pods,svc,ingress -n $(NAMESPACE)

# Tail app logs across all replicas
k8s-logs:
	kubectl logs -f -l app=lead-engine --namespace $(NAMESPACE) --max-log-requests=6

# Remove all k8s resources (destructive — prompts for confirmation)
k8s-delete:
	@read -p "Delete ALL lead-engine resources in namespace '$(NAMESPACE)'? [y/N] " ans && \
	  [ "$$ans" = "y" ] && kubectl delete -f k8s/ || echo "Aborted."

# ── Guards ────────────────────────────────────────────────────────────────────

_require-env-deploy:
	@test -f .env.deploy || (echo "ERROR: .env.deploy not found. Copy .env.deploy.example and fill in values."; exit 1)

_require-env-production:
	@test -f .env.production || (echo "ERROR: .env.production not found. Copy .env.production.example and fill in values."; exit 1)
