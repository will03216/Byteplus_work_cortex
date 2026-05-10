# Cortex For Lead Generation

This repository contains the current working prototype of the Seedance lead generation engine: an LLM-powered qualification and prioritization pipeline designed to turn noisy outbound signals into a ranked, auditable lead list.

The long-term vision is bigger than lead scoring. The goal is to build a reusable "Cortex" for outbound: a context-aware system that understands Seedance's ICP, product, sales messaging, target personas, buying signals, and outreach style, then helps SDRs and AEs decide who to target and how to engage them.

Today, the repo implements the first practical layer of that vision:

- ingest lead data from JSON or Trigify-style CSV exports
- normalize and lightly enrich each lead
- run a two-stage LLM qualification workflow
- apply deterministic Python routing
- output CSV queues plus JSON artifacts for review and audit

This document serves as both the current README and the working PRD for what has been built so far.

---

## 1. Executive Summary

The business problem is straightforward: outbound agencies are not producing enough value, pipeline pressure is high, and manual lead review is slow, inconsistent, and hard to scale. Antti's direction is to build an internal AI-assisted outbound engine that improves lead quality fast, while laying the foundation for a broader sales operator over time.

The current system focuses on the shortest path to value:

- use existing lead exports and Trigify data
- enrich them with lightweight external context where possible
- have Cortex extract structured evidence first, then score fit second
- route each lead into `qualified`, `review`, or `disqualified`
- preserve artifacts so the team can debug false positives and improve the rubric quickly

The strategic direction is to move from prompt engineering to context engineering. Instead of writing one-off prompts, the system should reason from a centralized body of business context:

- product knowledge
- ICP definitions
- target personas
- use cases
- disqualification rules
- brand voice and sales messaging
- historical outreach examples
- market and vertical priorities

In the short term, success means producing a better ranked lead list than today's manual or agency-led process. In the long term, success means each AE starting the day with:

- a ranked account list
- context on why each account matters now
- draft outreach aligned with company messaging
- signals such as hiring, funding, leadership changes, and strategic initiatives

---

## 2. Product Vision

### Vision Statement

Build Seedance Cortex as a context-aware outbound operator for lead qualification, account prioritization, and eventually personalized outreach support.

### Why This Matters

Current outbound work has three major problems:

- too much manual reading of weak signals
- inconsistent qualification quality across people and days
- no durable memory of why a lead was accepted or rejected

The system should replace ad hoc judgment with a repeatable, inspectable workflow.

### Long-Term Product Direction

This project is intended to evolve from a lead qualification engine into a broader sales operations assistant that can support:

- account research
- buying signal detection
- strategic initiative detection
- daily account prioritization
- outreach drafting
- repetitive AE and SDR workflows

Cortex is expected to use LLM reasoning and personalization. Tools such as Clay or other enrichment systems can later supply structured firmographic, contact, and intent data.

---

## 3. Current Scope

### In Scope Today

- batch lead ingestion from `.json` and `.csv`
- Trigify-style CSV parsing into a normalized lead schema
- lead normalization and deduplication
- lightweight enrichment
- two-stage Cortex reasoning pipeline
- Python-side score validation and routing
- CSV outputs for action queues
- per-lead JSON artifacts for auditability
- domain knowledge encoded in `skills/` markdown files and prompt templates

### Explicitly Not Yet In Scope

- CRM write-back
- automated email or LinkedIn sending
- real-time ingestion or webhooks
- a dashboard or review UI
- learning from reviewer feedback automatically
- a full AE operator workflow
- production infrastructure for large-scale always-on outbound

### Status

This is a strong v1 prototype / pilot system, not yet a full production-grade outbound platform.

It already proves the architecture and gives the team something real to evaluate. It still needs data, workflow, integration, and reliability hardening before it should be treated as production-ready.

---

## 4. Target Users

- Antti and SDR leadership, who define ICP, qualification logic, and success criteria
- SDRs, who need a better first-pass qualified lead queue
- AEs, who eventually need ranked accounts plus context and draft messaging
- Engineering / ops, who need a deterministic system that can be tuned safely

---

## 5. What Has Been Built

The implemented system looks like this:

