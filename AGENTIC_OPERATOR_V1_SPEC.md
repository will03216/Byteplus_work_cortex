# Agentic Operator V1 Spec

## Purpose

This document turns the higher-level blueprint into a concrete v1 implementation spec for this repository.

It covers:

- database schema additions
- migration strategy
- API contracts
- backend service responsibilities
- implementation order
- rollout guardrails

The v1 objective is not to fully automate everything. The v1 objective is to establish a safe memory-backed operator foundation while preserving consistent lead scoring.

## V1 Scope

V1 includes:

1. persistent company memory
2. persistent person memory
3. append-only lead memory per run
4. human review feedback capture
5. profile APIs for company and person
6. history and retrieval APIs
7. version tagging for runs
8. no autonomous policy mutation

V1 does not include:

- automatic activation of draft campaigns
- autonomous threshold changes
- autonomous score weight changes
- autonomous prompt rewrites
- production-grade vector search requirement on day one

## Current Baseline

The current repo already provides:

- `runs`, `leads`, `campaigns`, `campaign_settings`, `exclusions`, `email_templates`, `skill_files`
- run orchestration in `main.py`
- DB persistence in `db/store.py`
- FastAPI routes in `ui/server.py`

This v1 spec adds a memory layer and an agent-facing API surface without disrupting the scoring kernel.

## V1 Design Principles

### 1. Deterministic Scoring Stays Authoritative

The scoring path remains:

```text
input -> normalize -> enrich -> Stage 1 -> Stage 2 -> Python route -> persist
```

Nothing in v1 changes:

- score clamping
- routing thresholds
- hard disqualifier overrides
- low-signal override logic

### 2. Memory Is Additive

Memory augments future workflows, but does not directly redefine policy.

Memory can be used to:

- recall known companies and people
- retrieve prior evidence and outcomes
- answer profile questions
- improve human review efficiency

Memory cannot:

- override explicit current evidence
- silently change scoring rules

### 3. Campaign Policy Is Versioned

Every run should eventually be attributable to:

- `campaign_id`
- `campaign_version`
- `policy_version`

V1 will add these tags even if policy activation remains manual.

## Database Schema Additions

V1 adds six new tables and two optional columns.

### Table 1. `company_profiles`

Purpose:

- canonical memory record for a company

Suggested DDL:

```sql
CREATE TABLE IF NOT EXISTS company_profiles (
    id                      BIGSERIAL PRIMARY KEY,
    canonical_domain        TEXT,
    company_name            TEXT NOT NULL,
    normalized_company_name TEXT NOT NULL,
    latest_structured_profile JSONB NOT NULL DEFAULT '{}',
    latest_summary          TEXT NOT NULL DEFAULT '',
    latest_fit_by_campaign  JSONB NOT NULL DEFAULT '{}',
    first_seen_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    times_seen              INT NOT NULL DEFAULT 1,
    last_run_id             TEXT REFERENCES runs(run_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_company_profiles_domain
    ON company_profiles(canonical_domain)
    WHERE canonical_domain IS NOT NULL AND canonical_domain <> '';

CREATE INDEX IF NOT EXISTS idx_company_profiles_norm_name
    ON company_profiles(normalized_company_name);
```

Notes:

- prefer `canonical_domain` as primary lookup key
- fall back to normalized company name when domain is missing

### Table 2. `person_profiles`

Purpose:

- canonical memory record for a person/contact

Suggested DDL:

```sql
CREATE TABLE IF NOT EXISTS person_profiles (
    id                      BIGSERIAL PRIMARY KEY,
    person_name             TEXT NOT NULL,
    normalized_person_name  TEXT NOT NULL,
    linkedin_url            TEXT,
    company_profile_id      BIGINT REFERENCES company_profiles(id) ON DELETE SET NULL,
    latest_title            TEXT NOT NULL DEFAULT '',
    latest_summary          TEXT NOT NULL DEFAULT '',
    latest_fit_by_campaign  JSONB NOT NULL DEFAULT '{}',
    first_seen_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    times_seen              INT NOT NULL DEFAULT 1,
    last_run_id             TEXT REFERENCES runs(run_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_person_profiles_linkedin
    ON person_profiles(linkedin_url)
    WHERE linkedin_url IS NOT NULL AND linkedin_url <> '';

CREATE INDEX IF NOT EXISTS idx_person_profiles_norm_name
    ON person_profiles(normalized_person_name);
```

