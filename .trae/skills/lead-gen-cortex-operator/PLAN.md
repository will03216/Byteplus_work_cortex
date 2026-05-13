# Lead Gen Cortex Skill Plan

This plan converts the existing repository workflow into a reusable Claude Code skill that can execute the core scoring path directly, without requiring backend APIs for the two LLM calls and deterministic routing.

## Goal

Create an installable Claude Code skill that helps an operator:

1. understand the current lead pipeline
2. run direct enrichment and scoring safely
3. review outputs and artifacts
4. optionally use the downstream email-template workflow
5. iterate on the operating procedure without rewriting the repo first

## Current Workflow Translation

The repository already has five reusable layers that can be translated into skill instructions:

1. Input ingestion
   - `main.py`
   - `normalization/csv_loader.py`
   - `normalization/normalizer.py`
2. Enrichment
   - `tools/enrichment.py`
   - `tools/apollo_client.py`
   - `tools/web_enrichment.py`
3. Two-stage reasoning
   - `claude/stage1_evidence.py`
   - `claude/stage2_decision.py`
4. Deterministic routing and outputs
   - `router/decision_router.py`
   - `output/csv_writer.py`
5. Operator UI and downstream email workflow
   - `ui/server.py`
   - `db/store.py`

## Skill Packaging Strategy

### Step 1. Freeze The Scoring Contract

Document the non-negotiable process:

- Trigify CSV or normalized JSON in
- normalization and dedupe
- async enrichment
- Stage 1 evidence extraction
- Stage 2 scoring
- Python routing
- optional CSV and JSON artifacts out

This is already captured in `SKILL.md`.

### Step 2. Preserve Campaign Awareness

The repo is campaign-driven, not single-product only.

The skill must teach Claude to:

- detect campaign context
- look under `skills/` and `campaigns/`
- avoid hardcoding Seedance assumptions when another campaign exists
- respect per-campaign weights and thresholds

### Step 3. Separate Core Scoring From Backend Services

The skill must preserve the existing product boundary:

- core scoring can run directly inside the skill protocol
- storage, memory, UI, and email flows are optional backend services

This prevents accidental coupling between "qualification logic" and "operator infrastructure."

### Step 4. Package A Minimal Installable Artifact

Minimal v1 package:

- `.trae/skills/lead-gen-cortex-operator/SKILL.md`
- optional supporting docs in the same folder
- zip archive containing the skill directory

### Step 5. Validate The Package

Validation checklist:

- frontmatter is valid
- description says both what the skill does and when to invoke it
- only new files were added
- zip contains the skill directory root

## Recommended Iteration Roadmap

### Iteration 1. Self-Contained Scoring Skill

Objective:

- provide repository-aware guidance
- make the scoring workflow directly executable by the skill
- support architecture review, single-lead scoring, and output review

Deliverables:

- installable skill
- architecture summary
- packaging zip

### Iteration 2. Guided Input Templates

Objective:

- add example user prompts for common tasks
- add CSV and JSON input contract examples
- add single-person or single-company profile examples
- add common troubleshooting notes

Potential additions:

- `EXAMPLES.md`
- `RUNBOOK.md`

### Iteration 3. Campaign Authoring Companion

Objective:

- help create or tune campaign directories under `skills/`
- explain how `campaigns/loader.py` assembles prompts
- preserve threshold and weight invariants

Potential additions:

- campaign file checklist
- scoring calibration worksheet

### Iteration 4. Backend Companion

Objective:

- formalize when the skill should use backend memory, run history, and email flows
- separate direct skill scoring from backend-assisted batch operation
- capture persistence and activation prerequisites

Potential additions:

- email template playbook
- readiness checklist before sending

## Risks To Keep Explicit

- Apollo enrichment is optional and environment-driven.
- Website and LinkedIn scraping are best-effort, not guaranteed.
- The repo is audit-friendly but still pilot-grade.
- Email sending depends on DB and Instantly configuration.
- Some README text is slightly behind the actual web app capabilities, so the code should be treated as source of truth.
- The skill should not silently drift from the repo's deterministic routing rules.

## Definition Of Done For This Package

The package is complete when:

- Claude Code can install the skill from the zip
- the skill describes the real workflow accurately
- the skill can execute the core two-call reasoning path directly
- the skill distinguishes scoring from email operations
- the skill helps an operator work in a stepped, auditable way
