# Account Intelligence Layer — Design Document

## Overview

This document captures the problem statement, architecture decisions, GTM strategy analysis, and implementation plan for evolving Cortex from a batch lead qualification engine into a full account intelligence platform. It covers the entity memory layer, agentic enrichment pipeline, meeting intelligence capability, and multi-surface access design (UI, MCP, Skill).

***

## Problem Statement

The current Cortex system handles top-of-funnel lead qualification well: it ingests CSVs, scores leads across a five-factor model, and pushes qualified leads to Instantly for email outreach. The system is consistent, auditable, and deterministic.

The gap is everything that happens after a lead qualifies.

**AEs face three problems before every sales call:**

1. **Research time**: 30-60 minutes per account manually pulling funding news, LinkedIn profiles, company context, and AI investment signals from disparate sources.
2. **No institutional memory**: each AE researches from scratch. There is no shared record of what Cortex has already learned about a company across prior runs, which signals it found, or what happened when someone emailed them.
3. **No meeting-ready output**: the existing pipeline produces CSVs and scoring artifacts optimized for ops review, not for an AE walking into a call.

What is needed is a persistent account intelligence layer that captures everything the system learns about a company or person across all runs, enriches it proactively with external signals, and surfaces it on demand in a format AEs can actually use.

***

## Current System Capabilities

| Capability                                               | Status      |
| -------------------------------------------------------- | ----------- |
| Batch lead scoring (CSV → qualified/review/disqualified) | Done        |
| Two-stage LLM pipeline (evidence extraction + decision)  | Done        |
| Apollo.io enrichment with DB caching                     | Done        |
| Website + LinkedIn scraping                              | Done        |
| Campaign system (Seedance, Kling, extensible)            | Done        |
| FastAPI web UI with run history                          | Done        |
| PostgreSQL persistence                                   | Done        |
| Instantly.ai email outreach integration                  | Done        |
| Claude Code skill for standalone operation               | Done        |
| Persistent entity memory (company/person profiles)       | **Missing** |
| Signal timeline per company                              | **Missing** |
| Proactive signal refresh                                 | **Missing** |
| Outcome memory (reply/meeting/won/lost)                  | **Missing** |
| Meeting prep deck generation                             | **Missing** |
| MCP server for conversational AE access                  | **Missing** |

***

## Target Capability

The system should support three personas with different needs:

**AE / SA** — "I have a call with Acme Corp in 2 hours. Give me everything."

- Company snapshot: size, funding, tech stack, industry
- Key contacts at the account and their decision authority
- Recent signals: funding news, AI hiring, product launches
- Cortex fit score and evidence summary for the relevant campaign
- Recommended talking points and angle
- Risk flags and open unknowns
- Prior interaction history

**Ops / SDR** — "Which accounts from last week's run are heating up?"

- Pipeline view across all scored accounts
- Signal activity feed (who raised money, who is hiring AI roles)
- Hot-lead flags triggered by new signals
- Human feedback capture (override scores, mark outcomes)

**Campaign Manager** — "We want to add a Google Veo campaign. What would the ICP look like?"

- Campaign authoring from product + ICP inputs
- Benchmark evaluation against historical labeled leads
- Shadow scoring before activation

***

## GTM Strategy Assessment

The approach is architecturally sound for the following reasons:

**The scoring kernel already extracts the right data.** Stage 1 produces structured evidence — signals, use cases, unknowns, hard disqualifiers — per lead. Stage 2 produces a scored decision with sub-scores and reasoning. This is the foundation of a meeting deck. The data exists; it is just not persisted at the entity level.

**Apollo already runs on every lead and caches to DB.** The `apollo_cache` table holds company enrichment data keyed by domain. This is the seed for a company entity store. The migration is incremental, not a rebuild.

**The campaign system already encodes what "good fit" looks like per product.** This context should flow into meeting prep output, not just into scoring. An AE meeting an account for Seedance needs different talking points than one meeting the same account for Kling.

**What the current design does not handle:**

- Signals are enriched once at ingestion and discarded after scoring. There is no signal timeline per company. A Series B raised three months ago disappears when the profile is re-enriched.
- Instantly reply and bounce events never flow back into the system. The highest-value learning signal — whether qualified leads actually respond — is completely unused.
- There is no scheduled refresh. Apollo data ages. The system has no mechanism to re-enrich stale profiles.
- Entity identity is not resolved. "ByteDance", "Bytedance Ltd", and "bytedance.com" can produce three separate records.