```text
Lead input (JSON or CSV)
        ↓
Normalization
        ↓
Enrichment
        ↓
Cortex Stage 1: Evidence Extraction
        ↓
Cortex Stage 2: Qualification Decision
        ↓
Python Decision Router
        ↓
CSV outputs + JSON artifacts
```

### Core Principle

The LLM is the reasoning engine. Python is the control plane.

The LLM does not own workflow state, retries, routing, file output, or score integrity. Cortex only accepts structured JSON from the model. Python orchestrates the pipeline and enforces deterministic behavior where possible.

### Repository Structure

```text
lead_generation_engine/
├── main.py
├── config.py
├── requirements.txt
├── README.md
├── SOLUTION_DESIGN.md
├── PRD_REVIEW.md
├── normalization/
│   ├── csv_loader.py
│   └── normalizer.py
├── claude/
│   ├── prompts.py
│   ├── stage1_evidence.py
│   └── stage2_decision.py
├── tools/
│   ├── enrichment.py
│   └── web_enrichment.py
├── router/
│   └── decision_router.py
├── output/
│   ├── csv_writer.py
│   └── artifacts/
└── skills/
    ├── disqualification-rules/
    ├── lead-qualification/
    ├── seedance-icp/
    └── trigify-signal-scoring/
```

---

## 6. System Workflow

### 6.1 Ingestion

The CLI accepts either:

- JSON arrays of lead objects
- Trigify-style CSV exports

The code auto-detects the file type from the extension.

### 6.2 CSV Handling

The CSV loader maps rows into the internal lead format. Based on the current sample in `10 gen leads.csv`, the engine expects columns such as:

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

Because the CSV does not contain one canonical signal body, `signal_text` is synthesized from available fields, especially:

- who the person is
- what interaction happened
- the Trigify qualification note
- company context such as industry, size, and location

### 6.3 Normalization

The normalization layer:

- validates required fields using Pydantic
- strips and cleans text
- normalizes domains
- standardizes LinkedIn URLs
- normalizes titles
- deduplicates by `lead_id`
- skips invalid leads instead of crashing the full batch

### 6.4 Enrichment

The enrichment layer currently combines:

- stubbed company profile lookup
- stubbed recent signal lookup
- local role seniority heuristic
- best-effort website scraping
- best-effort LinkedIn profile scraping

Important caveat: the structured company and recent-signal enrichment functions are still stubs, but web enrichment is live and best-effort.

Current enrichment behavior:

- `get_company_profile()` returns an empty structured object
- `get_recent_signals()` returns an empty structured object
- `get_role_seniority()` classifies titles into `senior`, `mid`, `junior`, or `unknown`
- `scrape_company_website()` fetches and summarizes homepage content
- `scrape_linkedin_profile()` tries to recover public profile text and tenure clues

### 6.5 Cortex Stage 1: Evidence Extraction

Stage 1 does not score leads. It extracts structured evidence.

It returns:

- `segment`
- `candidate_use_case`
- `signal_strength`
- `observed_evidence`
- `inferred_evidence`
- `unknowns`
- `hard_disqualifiers`

This separation is important. The engine explicitly distinguishes:

- observed facts
- inferred conclusions
- unknown data

That keeps the system more auditable and reduces the risk that confident inferences get treated like facts.

### 6.6 Cortex Stage 2: Qualification Decision

Stage 2 receives only the Stage 1 output, not the raw lead.

It returns:

- `decision`
- `score`
- `confidence`
- five sub-scores
- qualification and disqualification reasons
- reasoning summary
- suggested use case

Python then:

- forces `lead_id` consistency
- clamps sub-scores to configured maxima
- re-sums the total score
- corrects the decision if it does not match thresholds

### 6.7 Final Python Routing

The router applies the final authoritative rules:

- hard disqualifiers always route to `disqualified`
- `signal_strength == "low"` always routes to `disqualified`
- otherwise threshold-based routing applies

```text
score >= 70        -> qualified
60 <= score < 70   -> review
score < 60         -> disqualified
```

### 6.8 Output

Each run writes:

- `output/qualified_leads.csv`
- `output/review_leads.csv`
- `output/disqualified_leads.csv`
- `output/run.log`

Each processed lead also gets three JSON artifacts:

- `{lead_id}_input.json`
- `{lead_id}_evidence.json`
- `{lead_id}_decision.json`