Notes:

- LinkedIn URL is the best unique key when available
- otherwise use normalized name plus linked company in application logic

### Table 3. `lead_memory`

Purpose:

- append-only scoring history for each processed lead instance

Suggested DDL:

```sql
CREATE TABLE IF NOT EXISTS lead_memory (
    id                   BIGSERIAL PRIMARY KEY,
    run_id               TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    lead_id              TEXT NOT NULL,
    campaign_id          TEXT NOT NULL REFERENCES campaigns(id),
    campaign_version     TEXT NOT NULL DEFAULT 'v1',
    policy_version       TEXT NOT NULL DEFAULT 'v1',
    company_profile_id   BIGINT REFERENCES company_profiles(id) ON DELETE SET NULL,
    person_profile_id    BIGINT REFERENCES person_profiles(id) ON DELETE SET NULL,
    raw_input            JSONB NOT NULL DEFAULT '{}',
    enrichment_snapshot  JSONB NOT NULL DEFAULT '{}',
    evidence_snapshot    JSONB NOT NULL DEFAULT '{}',
    decision_snapshot    JSONB NOT NULL DEFAULT '{}',
    routing              TEXT NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_lead_memory_run_id ON lead_memory(run_id);
CREATE INDEX IF NOT EXISTS idx_lead_memory_campaign_id ON lead_memory(campaign_id);
CREATE INDEX IF NOT EXISTS idx_lead_memory_company_profile_id ON lead_memory(company_profile_id);
CREATE INDEX IF NOT EXISTS idx_lead_memory_person_profile_id ON lead_memory(person_profile_id);
CREATE INDEX IF NOT EXISTS idx_lead_memory_lead_id ON lead_memory(lead_id);
```

Notes:

- this is not a replacement for `leads`
- `leads` remains the run result table
- `lead_memory` becomes the full audit/memory table

### Table 4. `review_feedback`

Purpose:

- capture human review and downstream outcomes

Suggested DDL:

```sql
CREATE TABLE IF NOT EXISTS review_feedback (
    id                  BIGSERIAL PRIMARY KEY,
    run_id              TEXT REFERENCES runs(run_id) ON DELETE CASCADE,
    lead_id             TEXT NOT NULL,
    campaign_id         TEXT NOT NULL REFERENCES campaigns(id),
    company_profile_id  BIGINT REFERENCES company_profiles(id) ON DELETE SET NULL,
    person_profile_id   BIGINT REFERENCES person_profiles(id) ON DELETE SET NULL,
    feedback_type       TEXT NOT NULL,
    feedback_reason     TEXT NOT NULL DEFAULT '',
    corrected_decision  TEXT,
    fit_outcome         TEXT,
    owner               TEXT NOT NULL DEFAULT '',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_review_feedback_campaign_id ON review_feedback(campaign_id);
CREATE INDEX IF NOT EXISTS idx_review_feedback_run_id ON review_feedback(run_id);
CREATE INDEX IF NOT EXISTS idx_review_feedback_lead_id ON review_feedback(lead_id);
```

Accepted `feedback_type` values for v1:

- `false_positive`
- `false_negative`
- `wrong_persona`
- `wrong_company`
- `duplicate`
- `good_fit_bad_timing`
- `won`
- `lost`
- `manual_override`

### Table 5. `campaign_versions`

Purpose:

- preserve draft and active campaign content versions

Suggested DDL:

```sql
CREATE TABLE IF NOT EXISTS campaign_versions (
    id                   BIGSERIAL PRIMARY KEY,
    campaign_id          TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    version              TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'draft',
    config_json          TEXT NOT NULL DEFAULT '',
    product_md           TEXT NOT NULL DEFAULT '',
    icp_md               TEXT NOT NULL DEFAULT '',
    signals_md           TEXT NOT NULL DEFAULT '',
    disqualification_md  TEXT NOT NULL DEFAULT '',
    qualification_md     TEXT NOT NULL DEFAULT '',
    created_by           TEXT NOT NULL DEFAULT '',
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    approved_by          TEXT,
    approved_at          TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_campaign_versions_unique
    ON campaign_versions(campaign_id, version);
```

