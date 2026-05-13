# Agentic Operator Blueprint

## Purpose

This document defines the recommended technical design for evolving the current repository from a batch lead qualification engine into an agentic, memory-backed outbound operator with strict scoring guardrails.

The design goal is:

1. keep scoring behavior highly consistent
2. make the system more agentic and useful across repeated workflows
3. add persistent memory across runs, companies, people, and campaigns
4. support new products and campaigns safely
5. preserve auditability, deterministic enforcement, and human control

This blueprint is intentionally anchored to the current codebase rather than proposing a greenfield rebuild.

## Current State

The repository already implements a strong deterministic scoring kernel:

- batch orchestration in `main.py`
- normalization in `normalization/`
- enrichment in `tools/`
- Stage 1 evidence extraction in `claude/stage1_evidence.py`
- Stage 2 scoring and decision in `claude/stage2_decision.py`
- Python-authoritative routing in `router/decision_router.py`
- campaign prompt assembly in `campaigns/`
- campaign content files in `skills/`
- run management and email template workflows in `ui/server.py`
- persistence in `db/store.py`

This is the correct foundation. The next version should add an agent layer around this kernel, not replace it.

## Product Outcome

The target system should support the following operator behaviors:

1. ingest a new CSV batch for a selected campaign
2. score and route leads with stable policy behavior
3. remember prior companies, contacts, evidence, and outcomes
4. answer profile questions about a company or person
5. explain whether an account or contact is a fit and why
6. draft a new campaign pack for a new product
7. compare proposed campaign logic against historical labeled examples
8. prepare outreach previews using campaign-scoped templates

## Core Design Principle

Use this architecture:

```text
agentic orchestration
        +
memory and retrieval
        +
deterministic scoring kernel
        +
human approval for policy changes
```

The most important rule is:

`memory may evolve automatically; policy may not`

In practice:

- the system can write new memories automatically
- the system can summarize patterns automatically
- the system can retrieve similar historical examples automatically
- the system cannot silently change scoring thresholds, routing rules, disqualifiers, or campaign policy

## Target Architecture

### Layer 1. Deterministic Scoring Kernel

This remains the core backend and should stay mostly intact.

Primary modules:

- `main.py`
- `claude/stage1_evidence.py`
- `claude/stage2_decision.py`
- `router/decision_router.py`
- `output/csv_writer.py`

Responsibilities:

- normalize and enrich leads
- produce evidence and decision JSON
- enforce score integrity
- enforce routing overrides
- write auditable artifacts

This layer is the authoritative scoring engine.

### Layer 2. Agent Orchestration Layer

This is the new system behavior layer.

Responsibilities:

- select campaign
- launch batch runs
- query memory before scoring
- answer profile questions
- explain historical decisions
- prepare or compare campaign drafts
- prepare outreach previews and export actions

This layer should operate through explicit backend actions, not by ad hoc prompt improvisation.

Recommended deployment options:

- a Claude Code skill that can operate the repo locally
- a service-backed agent that calls your FastAPI backend
- later, an MCP server exposing backend actions as tools

### Layer 3. Memory and Retrieval Layer

This is the persistent learning layer.

Responsibilities:

- store lead-level history
- store company-level memory
- store person-level memory
- store run outcomes and human review signals
- retrieve similar cases
- summarize prior context for new decisions

This layer should be additive and retrieval-oriented. It should not directly modify scoring policy.

### Layer 4. Policy and Evaluation Layer

This is the guardrail layer.

Responsibilities:

- version campaigns
- version scoring policies
- evaluate candidate changes offline
- require human approval before activation
- preserve rollback ability

This layer makes the system stable over time.

## Operating Modes

The system should support three modes.

### Mode A. Batch Campaign Processing

Inputs:

- campaign id
- CSV file
- optional operator notes

Outputs:

- run record
- scored leads
- artifacts
- memory updates

### Mode B. Entity Profiling

Inputs:

- company name or domain
- optionally person name or LinkedIn URL
- target campaign

Outputs:

- company profile
- person profile
- historical memory
- fit summary
- current recommendation
- explanation with evidence and unknowns

### Mode C. Campaign Authoring

Inputs:

- new product description
- ICP and anti-ICP notes
- target personas
- disqualification rules
- example good and bad leads

Outputs:

- draft campaign pack
- draft thresholds and weights
- evaluation report against benchmark set
- approval-ready activation package

## Recommended Data Model

The current `runs`, `leads`, `campaigns`, `email_templates`, and `skill_files` tables are a strong start. Add the following tables.

### 1. `company_profiles`

Purpose:

- one memory record per company/domain

Suggested fields:

- `id`
- `canonical_domain`
- `company_name`
- `normalized_company_name`
- `latest_structured_profile` JSONB
- `latest_summary`
- `latest_fit_by_campaign` JSONB
- `first_seen_at`
- `last_seen_at`
- `times_seen`
- `last_run_id`

### 2. `person_profiles`

Purpose:

- one memory record per contact/person

Suggested fields:

- `id`
- `person_name`
- `normalized_person_name`
- `linkedin_url`
- `company_profile_id`
- `latest_title`
- `latest_summary`
- `latest_fit_by_campaign` JSONB
- `first_seen_at`
- `last_seen_at`
- `times_seen`
- `last_run_id`

### 3. `lead_memory`

Purpose:

- immutable or append-only lead-level scoring history

Suggested fields:

- `id`
- `run_id`
- `lead_id`
- `campaign_id`
- `company_profile_id`
- `person_profile_id`
- `raw_input` JSONB
- `enrichment_snapshot` JSONB
- `evidence_snapshot` JSONB
- `decision_snapshot` JSONB
- `routing`
- `policy_version`
- `campaign_version`
- `created_at`

### 4. `review_feedback`

Purpose:

- human corrections and outcomes

Suggested fields:

- `id`
- `run_id`
- `lead_id`
- `campaign_id`
- `feedback_type`
- `feedback_reason`
- `corrected_decision`
- `fit_outcome`
- `owner`
- `created_at`

`feedback_type` examples:

- `false_positive`
- `false_negative`
- `wrong_persona`
- `wrong_company`
- `duplicate`
- `good_fit_bad_timing`
- `won`
- `lost`

### 5. `campaign_versions`

Purpose:

- immutable history of campaign policy changes

Suggested fields:

- `id`
- `campaign_id`
- `version`
- `status`
- `config_json`
- `product_md`
- `icp_md`
- `signals_md`
- `disqualification_md`
- `qualification_md`
- `created_by`
- `created_at`
- `approved_by`
- `approved_at`

Statuses:

- `draft`
- `candidate`
- `active`
- `archived`

### 6. `policy_versions`

Purpose:

- explicit version tracking for scoring kernel settings

Suggested fields:

- `id`
- `campaign_id`
- `policy_version`
- `score_weights` JSONB
- `qualified_threshold`
- `disqualified_threshold`
- `routing_rules` JSONB
- `status`
- `created_at`
- `approved_at`

### 7. `entity_embeddings`

Purpose:

- semantic retrieval across companies, people, and prior lead summaries

Suggested fields:

- `id`
- `entity_type`
- `entity_id`
- `campaign_id`
- `text_payload`
- `embedding`
- `created_at`

If using PostgreSQL, use `pgvector`.

## Memory Strategy

### What The Agent Should Remember

The agent should build memory in three layers.

#### A. Factual Memory

Examples:

- company industry and size
- person title and seniority
- prior enrichment findings
- prior campaign participation

This is safe to write automatically.

#### B. Decision Memory

Examples:

- prior scores
- prior qualification reasons
- prior disqualification reasons
- final routed decision
- human override decisions

This is safe to write automatically if the source is explicit.

#### C. Outcome Memory

Examples:

- replied
- bounced
- booked meeting
- no fit
- competitor
- poor timing

This is the most valuable long-term signal and should be tied to human or system-of-record events.

### What The Agent Must Not Treat As Memory

The agent must not silently convert these into truth:

- speculative inferences
- one-off prompt phrasing
- accidental false positives
- unapproved policy changes
- operator gossip or casual notes without source attribution

## Retrieval Strategy

Retrieval should happen before profiling, before campaign drafting, and optionally before Stage 1 scoring.

### Retrieval Inputs