This is one of the most useful parts of the implementation because it creates an audit trail for review, debugging, and scoring calibration.

---

## 7. ICP And Qualification Logic

The current business logic is based on the Antti discussion and encoded in both prompts and `skills/` reference files.

### Priority Verticals

- film studios and film production companies
- short drama / short-form vertical video producers
- AI tools companies building with video generation
- video-focused creative agencies
- high-volume content creator businesses and influencer studios
- advertising teams with video at scale
- adjacent marketing teams where video production is a real bottleneck

### Priority Personas

Highest priority:

- Founder / Co-founder
- CEO / Managing Director
- Head of Production
- Head of Content
- VP / Director of Creative
- CTO / CPO for AI-video product companies

Strong champions / influencers:

- Creative Director
- Senior Producer
- Lead Editor
- AI / ML lead
- Senior Motion Designer

Low-value or disqualifying contacts:

- interns
- junior roles
- coordinators
- assistants
- analysts without real buying power

### Important Signal Logic

Antti's signal priority is reflected in the prompt design:

- posts are strongest
- comments are stronger than likes
- likes are still meaningful for the right ICP and persona
- low signal should only mean weak evidence plus poor fit, not merely a passive interaction

### Important Context Signals

The system is designed to notice signals such as:

- hiring for AI, video, or creative roles
- new executives or leadership changes
- funding events
- strategic expansion
- product launches

### New Executive Signal

One important rule from Antti is explicitly encoded: a newly appointed senior decision-maker can be a high-value target because they are under pressure to make changes.

---

## 8. Scoring Model

The current implemented weights in `config.py` are:

| Factor | Weight | What It Measures |
|---|---:|---|
| Segment Fit | 30 | how closely the company matches the priority verticals |
| Buyer Fit | 25 | how much decision power or influence the person likely has |
| Pain Fit | 20 | how strongly the signal suggests speed, scale, cost, or workflow pain |
| Workflow Fit | 15 | whether the company/person has a relevant video, AI, or creative workflow and how strong the engagement signal is |
| Commercial Fit | 10 | whether the company appears viable in size, stage, and momentum |

Current thresholds:

```text
score >= 70        -> qualified
60 <= score < 70   -> review
score < 60         -> disqualified
```

Hard overrides:

- any hard disqualifier -> disqualified
- low signal strength -> disqualified

This weighting reflects the latest code and prompt calibration, which now puts the highest emphasis on:

- vertical fit
- buyer quality
- pain
- workflow and signal quality
- commercial viability

---

## 9. Why The Two-Stage Cortex Design Matters

The system intentionally splits reasoning into two steps.

### Stage 1 answers:

"What do we actually know?"

### Stage 2 answers:

"Given what we know, how should we score this lead?"

This separation improves:

- auditability
- prompt clarity
- scoring consistency
- debugging when lead quality is wrong

If a lead is overqualified, the team can inspect whether the mistake came from:

- bad evidence extraction
- incorrect business logic
- wrong scoring weights
- weak source data

---

## 10. Run The Project

### Local Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Configure Environment

Copy `.env.example` to `.env`, then choose the model/provider you want:

```bash
cp .env.example .env
```

Example `.env` for Claude:

```dotenv
LLM_MODEL=anthropic/claude-haiku-4-5-20251001
LLM_API_KEY=sk-ant-...
LLM_PROVIDER=
LLM_API_BASE=
LLM_TIMEOUT_SECONDS=30
```

Example `.env` for BytePlus ModelArk / Dola Seed 2.0 Pro:

```dotenv
LLM_MODEL=seed-2-0-pro-260328
LLM_API_KEY=your-ark-api-key
LLM_PROVIDER=openai
LLM_API_BASE=https://ark.ap-southeast.bytepluses.com/api/v3
LLM_TIMEOUT_SECONDS=30
```

The app reads these values at startup, so switching providers is a `.env` change instead of a code change.

### Run The CLI With A CSV Export

```bash
python3 main.py --input "10 gen leads.csv" --output-dir ./output --concurrency 10
```

### Run The CLI With JSON Input

```bash
python3 main.py --input ./leads.json --output-dir ./output --concurrency 10
```

### CLI Options