Accepted `status` values:

- `draft`
- `candidate`
- `active`
- `archived`

### Table 6. `policy_versions`

Purpose:

- preserve scoring and routing control state

Suggested DDL:

```sql
CREATE TABLE IF NOT EXISTS policy_versions (
    id                    BIGSERIAL PRIMARY KEY,
    campaign_id           TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    policy_version        TEXT NOT NULL,
    score_weights         JSONB NOT NULL DEFAULT '{}',
    qualified_threshold   INT NOT NULL,
    disqualified_threshold INT NOT NULL,
    routing_rules         JSONB NOT NULL DEFAULT '{}',
    status                TEXT NOT NULL DEFAULT 'draft',
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    approved_at           TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_policy_versions_unique
    ON policy_versions(campaign_id, policy_version);
```

### Optional Column Additions

Add to `runs`:

```sql
ALTER TABLE runs ADD COLUMN IF NOT EXISTS campaign_version TEXT DEFAULT 'v1';
ALTER TABLE runs ADD COLUMN IF NOT EXISTS policy_version TEXT DEFAULT 'v1';
```

These columns allow every run to be traced to the governing campaign and policy version.

## Migration Strategy

Do not try to fold all of this into `create_tables()` only.

Recommended migration plan:

### Migration 002. Memory Tables

Create:

- `company_profiles`
- `person_profiles`
- `lead_memory`
- `review_feedback`
- `runs.campaign_version`
- `runs.policy_version`

### Migration 003. Version Tables

Create:

- `campaign_versions`
- `policy_versions`

Backfill:

- current campaign files from `skill_files`
- current thresholds from `campaign_settings`

### Migration 004. Optional Vector Support

If using `pgvector`, add:

- extension creation
- embeddings table
- embedding indexes

This migration should be optional and environment-gated.

## Backfill Rules

### Company Profile Backfill

For each existing lead:

- derive `canonical_domain` from `company_website` when possible
- otherwise use normalized company name
- create or update `company_profiles`

### Person Profile Backfill

For each existing lead:

- use `linkedin_url` if present
- otherwise use normalized person name plus company link
- create or update `person_profiles`

### Lead Memory Backfill

For each existing `leads` row:

- create one `lead_memory` record
- reuse `evidence`, score dimensions, and decision fields already stored
- use placeholder `campaign_version = 'backfill-v1'`
- use placeholder `policy_version = 'backfill-v1'`

## Store Layer Additions

Add the following functions to `db/store.py`.

### Company Profile Functions

- `normalize_company_key(company_name: str, domain: str) -> dict`
- `upsert_company_profile(...) -> dict`
- `get_company_profile_by_domain(...) -> Optional[dict]`
- `search_company_profiles(...) -> list[dict]`

### Person Profile Functions

- `normalize_person_key(person_name: str, linkedin_url: str) -> dict`
- `upsert_person_profile(...) -> dict`
- `get_person_profile(...) -> Optional[dict]`
- `search_person_profiles(...) -> list[dict]`

### Lead Memory Functions

- `insert_lead_memory(...) -> dict`
- `get_lead_memory_for_company(...) -> list[dict]`
- `get_lead_memory_for_person(...) -> list[dict]`
- `get_lead_memory_for_run(...) -> list[dict]`

### Review Feedback Functions

- `add_review_feedback(...) -> dict`
- `list_review_feedback(...) -> list[dict]`
- `get_feedback_examples(...) -> list[dict]`

### Campaign/Policy Version Functions

- `create_campaign_version(...) -> dict`
- `list_campaign_versions(...) -> list[dict]`
- `activate_campaign_version(...) -> dict`
- `create_policy_version(...) -> dict`
- `list_policy_versions(...) -> list[dict]`

## Write Path Changes

### Current Write Path

Today:

1. run batch
2. write CSV and artifacts
3. import summarized leads into `leads`

### V1 Write Path

Add these steps after a run finishes:

1. import summarized leads into `leads`
2. upsert company profile for each lead
3. upsert person profile for each lead
4. insert one `lead_memory` row per lead
5. update company/person latest fit summary

Important:

- `lead_memory` should use artifact data when available
- if artifacts are not available in DB-backed cleanup flow, the importer should reconstruct the best available snapshot from lead records