***

## Architecture

### Four Layers

```
┌────────────────────────────────────────────────────────┐
│  Layer 2 — Agent Orchestration                         │
│  Claude agent (Claude.ai desktop or Claude Code)       │
│  Fixed tool set exposed via MCP server                 │
│  Reads and acts; no policy authority                   │
└────────────────────────┬───────────────────────────────┘
                         │ MCP tool calls
┌────────────────────────▼───────────────────────────────┐
│  Layer 1 — Deterministic Scoring Kernel                │
│  main.py / stage1 / stage2 / router                    │
│  FastAPI backend (existing)                            │
│  Agent API endpoints (new)                             │
└────────────────────────┬───────────────────────────────┘
                         │ reads / writes
┌────────────────────────▼───────────────────────────────┐
│  Layer 3 — Memory and Retrieval                        │
│  PostgreSQL + pgvector                                 │
│  company_profiles, company_signals, person_profiles    │
│  lead_memory, review_feedback, entity_embeddings       │
└────────────────────────┬───────────────────────────────┘
                         │ enforces
┌────────────────────────▼───────────────────────────────┐
│  Layer 4 — Policy and Evaluation                       │
│  campaign_versions, policy locked in campaign files    │
│  Human approval required for any threshold change      │
└────────────────────────────────────────────────────────┘
```

**Core invariant:** **`memory may evolve automatically; policy may not`**

The system can write new company memories, update signal timelines, and summarize historical patterns automatically. It cannot silently change scoring thresholds, routing rules, disqualifiers, or campaign prompt content.

### Operating Modes

**Mode A — Batch Campaign Processing**
Ingest CSV → score all leads → write to entity store → push qualified to Instantly.
Existing behavior, extended with entity upsert after each lead.

**Mode B — Entity Profiling**
Query: "Tell me about Acme Corp and whether they fit Seedance."
Returns: company profile, person profiles, historical scores, fit summary, open unknowns.

**Mode C — Campaign Authoring**
Input: product description, ICP notes, example good/bad leads.
Output: draft campaign pack, benchmark evaluation report, approval-ready activation.

**Mode D — Meeting Intelligence**
Input: company domain, attendee names, meeting context, campaign.
Output: structured briefing deck with talking points, signal timeline, contact map, risk flags.
This is distinct from Mode B: it is time-sensitive, audience-specific, and formatted for a human reading a brief before a call, not for the system querying fit.

***

## Data Model

### New Tables (extend existing schema)

#### `company_profiles`

One memory record per company. `canonical_domain` is the primary key.

```sql
CREATE TABLE IF NOT EXISTS company_profiles (
    canonical_domain          TEXT        PRIMARY KEY,
    company_name              TEXT        NOT NULL,
    normalized_company_name   TEXT        NOT NULL,
    latest_structured_profile JSONB       NOT NULL DEFAULT '{}',
    latest_summary            TEXT,
    latest_fit_by_campaign    JSONB       NOT NULL DEFAULT '{}',
    is_hot                    BOOLEAN     NOT NULL DEFAULT FALSE,
    first_seen_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_enriched_at          TIMESTAMPTZ,
    times_seen                INT         NOT NULL DEFAULT 1,
    last_run_id               TEXT        REFERENCES runs(run_id)
);
```

`latest_structured_profile` stores only the current enrichment snapshot. Historical events go into `company_signals`, never into this column.

#### `company_signals`

Append-only time-series of events per company. Write-once. Never update or delete rows.

```sql
CREATE TABLE IF NOT EXISTS company_signals (
    id                SERIAL      PRIMARY KEY,
    domain            TEXT        NOT NULL REFERENCES company_profiles(canonical_domain),
    signal_type       TEXT        NOT NULL,
    headline          TEXT        NOT NULL,
    source_url        TEXT,
    source            TEXT        NOT NULL,
    occurred_at       TIMESTAMPTZ NOT NULL,
    captured_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    enrichment_run_id TEXT
);
```

Signal types: `funding_round`, `ai_hiring`, `product_launch`, `news_mention`, `job_posting`, `tech_stack_change`, `outreach_reply`, `meeting_booked`