```text
python3 main.py --input <file> [options]

--input, -i       required input file (.json or .csv)
--output-dir, -o  output directory for CSVs and artifacts
--concurrency, -c concurrent lead processing, allowed range 5-20
--verbose, -v     enable debug logging
```

### Run The Web UI Locally

The FastAPI web UI lets you upload CSVs, inspect current and historical runs, and deep-link to a run by `run_id` in the URL.

Without a database:

```bash
python3 ui/server.py
```

Open:

- `http://localhost:8080`

With a database:

- Set `DATABASE_URL` before starting the server
- Results persist across restarts and historical runs stay available in the UI

Example:

```bash
export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/seedance
python3 ui/server.py
```

### Run Locally With Docker Compose

This is the easiest full local setup if you want both the web app and PostgreSQL.

```bash
docker compose up --build
```

Open:

- `http://localhost:8080`

What Docker Compose provides:

- `app` service for the web UI and pipeline runner
- `db` service using PostgreSQL 16
- persisted Docker volumes for database state and run files

To stop:

```bash
docker compose down
```

To remove volumes too:

```bash
docker compose down -v
```

### Deploy In The Cloud

The recommended production shape is:

- one app container running `ui/server.py`
- one managed PostgreSQL instance
- environment variables injected by the hosting platform

Required environment variables:

```dotenv
DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/seedance
LLM_MODEL=seed-2-0-pro-260328
LLM_API_KEY=your-api-key
LLM_PROVIDER=openai
LLM_API_BASE=https://ark.ap-southeast.bytepluses.com/api/v3
LLM_TIMEOUT_SECONDS=30
```

Build the image:

```bash
docker build -t lead-generation-engine-v1 .
```

Run the image manually against managed PostgreSQL:

```bash
docker run -d \
  --name lead-engine \
  -p 8080:8080 \
  -e DATABASE_URL='postgresql://USER:PASSWORD@HOST:5432/seedance' \
  -e LLM_MODEL='seed-2-0-pro-260328' \
  -e LLM_API_KEY='your-api-key' \
  -e LLM_PROVIDER='openai' \
  -e LLM_API_BASE='https://ark.ap-southeast.bytepluses.com/api/v3' \
  -e LLM_TIMEOUT_SECONDS='30' \
  lead-generation-engine-v1
```

Suitable cloud targets include:

- AWS ECS or Fargate with RDS PostgreSQL
- GCP Cloud Run with Cloud SQL PostgreSQL
- Render, Railway, or Fly.io with managed PostgreSQL
- a VM with Docker plus managed PostgreSQL

Cloud deployment checklist:

- provision PostgreSQL and set `DATABASE_URL`
- store API keys in the platform secret manager
- expose port `8080`
- verify `GET /api/runs` after deployment
- upload a sample CSV and confirm runs persist after container restart

---

## 11. Input Schema

For JSON input, each lead should follow this shape:

```json
{
  "lead_id": "unique-string",
  "person_name": "Full Name",
  "title": "VP of Creative",
  "company_name": "Example Studio",
  "company_domain": "examplestudio.com",
  "linkedin_url": "https://linkedin.com/in/example",
  "signal_text": "Raw signal or qualification note",
  "signal_type": "linkedin_post",
  "signal_timestamp": "2026-04-17T10:00:00Z"
}
```

---

## 12. Outputs

### Qualified Leads

The qualified CSV captures:

- core lead identity
- decision
- score
- confidence
- segment
- suggested use case
- reasoning summary

### Review Leads

The review CSV captures:

- core lead identity
- score and confidence
- missing data derived from Stage 1 unknowns
- reasoning summary

### Disqualified Leads

The disqualified CSV captures:

- core lead identity
- score and confidence
- disqualification reasons
- reasoning summary

### Artifacts

Artifacts are the real source of truth for debugging. They preserve:

- what entered the pipeline
- what Cortex extracted
- what Cortex scored after Python validation

---

## 13. Existing Knowledge Sources In The Repo

The repo already includes a structured knowledge layer under `skills/`. These files capture the campaign-specific and shared business context that Cortex uses for qualification:

- product context
- ICP and persona definitions
- signal interpretation rules
- qualification logic
- disqualification rules
- shared evidence extraction guidance