## API Surface V1

Expose a new API namespace under `/api/agent`.

This keeps the agent-oriented endpoints separate from the current UI routes.

## Endpoint Group 1. Run Inspection

### `GET /api/agent/runs/{run_id}`

Purpose:

- fetch run metadata with version tags

Response:

```json
{
  "run_id": "uuid",
  "campaign_id": "seedance",
  "campaign_version": "v1",
  "policy_version": "v1",
  "status": "done",
  "lead_count": 100,
  "n_qualified": 18,
  "n_review": 26,
  "n_disqualified": 56,
  "created_at": "2026-05-11T10:00:00Z"
}
```

### `GET /api/agent/runs/{run_id}/results`

Purpose:

- fetch scored lead summaries

Query params:

- `decision`
- `min_score`
- `max_score`
- `limit`
- `offset`

### `GET /api/agent/runs/{run_id}/artifacts/{lead_id}`

Purpose:

- return the best available full record for one lead

Response:

```json
{
  "lead_id": "abc123",
  "raw_input": {},
  "enrichment_snapshot": {},
  "evidence_snapshot": {},
  "decision_snapshot": {},
  "routing": "qualified"
}
```

## Endpoint Group 2. Company Profile

### `POST /api/agent/profile/company`

Purpose:

- build or retrieve a company profile

Request:

```json
{
  "campaign_id": "seedance",
  "company_name": "Example Studio",
  "domain": "example.com",
  "refresh_enrichment": false
}
```

Response:

```json
{
  "company_profile": {
    "id": 10,
    "company_name": "Example Studio",
    "canonical_domain": "example.com",
    "latest_summary": "Film production company with repeated prior positive fit signals."
  },
  "history": {
    "times_seen": 4,
    "runs": 3,
    "qualified_count": 2,
    "review_count": 1,
    "disqualified_count": 1
  },
  "fit_summary": {
    "campaign_id": "seedance",
    "last_known_fit": "qualified",
    "last_score": 78
  }
}
```

### `GET /api/agent/profile/company/{domain}`

Purpose:

- fetch existing company profile by canonical domain

## Endpoint Group 3. Person Profile

### `POST /api/agent/profile/person`

Purpose:

- build or retrieve a person profile

Request:

```json
{
  "campaign_id": "seedance",
  "person_name": "Jane Doe",
  "linkedin_url": "https://linkedin.com/in/jane-doe",
  "company_domain": "example.com"
}
```

Response:

```json
{
  "person_profile": {
    "id": 23,
    "person_name": "Jane Doe",
    "latest_title": "Head of Production",
    "latest_summary": "Senior buyer-type persona with repeated creative workflow relevance."
  },
  "history": {
    "times_seen": 2,
    "qualified_count": 1,
    "review_count": 1,
    "disqualified_count": 0
  },
  "fit_summary": {
    "campaign_id": "seedance",
    "last_known_fit": "qualified",
    "last_score": 81
  }
}
```

## Endpoint Group 4. Fit Analysis

### `POST /api/agent/profile/fit`

Purpose:

- answer "is this company/person a fit for this campaign?"

Request:

```json
{
  "campaign_id": "seedance",
  "company_name": "Example Studio",
  "domain": "example.com",
  "person_name": "Jane Doe",
  "linkedin_url": "https://linkedin.com/in/jane-doe",
  "use_memory": true,
  "refresh_enrichment": false
}
```

Response:

```json
{
  "campaign_id": "seedance",
  "verdict": "qualified",
  "confidence": "medium",
  "score": 78,
  "why_for": [
    "Strong segment fit from prior and current company profile.",
    "Senior buyer persona aligned with production workflow ownership."
  ],
  "why_against": [
    "Limited current timing signal."
  ],
  "memory_used": [
    "2 prior company records",
    "1 prior person record"
  ],
  "recommended_next_action": "review_for_outreach"
}
```

Implementation note:

- this endpoint does not need to call `main.py`
- it can run a lighter-weight entity evaluation flow using current retrieval, enrichment, and the same stage logic

## Endpoint Group 5. Review Feedback

### `POST /api/agent/review-feedback`

Purpose:

- attach human feedback to a lead, company, or person

Request:

