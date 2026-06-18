---
name: "lead-gen-cortex-operator"
description: "Runs self-contained lead enrichment, 2-stage LLM scoring, and deterministic routing. Invoke when scoring Trigify leads, profiling accounts, or drafting new campaign logic."
---

# Lead Gen Cortex Operator

Use this skill when the user wants Claude Code to execute or reason about the lead-generation workflow directly, without depending on backend APIs for the core scoring path.

## What This Skill Covers

This skill turns the repository logic into a self-contained operator protocol:

1. Ingest lead data from Trigify-style CSV or normalized JSON.
2. Normalize and deduplicate leads.
3. Enrich each lead with company, website, LinkedIn, and heuristic context.
4. Run two LLM passes:
   - Stage 1: evidence extraction
   - Stage 2: qualification scoring and decision
5. Apply deterministic Python routing.
6. Review CSV outputs and JSON artifacts.
7. Optionally use the backend only for storage, run history, or email workflow.

Bundled runtime support inside the skill package:

- `runner/score_csv.py`: standalone CSV scorer
- `runner/evaluate_benchmark.py`: benchmark evaluator
- `campaigns/`: bundled campaign packs and shared rules
- `docs/RUNBOOK.md`: exact execution workflow
- `docs/EXAMPLES.md`: ready-to-use command examples
- `docs/INSTALL.md`: upload and install guide
- `docs/RELEASE_CHECKLIST.md`: release process
- `docs/CHANGELOG.md`: release history
- `VERSION`: package version marker

## Core Position

The skill should not rely on backend APIs for the core qualification decision.

Default model:

- the skill can ingest and normalize the lead
- the skill can gather enrichment context
- the skill can make the two LLM calls directly
- the skill can apply deterministic score validation and routing logic itself

Backend APIs are optional and only used when the user wants:

- batch orchestration
- persistent memory
- run history
- campaign version storage
- email preview, export, or sending

## When To Invoke

Invoke this skill when the user asks to:

- process or inspect a Trigify lead export
- run or explain the lead-scoring pipeline directly
- trace how the two LLM calls work
- work with campaign prompts under `skills/`
- profile a company or person for fit
- review qualified, review, or disqualified output queues
- prepare outreach/email output from scored leads
- package this workflow into a repeatable operator procedure

## Repository Mental Model

Treat the project as two layers:

- self-contained scoring logic
  - input -> normalize -> enrich -> Stage 1 -> Stage 2 -> route
- optional backend services
  - run management, persistence, memory, campaign versions, template-backed email flows

Core modules:

- `main.py`: batch orchestration
- `normalization/csv_loader.py`: Trigify CSV mapping
- `normalization/normalizer.py`: validation, cleanup, dedupe
- `tools/enrichment.py`: async enrichment orchestration
- `tools/apollo_client.py`: Apollo organization enrichment with DB cache
- `tools/web_enrichment.py`: website and LinkedIn scraping
- `claude/stage1_evidence.py`: structured evidence extraction
- `claude/stage2_decision.py`: structured scoring decision
- `router/decision_router.py`: Python-authoritative routing
- `output/csv_writer.py`: CSV and artifact writing
- `campaigns/`: campaign prompt assembly
- `skills/`: campaign knowledge files
- `ui/server.py`: optional review UI and operator APIs
- `db/store.py`: optional persistence and memory

## Default Operating Procedure

Follow this order unless the user asks for something narrower.

### 1. Understand The Run Context

- Identify whether the user is working from CSV, JSON, prior run artifacts, or the web UI.
- Identify campaign context such as `seedance` or `kling`.
- Check whether the task is direct scoring, profiling, batch execution, packaging, debugging, or workflow design.

### 2. Confirm Input Shape

For Trigify CSVs, expect fields such as:

- `NAME`
- `JOB TITLE`
- `COMPANY`
- `INDUSTRY`
- `COMPANY SIZE`
- `ICP SCORE`
- `COMPANY URL`
- `LINKEDIN PROFILE`
- `LOCATION`
- `HQ COMPANY LOCATION`
- `INTERACTION`
- `POST URL`