Today, these files act as managed prompt inputs. Over time, they can evolve into a richer context layer that is versioned, reviewed, and reused across campaigns.

---

## 14. Pilot Plan

The recommended pilot is simple and practical:

- take a recent batch of 100 leads
- have Antti manually identify the top 10 accounts or contacts
- run the same batch through Cortex
- compare routing, scores, and ranking against that human baseline
- refine ICP, disqualifiers, thresholds, and signal interpretation based on misses

This pilot matters because the real question is not whether the system runs. It is whether Cortex improves business judgment, reduces review time, and surfaces better leads than the current process.

---

## 15. What Is Still Missing For Production Level

The core architecture is in place, but production readiness still requires work across product, data, workflow, and reliability.

### Product And Calibration

- final ICP, persona, and anti-ICP tuning using real lead outcomes
- clearer evaluation metrics for precision, recall, and ranking quality
- a regular human review loop for false positives and false negatives
- stronger campaign messaging and brand-voice assets for downstream outreach

### Data And Enrichment

- more reliable company and people enrichment sources
- better signal coverage for funding, hiring, seniority, and recency
- ingestion beyond CSV exports into more repeatable upstream pipelines
- stronger handling for sparse or partially missing lead data

### Workflow And Distribution

- CRM write-back and downstream workflow integration
- a cleaner SDR review and approval process
- account-level prioritization, not just lead-level routing
- outreach drafting and next-best-action outputs once qualification quality is stable

### Reliability And Operations

- broader automated test coverage
- run-level monitoring, metrics, and alerting
- stronger failure handling around external data dependencies
- production-grade deployment, secrets management, and operational hardening

---

## 16. Next Steps

The next phase should stay focused on a tight build-measure-improve loop rather than a broad feature expansion.

### Immediate Priorities

- run the 100-lead pilot against Antti's ranked baseline
- review where Cortex matched, missed, or over-weighted weak signals
- refine scoring weights, thresholds, and disqualification logic
- replace remaining placeholder assumptions with Seedance-specific targeting rules

### Near-Term Build Priorities

- improve company, person, and momentum enrichment coverage
- make review outputs more useful for SDR decision-making
- add account-level prioritization on top of per-lead qualification
- prepare outreach drafting only after qualification quality is consistently strong

### Medium-Term Priorities

- integrate with CRM and AE workflows
- expand the reusable context layer for product, ICP, and messaging
- pair Cortex reasoning with stronger structured data sources such as Clay or similar systems
- evolve from a qualification tool into a daily outbound operating layer

---

## 17. Estimated Time To Reach Production Level

The timeline depends mostly on data access, review speed, and how quickly the team can make calibration decisions. Assuming one focused builder, frequent feedback from Antti, and no major procurement blockers, a reasonable estimate is:

### Phase 1: Pilot Readiness

Estimated time: `1-2 weeks`

Focus:

- calibrate ICP and scoring against real leads
- complete the 100-lead benchmark
- improve ranking quality and reduce obvious false positives

### Phase 2: Operational Hardening

Estimated time: `1-2 weeks`

Focus:

- strengthen enrichment and data completeness
- improve review outputs and evaluation metrics
- define acceptance thresholds for internal use

### Phase 3: Workflow Integration

Estimated time: `2-4 weeks`

Focus:

- add CRM and workflow integrations
- support account prioritization and outreach handoff
- harden deployment, monitoring, and operational reliability

### Practical Estimate

- internal pilot ready for SDR use: `3-4 weeks`
- stronger production-ready lead engine with integrations and guardrails: `5-8 weeks`
- broader outbound operating layer beyond qualification: additional phased work after that

These estimates assume the team continues testing with real leads instead of waiting for a fully polished first release.

---

## 18. Strategic Direction Beyond This Repo

The current repository is the first useful building block, not the end state.

The broader roadmap is:

- move from prompt engineering to context engineering
- centralize company knowledge so Cortex reasons from the same source of truth every time
- combine Cortex reasoning with structured data systems for signal collection
- evolve from lead qualification into a daily outbound operating system for SDRs and AEs

If this project works, the next version should not just answer "is this lead good?"

It should answer:

- which accounts matter most today
- why they matter now
- who to contact
- what signal triggered the recommendation
- what message should be sent first

That is the real Cortex vision this repo is starting to implement.