```json
{
  "run_id": "uuid",
  "lead_id": "abc123",
  "campaign_id": "seedance",
  "feedback_type": "false_positive",
  "feedback_reason": "Right title, wrong industry. No real production workflow.",
  "corrected_decision": "disqualified",
  "fit_outcome": "not_fit",
  "owner": "antti"
}
```

Response:

```json
{
  "ok": true,
  "feedback_id": 45
}
```

### `GET /api/agent/review-feedback`

Query params:

- `campaign_id`
- `feedback_type`
- `limit`
- `offset`

## Endpoint Group 6. Campaign Versions

### `POST /api/agent/campaigns/{campaign_id}/versions`

Purpose:

- create a draft campaign version

Request:

```json
{
  "version": "v2-draft-01",
  "config_json": "{...}",
  "product_md": "...",
  "icp_md": "...",
  "signals_md": "...",
  "disqualification_md": "...",
  "qualification_md": "...",
  "created_by": "agent"
}
```

### `GET /api/agent/campaigns/{campaign_id}/versions`

Purpose:

- list draft, candidate, and active campaign versions

### `POST /api/agent/campaigns/{campaign_id}/versions/{version}/activate`

Purpose:

- activate a previously approved campaign version

Guardrail:

- reject activation unless request includes `approved_by`

## Validation Rules

Apply these rules in v1.

### Input Validation

- reject empty `campaign_id`
- reject invalid `feedback_type`
- reject activation for nonexistent version
- reject `disqualified_threshold >= qualified_threshold`

### Memory Validation

- company profile lookup prefers domain when present
- person lookup prefers LinkedIn URL when present
- no duplicate exact-memory writes for the same run and lead

### Policy Validation

- only one active campaign version per `campaign_id`
- only one active policy version per `campaign_id`
- runs must never reference null `campaign_id`

## File-by-File Implementation Plan

### Step 1. Migrations

Add:

- `db/migrations/002_add_agent_memory.sql`
- `db/migrations/003_add_campaign_policy_versions.sql`

### Step 2. `db/store.py`

Implement:

- DDL updates in `create_tables()`
- new CRUD methods for profiles, memory, versions, and feedback
- row mappers for company/person/profile views

### Step 3. `ui/server.py`

Implement:

- `/api/agent/...` endpoints
- request validation helpers
- response serializers

### Step 4. Run Import Path

Update:

- `_import_to_db()` in `ui/server.py`

Add:

- profile upsert after lead import
- `lead_memory` writes

### Step 5. Run Version Tagging

Update:

- `insert_run()` in `db/store.py`
- batch run creation flow in `ui/server.py`

Add:

- default `campaign_version`
- default `policy_version`

### Step 6. Profile Assembly

Add helper functions for:

- company rollup
- person rollup
- fit history summary
- recent evidence summary

## Rollout Strategy

### Rollout A. Schema + Passive Memory

Ship:

- tables
- import writes

Do not expose new agent endpoints yet.

Goal:

- accumulate memory safely

### Rollout B. Read APIs

Ship:

- profile endpoints
- history endpoints
- feedback endpoints

Goal:

- make memory usable without changing scoring

### Rollout C. Campaign Version APIs

Ship:

- campaign version create/list/activate

Goal:

- add safer campaign evolution

## Testing Strategy

### DB Tests

Validate:

- company upsert idempotency
- person upsert idempotency
- lead memory append behavior
- review feedback write and query behavior

### API Tests

Validate:

- company profile endpoint response shape
- person profile endpoint response shape
- fit endpoint response shape
- campaign activation guardrails

### Regression Tests

Validate:

- existing run ingestion still works
- existing email preview/export still works
- existing campaign listing still works

## Recommended Defaults

Use these defaults in v1:

- `campaign_version = "v1"`
- `policy_version = "v1"`
- `status = "draft"` for new campaign versions
- no vector search until enough memory exists to justify it

## Immediate Next Build Recommendation

Implement this in the following exact order:

1. migration 002
2. `db/store.py` profile and memory helpers
3. `_import_to_db()` memory write-through
4. company/person profile read APIs
5. review feedback APIs
6. migration 003
7. campaign version APIs

This sequence gives you useful memory fast, while keeping risk to scoring consistency very low.
