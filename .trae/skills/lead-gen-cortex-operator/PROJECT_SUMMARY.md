# Project Summary

This summary captures what the repository does today so the Claude skill stays aligned with the actual codebase while still being able to execute the core scoring path directly.

## System Purpose

The project is an LLM-assisted outbound lead qualification engine. It ingests lead exports, enriches them, scores them through a two-stage reasoning flow, routes them deterministically, and stores reviewable outputs.

For skill design, the important separation is:

- core scoring logic is self-contained
- backend services are optional operator infrastructure

## End-To-End Flow

```text
lead input
-> CSV/JSON ingestion
-> normalization and dedupe
-> enrichment
-> Stage 1 evidence extraction
-> Stage 2 qualification scoring
-> Python routing
-> CSV queues + JSON artifacts
-> optional persistence, review UI, memory, and email workflow
```

## Source Of Truth By Layer

- Ingestion: `main.py`, `normalization/csv_loader.py`
- Schema and cleanup: `normalization/normalizer.py`
- Enrichment: `tools/enrichment.py`, `tools/apollo_client.py`, `tools/web_enrichment.py`
- LLM interface: `llm/client.py`
- Stage 1: `claude/stage1_evidence.py`
- Stage 2: `claude/stage2_decision.py`
- Routing: `router/decision_router.py`
- Outputs: `output/csv_writer.py`
- Campaign prompts: `campaigns/`, `skills/`
- Web operations: `ui/server.py`
- Persistence: `db/store.py`

## Important Behavioral Rules

### Core control-plane principle

Python is authoritative for workflow, validation, routing, and persistence. The model supplies structured reasoning, not final uncontrolled state.

For the skill, this means:

- the skill may run the two LLM calls directly
- the skill must still enforce deterministic validation and routing after the model responds

### Stage split

- Stage 1 extracts structured evidence.
- Stage 2 scores and decides from Stage 1 only.

### Routing overrides

- confirmed hard disqualifiers force `disqualified`
- `signal_strength == "low"` also forces `disqualified`

### Auditability

Each lead can produce input, evidence, and decision artifacts. These are the main debugging surface.

## Campaign Model

Campaign behavior is assembled from markdown and JSON files under `skills/<campaign_id>/` plus shared rules under `skills/common/`.

Current repository-supported campaign files:

- `config.json`
- `product.md`
- `icp.md`
- `signals.md`
- `disqualification.md`
- `qualification.md`

The campaign loader builds prompt text from these files and injects weights and thresholds into the stage prompts.

This means a skill can execute the same campaign logic directly if it reads the same files and preserves the same thresholds and score caps.

## Email Workflow Reality

The repository includes email-template handling, preview, export, and optional sending through the web server. That capability exists downstream of lead scoring and depends on UI and DB flows.

It should be described as:

- template-backed outreach preparation
- optional export/send workflow

It should not be described as:

- a native part of `main.py`
- a fully production-hardened outbound automation system

## Skill Execution Model

The preferred skill behavior is:

1. normalize a lead
2. enrich it directly
3. run Stage 1 evidence extraction directly
4. run Stage 2 scoring directly
5. apply deterministic routing rules directly
6. use backend APIs only when the user wants persistence, run history, memory, versioning, or email workflows

## Operational Caveats

- Apollo enrichment is disabled unless configured.
- Website and LinkedIn scraping are best-effort.
- DB-backed persistence is optional.
- The system is strong v1/pilot quality, not fully production-hardened.