If the input is JSON, ensure it can become the internal `LeadInput` schema:

- `lead_id`
- `person_name`
- `title`
- `company_name`
- `company_domain`
- `company_size`
- `linkedin_url`
- `signal_text`
- `signal_type`
- `signal_timestamp`

### 3. Run The Core Scoring Protocol Directly

Default protocol:

```text
raw input
-> normalize
-> enrich
-> Stage 1 evidence extraction
-> Stage 2 qualification decision
-> Python routing
-> CSV outputs + JSON artifacts
```

Important rules:

- Python owns control flow and score integrity.
- Stage 2 consumes Stage 1 evidence only, not raw lead input.
- Hard disqualifiers and low signal can override model scoring.
- Artifacts are the audit trail and should be preserved.

For actual CSV execution inside Claude Code, prefer the bundled script:

```bash
python3 .trae/skills/lead-gen-cortex-operator/runner/score_csv.py \
  --input "/absolute/path/to/leads.csv" \
  --output-dir "/absolute/path/to/output" \
  --campaign seedance
```

Required environment:

- `LLM_API_KEY`
- `LLM_MODEL`

Optional environment:

- `LLM_API_BASE`
- `APOLLO_ENABLED` (`true` to turn on Apollo.io firmographic enrichment)
- `APOLLO_API_KEY` (required when Apollo is enabled)
- `APOLLO_TIMEOUT_SECONDS` (per-request Apollo timeout, default 10)

To enable Apollo enrichment, either set `APOLLO_ENABLED=true` with `APOLLO_API_KEY`, or pass `--enable-apollo --apollo-api-key ...`:

```bash
python3 .trae/skills/lead-gen-cortex-operator/runner/score_csv.py \
  --input "/absolute/path/to/leads.csv" \
  --output-dir "/absolute/path/to/output" \
  --campaign seedance \
  --enable-apollo --apollo-api-key "$APOLLO_API_KEY"
```

### 4. Direct Skill Execution Rules

When operating without backend APIs, follow this exact decision path.

#### Step A. Normalize Input

Convert the lead into the internal shape:

- `lead_id`
- `person_name`
- `title`
- `company_name`
- `company_domain`
- `company_size`
- `linkedin_url`
- `signal_text`
- `signal_type`
- `signal_timestamp`

#### Step B. Enrich

Gather only best-effort enrichment:

- role seniority from title
- company website summary
- LinkedIn public profile summary
- Apollo.io firmographics when enabled — the runner calls the Apollo organization
  enrich API (domain first, company name fallback) and attaches a compact summary
  (`industry`, `estimated_num_employees`, `annual_revenue_printed`, `founded_year`,
  `country`/`city`, `keywords`, `short_description`) to the enrichment artifact and
  Stage 1 payload. Results are cached per run so repeated companies hit the API once.
  Missing data stays absent rather than fabricated, so Stage 1 treats it as unknown.
- optional prior memory if the user explicitly wants history-aware context

#### Step C. Stage 1 LLM Call

Goal:

- extract evidence only

Required output shape:

```json
{
  "lead_id": "string",
  "segment": "string",
  "candidate_use_case": "string",
  "signal_strength": "high|medium|medium-low|low",
  "observed_evidence": ["string"],
  "inferred_evidence": ["string"],
  "unknowns": ["string"],
  "hard_disqualifiers": ["string"]
}
```

Stage 1 rules:

- do not score
- separate observed from inferred evidence
- only confirmed negatives go into `hard_disqualifiers`
- tentative negatives go to `unknowns` or `inferred_evidence`

#### Step D. Stage 2 LLM Call

Goal:

- score the lead from Stage 1 evidence only

Required output shape:

```json
{
  "lead_id": "string",
  "decision": "qualified|disqualified|review",
  "score": 0,
  "confidence": "high|medium|low",
  "segment_fit": 0,
  "workflow_fit": 0,
  "pain_fit": 0,
  "buyer_fit": 0,
  "commercial_fit": 0,
  "qualification_reasons": ["string"],
  "disqualification_reasons": ["string"],
  "reasoning_summary": "string",
  "suggested_use_case": "string"
}
```

#### Step E. Deterministic Validation

The skill must validate Stage 2 output itself:

- clamp `segment_fit` to campaign max
- clamp `buyer_fit` to campaign max
- clamp `pain_fit` to campaign max
- clamp `workflow_fit` to campaign max
- clamp `commercial_fit` to campaign max
- re-sum score from the clamped subscores
- re-derive the decision from thresholds

Default max weights:

- `segment_fit`: 30
- `buyer_fit`: 25
- `pain_fit`: 20
- `workflow_fit`: 15
- `commercial_fit`: 10

Default thresholds:

- `score >= 70` -> `qualified`
- `score < 60` -> `disqualified`
- otherwise -> `review`

#### Step F. Routing Overrides

Always apply these after Stage 2:

- if `hard_disqualifiers` is non-empty -> `disqualified`
- if `signal_strength == "low"` -> `disqualified`

Only after these checks is the final routing authoritative.

### 5. Interpret Outputs

Primary outputs:

- `qualified_leads.csv`
- `review_leads.csv`
- `disqualified_leads.csv`
- `artifacts/{lead_id}_input.json`
- `artifacts/{lead_id}_enrichment.json`
- `artifacts/{lead_id}_evidence.json`
- `artifacts/{lead_id}_decision.json`
- `run.log`

When reviewing outcomes, prioritize:

1. routing correctness
2. score integrity
3. evidence quality
4. missing data patterns
5. template and outreach readiness

### 6. Use Backend Only When Helpful

Use the backend when the user wants infrastructure rather than just reasoning:

- batch run management
- persistent entity memory
- campaign version activation
- run history lookup
- email preview/export/send

Do not require the backend when the user only wants:

- a single lead scored
- a company or person profile
- a campaign prompt explanation
- direct reasoning on a fresh CSV sample

### 7. Handle Email Workflow Carefully

The current email workflow lives in `ui/server.py` and `db/store.py`.

What exists:

- campaign-scoped template storage
- email preview generation
- CSV export including subject and body
- optional Instantly send flow for eligible leads

What does not exist as a core CLI behavior:

- fully automated email generation inside `main.py`

Treat email as a downstream operator layer on top of scored leads, not part of the scoring engine itself.

## Stepped Iteration Loop

When the user wants to evolve the workflow, iterate in this order:

1. Map the campaign context and scoring assumptions.
2. Confirm whether the user wants direct skill execution or backend-assisted workflow.
3. Reuse existing campaign files and thresholds before inventing new logic.
4. Keep scoring behavior deterministic and auditable.
5. Validate outputs from artifacts, structured JSON, and subscores.
6. Only then extend downstream email, memory, or campaign-management behavior.

## Constraints

- Do not silently change routing logic.
- Do not trust Stage 2 scores without Python-side validation.
- Do not collapse Stage 1 and Stage 2 into one call unless explicitly redesigning.
- Do not let memory or historical examples silently override current evidence.
- Do not treat email sending as guaranteed production-safe.
- Do not require backend APIs for core scoring when the user wants a direct skill execution path.

## Deliverable Shapes

Depending on the request, produce one or more of:

- architecture explanation
- direct scoring checklist
- direct CSV scoring run
- profiling checklist
- campaign prompt review
- artifact review summary
- packaging plan
- migration plan into a Claude Code skill

## Example Invocations

- "Process this Trigify CSV and summarize which leads were qualified."
- "Score this single person and company directly without running the backend."
- "Use the campaign rubric and tell me if this contact is a fit."
- "Explain why this lead was routed to review."
- "Turn the Seedance workflow into a reusable operator skill."
- "Show me how campaign prompts affect the two LLM stages."
- "Prepare email previews for the latest run."