- company name
- domain
- person name
- LinkedIn URL
- campaign id
- free-text operator query

### Retrieval Outputs

- exact entity matches
- similar companies
- similar people
- prior decisions for same entity
- examples of similar accepted or rejected leads

### Retrieval Injection Policy

Use retrieval carefully.

Recommended usage:

- inject compact factual memory into a profile task
- inject similar accepted or rejected examples into an analyst summary
- inject prior human review outcomes into campaign evaluation

Avoid:

- stuffing large memory dumps into Stage 2 prompts
- allowing noisy memory to override explicit current evidence

The safest pattern is:

1. retrieve memory
2. summarize memory into compact factual context
3. keep current lead evidence primary
4. use memory as supporting evidence only

## Guardrail Model

### Guardrail Category 1. Deterministic Score Enforcement

Keep and strengthen the current Python controls:

- clamp sub-scores
- re-sum total score
- re-derive decision from thresholds
- preserve hard disqualifier override
- preserve low-signal override

Additional recommended controls:

- require explicit reason coverage for high scores
- require minimum evidence count for `qualified`
- reject `qualified` if too many critical unknowns remain
- enforce campaign-specific required persona or segment checks

### Guardrail Category 2. Policy Immutability

No live autonomous mutation of:

- thresholds
- weights
- disqualification rules
- routing logic
- campaign files

Any change must go through:

1. draft
2. benchmark evaluation
3. human approval
4. activation

### Guardrail Category 3. Memory Hygiene

Require:

- source attribution
- timestamps
- entity linkage
- confidence metadata for inferred summaries

Do not let unverified memory become first-class policy.

### Guardrail Category 4. Campaign Isolation

Each campaign must preserve separate:

- thresholds
- templates
- memory summaries
- evaluation reports
- activation status

This prevents leakage of Seedance assumptions into future products.

## Agent API Surface

The current FastAPI app is the right place to expose explicit agent actions.

Add an `agent` API surface or MCP-compatible command surface with actions like:

### Run Actions

- `POST /api/agent/runs`
  - start a batch run from uploaded CSV
- `GET /api/agent/runs/{run_id}`
  - get run summary
- `GET /api/agent/runs/{run_id}/results`
  - get scored leads
- `GET /api/agent/runs/{run_id}/artifacts/{lead_id}`
  - get full artifacts

### Profile Actions

- `POST /api/agent/profile/company`
  - build or retrieve a company profile
- `POST /api/agent/profile/person`
  - build or retrieve a person profile
- `POST /api/agent/profile/fit`
  - score a company/person pair against a campaign

### Memory Actions

- `GET /api/agent/memory/company/{domain}`
  - retrieve company history
- `GET /api/agent/memory/person`
  - retrieve person history
- `POST /api/agent/memory/search`
  - semantic search over prior entities and decisions

### Campaign Actions

- `POST /api/agent/campaigns/draft`
  - create a draft campaign pack
- `POST /api/agent/campaigns/{campaign_id}/evaluate`
  - run benchmark evaluation
- `POST /api/agent/campaigns/{campaign_id}/activate`
  - activate only after approval
- `GET /api/agent/campaigns/{campaign_id}/versions`
  - list campaign history

### Review Actions

- `POST /api/agent/review-feedback`
  - attach human feedback to a lead or entity
- `GET /api/agent/review-feedback`
  - query labeled examples for evaluation

## Backend Changes By Module

### `db/store.py`

Add:

- new memory tables
- new campaign version tables
- retrieval helpers
- entity upsert methods
- feedback write methods
- vector search support if using `pgvector`

### `ui/server.py`

Add:

- agent-focused profile endpoints
- run result and artifact retrieval endpoints
- review feedback endpoints
- campaign draft and evaluation endpoints

### `main.py`

Keep the core orchestration intact.

Potential additions:

- optional retrieval context pre-step
- write-through into memory tables after each lead
- policy version and campaign version tagging per run

### `campaigns/loader.py`

Add support for:

- campaign version loading
- draft versus active campaign selection
- evaluation-only campaign contexts

### `output/csv_writer.py`

Optional additions:

- include `policy_version`
- include `campaign_version`
- include `entity_id` references

## Scoring Consistency Enhancements

