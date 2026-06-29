# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Local setup
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
cp .env.example .env   # then fill in LLM_API_KEY, LLM_MODEL, etc.

# Run the CLI pipeline
python3 main.py --input "10 gen leads.csv" --output-dir ./output --concurrency 10
python3 main.py --input leads.json --campaign kling --output-dir ./output

# Run the web UI (no DB — no persistence)
python3 ui/server.py              # → http://localhost:8080

# Run the web UI with PostgreSQL
export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/seedance
python3 ui/server.py

# Docker Compose (app + local PostgreSQL, easiest full local setup)
make dev          # docker compose up --build
make dev-stop     # docker compose down

# Deploy (build + push image to Byteplus CR)
make push         # requires .env.deploy and .env.production
```

## Architecture

### Core principle

**Claude is the reasoning engine. Python is the control plane.**

Claude only returns structured JSON. Python orchestrates the pipeline, validates scores, clamps sub-scores, re-sums totals, and enforces routing — it never trusts Claude's output unconditionally.

### Campaign system

Every product campaign (Seedance, Kling, future products) is a **directory under `skills/`**. A campaign must contain: `config.json`, `product.md`, `icp.md`, `signals.md`, `disqualification.md`, `qualification.md`. Shared rules live in `skills/common/` and are injected into every campaign prompt automatically.

At startup, `campaigns/registry.py:load_all_campaigns()` scans `skills/`, skips `common/`, and calls `campaigns/loader.py:CampaignLoader.load()` for each directory that has a `product.md`. The loader reads the markdown files and assembles two system prompts (stage 1 and stage 2) from the `_STAGE1_TEMPLATE` and `_STAGE2_TEMPLATE` string templates in `campaigns/loader.py`. The assembled `CampaignContext` (frozen dataclass) is stored in the in-memory registry and passed explicitly through the entire pipeline — there is no global mutable state.

**To add a new campaign** (e.g., Google Veo): create `skills/veo/` with the six required files. It will be auto-discovered on next startup.

### Two-stage Claude pipeline

```
raw input → normalize → enrich → Stage 1 (evidence extraction) → Stage 2 (qualification decision) → Python router → CSV + JSON artifacts
```

- **Stage 1** (`claude/stage1_evidence.py`): extracts structured evidence only — segment, signal_strength, observed/inferred/unknown evidence, hard_disqualifiers. Does **not** score.
- **Stage 2** (`claude/stage2_decision.py`): receives Stage 1 output only (not the raw lead), applies the 5-factor scoring model, returns decision + sub-scores.
- **Python router** (`router/decision_router.py`): final authority. Hard disqualifiers and `signal_strength == "low"` always force `disqualified` regardless of score.

Score clamping happens in Python after Stage 2: sub-scores are clamped to their configured maxima, total is re-summed, decision is re-derived from thresholds.

### Scoring model (`config.py`)

| Factor | Max | Weight |
|---|---|---|
| `segment_fit` | 30 | Primary filter — vertical match |
| `buyer_fit` | 25 | Seniority + decision power |
| `pain_fit` | 20 | Explicit or implied production pain |
| `workflow_fit` | 15 | Signal engagement quality + video/creative workflow |
| `commercial_fit` | 10 | Company size + momentum signals |

Thresholds: `≥70 → qualified`, `<60 → disqualified`, `60–69 → review`. Per-campaign overrides in `config.json`.

### Web UI (`ui/server.py`)

FastAPI app serving an SSR HTML UI (all markup returned as `HTMLResponse`). No separate frontend build step. Routes follow the pattern `/api/campaigns/{campaign_id}/...` for campaign-scoped resources and `/api/runs/...` for run-level operations. SSE (`/api/progress/{run_id}`) streams live pipeline logs to the browser.

Auth is session-based (`SessionMiddleware`). Credentials from `APP_USERNAME` / `APP_PASSWORD` env vars (default: `admin` / `changeme`).

### Persistence (`db/store.py`)

asyncpg connection pool. Schema created automatically on first startup via `store.create_tables()`. If `DATABASE_URL` is not set, the app runs in-memory-only mode (no run history across restarts). Key tables: `campaigns`, `runs`, `leads`, `settings`, `campaign_settings`, `exclusions`, `email_templates`, `instantly_campaigns`, `instantly_routing`.

### Multi-campaign Instantly routing

Outreach can fan out to multiple Instantly campaigns. `instantly_campaigns` stores a saved registry (label + external campaign id); `instantly_routing` maps a lead **vertical** → campaign. Each lead's target resolves as **per-lead override (`leads.instantly_campaign_id`) → vertical routing map → `INSTANTLY_CAMPAIGN_ID` default**. Configured in the UI under Settings → Routing. At send time `send_emails` groups eligible leads by resolved campaign and pushes each group via `_send_to_instantly(leads, campaign_id)`. Status badges and CSV export query each lead against its own campaign. Empty routing map = everything to the default campaign (unchanged single-campaign behavior).

### LLM client (`llm/client.py`)

Wraps LiteLLM so any provider (Anthropic Claude, BytePlus ModelArk, OpenAI, etc.) works via `.env` config:

```dotenv
LLM_MODEL=anthropic/claude-haiku-4-5-20251001   # or seed-2-0-pro-260328
LLM_API_KEY=...
LLM_PROVIDER=openai                              # omit for auto-detect
LLM_API_BASE=https://ark.ap-southeast.bytepluses.com/api/v3  # for BytePlus
```

### Output structure

Each run writes to `output/runs/{run_id}/`:
- `qualified_leads.csv`, `review_leads.csv`, `disqualified_leads.csv`
- `artifacts/{lead_id}_input.json`, `_{lead_id}_evidence.json`, `_{lead_id}_decision.json`
- `run.log`

Artifacts are the primary debugging tool — they preserve exactly what entered and exited each pipeline stage.