Sources: `apollo`, `tavily`, `instantly_webhook`, `manual`

This table is the source of truth for meeting prep and signal history. A funding round from three months ago must not be lost when the profile is re-enriched.

#### `person_profiles`

One memory record per contact. `linkedin_url` is the stable primary key where available.

```sql
CREATE TABLE IF NOT EXISTS person_profiles (
    id                      SERIAL      PRIMARY KEY,
    linkedin_url            TEXT        UNIQUE,
    person_name             TEXT        NOT NULL,
    normalized_person_name  TEXT        NOT NULL,
    canonical_domain        TEXT        REFERENCES company_profiles(canonical_domain),
    latest_title            TEXT,
    latest_summary          TEXT,
    latest_fit_by_campaign  JSONB       NOT NULL DEFAULT '{}',
    first_seen_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    times_seen              INT         NOT NULL DEFAULT 1,
    last_run_id             TEXT        REFERENCES runs(run_id)
);
```

#### `lead_memory`

Immutable, append-only scoring history. One row per lead per run.

```sql
CREATE TABLE IF NOT EXISTS lead_memory (
    id                  SERIAL      PRIMARY KEY,
    run_id              TEXT        NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    lead_id             TEXT        NOT NULL,
    campaign_id         TEXT        REFERENCES campaigns(id),
    canonical_domain    TEXT        REFERENCES company_profiles(canonical_domain),
    person_profile_id   INT         REFERENCES person_profiles(id),
    raw_input           JSONB       NOT NULL DEFAULT '{}',
    enrichment_snapshot JSONB       NOT NULL DEFAULT '{}',
    evidence_snapshot   JSONB       NOT NULL DEFAULT '{}',
    decision_snapshot   JSONB       NOT NULL DEFAULT '{}',
    routing             TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

#### `review_feedback`

Human corrections and downstream outcomes. Writable from UI, API, and Instantly webhook.

```sql
CREATE TABLE IF NOT EXISTS review_feedback (
    id                  SERIAL      PRIMARY KEY,
    run_id              TEXT        REFERENCES runs(run_id),
    lead_id             TEXT,
    campaign_id         TEXT        REFERENCES campaigns(id),
    canonical_domain    TEXT        REFERENCES company_profiles(canonical_domain),
    feedback_type       TEXT        NOT NULL,
    feedback_reason     TEXT,
    corrected_decision  TEXT,
    fit_outcome         TEXT,
    owner               TEXT,
    notes               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Feedback types: `false_positive`, `false_negative`, `wrong_persona`, `wrong_company`, `duplicate`, `good_fit_bad_timing`, `won`, `lost`, `outreach_reply`, `outreach_bounce`, `meeting_booked`

#### `entity_embeddings`

Semantic retrieval via pgvector. One row per company or person, updated on re-enrichment.

```sql
CREATE TABLE IF NOT EXISTS entity_embeddings (
    id           SERIAL      PRIMARY KEY,
    entity_type  TEXT        NOT NULL,
    entity_id    TEXT        NOT NULL,
    campaign_id  TEXT,
    text_payload TEXT        NOT NULL,
    embedding    vector(1536),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

***

## Entity Resolution

Before any company or person is upserted, it must pass through entity resolution to prevent fragmented records. Skipping this causes the entity store to produce multiple records per company, breaking aggregate history queries.

### Company Resolution

1. Normalize domain: strip scheme, strip `www.`, lowercase, extract root domain
   - `www.ByteDance.com` → `bytedance.com`
2. Look up `company_profiles` by `canonical_domain`. If found, update in place.
3. If no domain, fuzzy-match `normalized_company_name` (Levenshtein ≤ 2 or token-sort ratio ≥ 90).
4. If still no match, create new record.

### Person Resolution

1. Normalize name: lowercase, strip honorifics (Mr, Dr, Prof) and suffixes (Jr, PhD, MBA).
2. Look up by `linkedin_url` — this is the stable primary key.
3. If no LinkedIn URL, fuzzy-match on `(normalized_person_name, canonical_domain)` pair.
4. If found via fuzzy match, update `latest_title` and `last_seen_at`. Do not overwrite historical data.
5. If not found, create new record.

***

## Signal Refresh Model

Company profiles become stale. The system must proactively refresh profiles, not only enrich at ingestion.

### Staleness TTL

| Entity             | Data Type                | Refresh If Older Than |
| ------------------ | ------------------------ | --------------------- |
| `company_profiles` | Apollo + website summary | 30 days               |
| `company_signals`  | News, job postings       | 30 days               |
| `person_profiles`  | Title, seniority         | 60 days               |

### Refresh Trigger Sources

- **Scheduled job**: background task queries `company_profiles` where `last_enriched_at < now() - interval` and re-enriches the batch.
- **On-demand**: meeting prep requests check staleness and re-enrich before generating a deck if the profile is stale.
- **Event-triggered**: an Instantly webhook delivering a reply or bounce triggers immediate re-enrichment of that company's signals.

### Hot-Lead Flagging

When a refresh detects a high-value signal (funding round, AI hiring spike, product launch), a `company_signals` row is written and `is_hot` is set to `TRUE` on `company_profiles`. AEs clear the flag after reviewing in the UI.

***

## Outcome Memory Pipeline

The highest-value learning signal is downstream outreach outcome — whether an email was replied to, a meeting was booked, or a deal was won or lost. This data lives in Instantly.ai and must flow back into the entity store automatically.

### Instantly Webhook Flow

Endpoint: `POST /api/webhooks/instantly` (no session auth — called by Instantly servers)

```
Instantly event arrives
    ↓
Extract domain from lead_email
    ↓
Resolve canonical_domain via entity resolution
    ↓
Map event_type to signal_type:
    reply  → outreach_reply  → write company_signals + set is_hot = TRUE
    bounce → outreach_bounce → write review_feedback only
    open   → skip
    click  → skip
    ↓
Write review_feedback row (feedback_type, lead_id, campaign_id, owner = "instantly_webhook")
    ↓
Return { "status": "ok" | "skipped" | "ignored" }
```

Never return 5xx for malformed payloads. Log and return `ignored`.

### AE-Entered Outcomes

AEs attach outcomes from the UI to any company or lead:

- Won / Lost (with competitor name if lost)
- No fit (wrong persona, wrong segment, poor timing, budget)
- Meeting booked (with attendees)
- Custom note with free text

These write to `review_feedback` and append a `company_signals` row with `source = manual`.

### What the Outcome Loop Enables

Once this data flows back, the system can:

- Surface reply rate by segment in the analytics view
- Weight similar-company retrieval toward historically responsive profiles
- Identify false-positive clusters (high score, low reply rate) for prompt calibration
- Feed labeled examples into the campaign evaluation harness

***

## Access Layer Design

Three surfaces serve different moments in the AE/ops workflow. They are complementary, not competing — they read the same underlying data.

### MCP Server (Primary — AE Meeting Prep)

**When used**: AE is about to get on a call. Conversational, on-demand.

**How it works**: AE opens Claude.ai desktop (or Claude Code). They have configured the MCP server once in settings. They query naturally:

> "Prep me for my Acme Corp call. They're a potential Seedance customer. Meeting is with their VP of Content."

Claude calls MCP tools in sequence and composes the answer.

**Tools to expose:**

| Tool                                                  | Description                                                               |
| ----------------------------------------------------- | ------------------------------------------------------------------------- |
| `get_company_profile(domain)`                         | Full enriched profile: Apollo data, size, funding, tech stack, fit scores |
| `get_person_profile(name, domain)`                    | Contact intel: role, seniority, LinkedIn signals, past interactions       |
| `get_signals(domain, days=30)`                        | Recent events from `company_signals` table                                |
| `get_campaign_fit(domain, campaign_id)`               | Latest score, evidence summary, qualification reasons                     |
| `generate_meeting_deck(domain, person_name, context)` | Structured markdown briefing in predefined format                         |
| `search_similar(description)`                         | pgvector semantic search across company profiles                          |
| `get_lead_history(domain)`                            | All prior Cortex runs and decisions for this account                      |

**Implementation**: A small Python MCP server (`mcp/server.py`) that wraps FastAPI backend endpoints. The MCP server translates tool calls into HTTP requests. The FastAPI endpoints do the real work.

**AE setup**: One-time config in Claude.ai desktop → Settings → MCP Servers. After that, zero friction — no CLI, no new UI to learn.

### Web UI (Secondary — Ops Visibility and Async Prep)

Keep all existing functionality. Add:

| New Page                   | Content                                                                                                |
| -------------------------- | ------------------------------------------------------------------------------------------------------ |
| `/accounts`                | Company profiles table — searchable, filterable by campaign fit, signal activity, is\_hot flag         |
| `/accounts/{domain}`       | Full company profile: Apollo data, signal timeline, all runs this company appeared in, fit by campaign |
| `/contacts/{linkedin_url}` | Person profile: role history, seniority, past interactions                                             |
| `/meeting-prep`            | Form: enter company + meeting context → generate deck → download as markdown                           |

The UI is the ops/manager view and the non-technical AE fallback. Same data as MCP, different interface.

### Claude Code Skill (Tertiary — Batch Ops and Technical Users)

The existing skill in `.trae/skills/lead-gen-cortex-operator/` handles batch CSV scoring. Extend it with a `profile` subcommand:

```
# Existing
score_csv.py --input leads.csv --campaign seedance

# New
profile.py --domain acme.com --campaign seedance
```

For AEs comfortable with the terminal or VS Code, the skill provides fast account lookups without needing the full UI or Claude.ai desktop.

### REST API (Programmatic / CRM Integration)

New agent API endpoints on the existing FastAPI app for programmatic access and future CRM integrations:

```
GET  /api/agent/memory/company/{domain}   — full company history
GET  /api/agent/memory/person             — person lookup (by linkedin_url or name+domain)
POST /api/agent/review-feedback           — AE outcome capture
POST /api/agent/profile/company           — trigger re-enrichment
POST /api/agent/profile/fit               — score a company/person against a campaign
POST /api/webhooks/instantly              — Instantly outcome webhook (no auth)
```

***

## Meeting Deck Format

The predefined structure for Mode D output:

```
1. Company Snapshot
   Industry, headcount, HQ, funding stage, total raised, tech stack keywords

2. Key Contacts
   Each attendee: title, seniority, decision authority, LinkedIn signals

3. Signal Timeline (last 30/60/90 days)
   Funding rounds, AI job postings, product launches, press mentions
   Source-attributed, dated

4. Campaign Fit
   Cortex score for the relevant campaign
   Evidence summary: observed, inferred, unknowns
   Hard disqualifiers (if any)

5. Recommended Angle
   Which product capability maps to their specific pain
   Phrased for the attending persona

6. Talking Points (3-5)
   Specific hooks grounded in signal evidence

7. Risk Flags
   Disqualifiers, churn indicators, competitive signals, open unknowns

8. Interaction History
   Prior leads scored, emails sent, reply/bounce outcomes, AE notes
```

***

## Implementation Phases

### Phase 1 — Entity Store Foundation (Build First)

**Goal**: every future run populates company and person history automatically.

- Add `company_profiles`, `company_signals`, `person_profiles`, `lead_memory`, `review_feedback` tables to `db/store.py`
- Implement entity resolution helpers (`normalize_domain`, `normalize_name`, `upsert_company`, `upsert_person`)
- Wire `upsert_company` + `upsert_person` + `write_lead_memory` into `main.py` after each lead is routed (fire-and-forget, never block the pipeline)
- Parse Apollo funding events from `EnrichedLead.recent_signals` and write to `company_signals`
- Add Instantly webhook endpoint (`POST /api/webhooks/instantly`)
- Add three read endpoints: `GET /api/agent/memory/company/{domain}`, `GET /api/agent/memory/person`, `POST /api/agent/review-feedback`

**Outcome**: the entity store populates silently on every batch run. The Instantly feedback loop closes.

### Phase 2 — Signal Enrichment + Profile Endpoints

**Goal**: profiles have fresh external signals, not just Apollo data from ingestion.

- Add Tavily or Perplexity connector for news and job posting signals
- Implement staleness check and on-demand re-enrichment
- Add `POST /api/agent/profile/company` endpoint (trigger re-enrichment, return full profile)
- Add `POST /api/agent/profile/fit` endpoint (score a company/person pair against a campaign without a CSV)
- UI: add `/accounts` list page and `/accounts/{domain}` detail page

**Outcome**: company profiles are fresh and queryable. AEs can look up any account in the UI.

### Phase 3 — MCP Server

**Goal**: AEs can query account intelligence conversationally from Claude.ai desktop.

- Create `mcp/server.py` as a thin Python MCP server wrapping the FastAPI backend
- Implement MCP tools: `get_company_profile`, `get_person_profile`, `get_signals`, `get_campaign_fit`, `get_lead_history`
- Publish setup instructions for AEs (one-time config in Claude.ai desktop settings)

**Outcome**: primary AE access pattern is live. No new UI needed.

### Phase 4 — Meeting Intelligence

**Goal**: AEs can generate a structured meeting deck in under 30 seconds.

- Implement `generate_meeting_deck` LLM call using company profile + signals + campaign fit + contact profiles
- Add MCP tool: `generate_meeting_deck`
- UI: add `/meeting-prep` page with form and markdown download
- Define and lock the deck format (see Meeting Deck Format section above)

**Outcome**: Mode D is live. AE meeting research time drops from 30-60 minutes to under 5.

### Phase 5 — pgvector + Semantic Search

**Goal**: AEs can find similar accounts and ops can cluster patterns.

- Install pgvector extension
- Add `entity_embeddings` table
- Generate and store embeddings on company profile upsert
- Implement `search_similar` MCP tool
- Add semantic search to `/accounts` UI page

**Outcome**: "Find companies like Midjourney but in video" works.

### Phase 6 — Campaign Lifecycle + Evaluation Harness

**Goal**: new campaigns can be onboarded safely without breaking existing ones.

- Add `campaign_versions` table with `draft → candidate → active → archived` lifecycle
- Implement shadow scoring (run candidate campaign version in parallel, review delta)
- Build evaluation harness against labeled benchmark leads
- Add campaign version tagging to `lead_memory` rows

**Outcome**: new products (e.g. Veo, future Byte products) can be added with confidence.

***

## Storage Decision

**PostgreSQL + pgvector. No new infrastructure.**

The system already runs PostgreSQL. Extending it with pgvector covers semantic search without adding a separate vector database to operate. JSONB handles semi-structured enrichment data. Full-text search handles free-text queries. The `apollo_cache` table already holds the seed data for `company_profiles`.

| Need                                     | Solution                            |
| ---------------------------------------- | ----------------------------------- |
| Structured entity profiles               | Relational tables                   |
| Semi-structured enrichment (Apollo JSON) | JSONB columns                       |
| Time-series signal history               | `company_signals` append-only table |
| Semantic search                          | pgvector extension                  |
| Full-text search over notes/signals      | `pg_tsvector`                       |
| Existing run history                     | Already in place                    |

A graph database (Neo4j, FalkorDB) is not recommended at this stage. The relationship queries needed (who at company X, which contacts are connected) can be answered with SQL joins on `person_profiles` and `company_profiles`. Revisit if org-chart traversal or investor relationship mapping becomes a requirement.

***

## Key Design Rules

1. **Memory may evolve automatically; policy may not.** The system can write entity memories, update signal timelines, and summarize patterns without human approval. It cannot change scoring thresholds, routing rules, disqualifiers, or campaign prompt content.
2. **`company_signals`** **is write-once.** Never update or delete rows. A funding round captured three months ago must still be readable when the profile is re-enriched.
3. **Entity resolution runs before every upsert.** Skipping it fragments the entity store. Domain normalization and fuzzy name matching are prerequisites, not optional.
4. **DB failures never abort the lead pipeline.** All entity upserts in `main.py` are fire-and-forget, wrapped in try/except. A DB outage degrades gracefully — leads are still scored and routed.
5. **Prior context enters Stage 2, not Stage 1.** Memory retrieved about a company should be injected into the Stage 2 system prompt only, not Stage 1. Stage 1 must always see only current evidence to preserve extraction integrity.
6. **MCP tools are read-and-act only.** The agent orchestration layer can retrieve data and trigger read-side operations. It cannot modify scoring thresholds, campaign files, or routing rules. Policy changes require human approval in the UI.

***

## What This Is Not

- This is not a replacement for the two-stage scoring kernel. The kernel stays intact.
- This is not a free-form autonomous agent. The MCP tools are a fixed, auditable set of actions.
- This is not a CRM. It does not replace Salesforce or HubSpot. It feeds into them.
- This is not an email sending system. Instantly.ai handles outreach. Cortex feeds it qualified leads and reads back outcomes.