To make scoring even more consistent, implement these next.

### 1. Qualification Preconditions

Before routing to `qualified`, require:

- minimum observed evidence count
- acceptable unknown count
- no unresolved target-fit ambiguity

### 2. Explanation Coverage Checks

Require:

- every high sub-score to be backed by at least one explicit reason
- disqualification overrides to map to a known rule category

### 3. Benchmark Set

Build a gold dataset of:

- clearly qualified examples
- borderline review cases
- clearly disqualified cases
- repeated companies and repeated personas

Use this to compare any campaign or prompt change before rollout.

### 4. Drift Detection

Add monitoring for:

- qualified rate change over time
- score distribution drift
- segment distribution drift
- reviewer override frequency
- false-positive clusters

## Campaign Lifecycle

### Stage 1. Draft

The agent drafts a new campaign pack from product and ICP inputs.

Outputs:

- `config.json`
- `product.md`
- `icp.md`
- `signals.md`
- `disqualification.md`
- `qualification.md`

Status:

- `draft`

### Stage 2. Candidate Evaluation

Run the draft against:

- historical labeled leads
- curated test CSVs
- known edge cases

Produce:

- precision and recall proxy metrics
- false-positive review
- false-negative review
- score distribution
- top failure modes

Status:

- `candidate`

### Stage 3. Human Approval

Required approver reviews:

- segment definitions
- persona rules
- hard disqualifiers
- thresholds
- score weights

Status:

- `active`

### Stage 4. Active Monitoring

Track:

- run-level outcomes
- reviewer corrections
- outreach results
- drift metrics

## Learning Loop

The correct learning loop is:

1. process leads
2. store artifacts and memory
3. collect human review and downstream outcomes
4. retrieve patterns and summarize failure clusters
5. draft policy improvements
6. benchmark them offline
7. approve and activate intentionally

This makes the system improve over time without becoming unstable.

## Deployment Recommendation

### Short Term

Deploy the agent as a skill-guided operator over the existing backend.

Good for:

- local use
- fast iteration
- safe workflow refinement

### Medium Term

Expose the backend as explicit agent actions via FastAPI or MCP.

Good for:

- non-local operation
- multi-user workflows
- cleaner agent integration

### Long Term

Run a dedicated memory-backed operator service with:

- API actions
- retrieval
- benchmark evaluation
- campaign lifecycle controls
- audit logs

## Recommended Implementation Phases

### Phase 1. Memory Foundation

Build:

- `company_profiles`
- `person_profiles`
- `lead_memory`
- `review_feedback`
- entity upsert and retrieval APIs

Outcome:

- the system remembers companies, people, and prior decisions

### Phase 2. Profile Workflows

Build:

- company profile endpoint
- person profile endpoint
- fit analysis endpoint

Outcome:

- the agent can answer "tell me about this company/person and whether they fit"

### Phase 3. Evaluation Harness

Build:

- benchmark datasets
- candidate campaign evaluation reports
- policy version tagging

Outcome:

- scoring changes become measurable and reviewable

### Phase 4. Campaign Lifecycle

Build:

- draft campaign creation
- candidate evaluation workflow
- approval and activation flow

Outcome:

- new products can be onboarded safely

### Phase 5. Agent API Or MCP

Build:

- explicit agent actions
- skill or MCP integration
- operator workflows across runs, profiles, memory, and campaigns

Outcome:

- the system becomes truly agentic without losing backend discipline

## Non-Goals

This blueprint does not recommend:

- replacing the two-stage scoring kernel with a single black-box agent call
- allowing autonomous prompt mutation in production
- treating semantic memory as scoring truth
- mixing email automation policy into scoring policy
- deploying unversioned campaign logic

## Final Recommendation

The best approach for this repository is:

1. keep the current backend as the deterministic scoring kernel
2. add a persistent memory layer for companies, people, leads, and outcomes
3. expose explicit agent actions for runs, profiles, retrieval, and campaigns
4. enforce versioned policy and approval gates for any scoring changes
5. use memory for retrieval and context, not silent policy drift

That gives you an agent that becomes more useful after 50 runs, can answer profile questions, can support new product campaigns, and still remains consistent enough to trust.
