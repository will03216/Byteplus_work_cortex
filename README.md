# Claude Brain For Lead Generation

This repository contains the current working prototype of the Seedance lead generation engine: a Claude-powered qualification and prioritization pipeline designed to turn noisy outbound signals into a ranked, auditable lead list.

The long-term vision is bigger than lead scoring. The goal is to build a reusable "Claude brain" for outbound: a context-aware system that understands Seedance's ICP, product, sales messaging, target personas, buying signals, and outreach style, then helps SDRs and AEs decide who to target and how to engage them.

Today, the repo implements the first practical layer of that vision:

- ingest lead data from JSON or Trigify-style CSV exports
- normalize and lightly enrich each lead
- run a two-stage Claude qualification workflow
- apply deterministic Python routing
- output CSV queues plus JSON artifacts for review and audit

This document serves as both the current README and the working PRD for what has been built so far.

---

## 1. Executive Summary

The business problem is straightforward: outbound agencies are not producing enough value, pipeline pressure is high, and manual lead review is slow, inconsistent, and hard to scale. Antti's direction is to build an internal AI-assisted outbound engine that improves lead quality fast, while laying the foundation for a broader sales operator over time.

The current system focuses on the shortest path to value:

- use existing lead exports and Trigify data
- enrich them with lightweight external context where possible
- have Claude extract structured evidence first, then score fit second
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

Build a Seedance "Claude brain" that acts as a context-aware outbound operator for lead qualification, account prioritization, and eventually personalized outreach support.

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

Claude is expected to handle reasoning and personalization. Tools such as Clay or other enrichment systems can later supply structured firmographic, contact, and intent data.

---

## 3. Current Scope

### In Scope Today

- batch lead ingestion from `.json` and `.csv`
- Trigify-style CSV parsing into a normalized lead schema
- lead normalization and deduplication
- lightweight enrichment
- two-stage Claude reasoning pipeline
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
Claude Stage 1: Evidence Extraction
        ↓
Claude Stage 2: Qualification Decision
        ↓
Python Decision Router
        ↓
CSV outputs + JSON artifacts
```

### Core Principle

Claude is the reasoning engine. Python is the control plane.

Claude does not own workflow state, retries, routing, file output, or score integrity. Claude only returns structured JSON. Python orchestrates the pipeline and enforces deterministic behavior where possible.

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

### 6.5 Claude Stage 1: Evidence Extraction

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

### 6.6 Claude Stage 2: Qualification Decision

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
score >= 75        -> qualified
60 <= score < 75   -> review
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
score >= 75        -> qualified
60 <= score < 75   -> review
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

## 9. Why The Two-Stage Claude Design Matters

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

## 10. Current CLI Usage

### Install

```bash
pip install -r requirements.txt
```

### Configure

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

Or put the same key in a local `.env` file.

### Run With A CSV Export

```bash
python main.py --input "10 gen leads.csv" --output-dir ./output --concurrency 10
```

### Run With JSON Input

```bash
python main.py --input ./leads.json --output-dir ./output --concurrency 10
```

### CLI Options

```text
python main.py --input <file> [options]

--input, -i       required input file (.json or .csv)
--output-dir, -o  output directory for CSVs and artifacts
--concurrency, -c concurrent lead processing, allowed range 5-20
--verbose, -v     enable debug logging
```

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
- what Claude extracted
- what Claude scored after Python validation

---

## 13. Existing Knowledge Sources In The Repo

The current repo already contains a structured knowledge layer in `skills/`:

- `skills/seedance-icp/SKILL.md`
- `skills/lead-qualification/SKILL.md`
- `skills/trigify-signal-scoring/SKILL.md`
- `skills/disqualification-rules/SKILL.md`

These files are important because they represent the beginning of the broader context-engineering approach. They are not just implementation notes. They are the raw ingredients for the future Claude brain knowledge layer.

Right now they are effectively source material for the prompts. In a later version they can become dynamically loaded context modules.

---

## 14. Pilot Plan

The agreed pilot shape from the Antti discussion is:

- take the latest 100 leads
- have Antti manually identify the top 10
- run all 100 through the engine
- compare model ranking and routing against Antti's judgment
- refine ICP, disqualifiers, scoring, and signal interpretation based on the gaps

This is the right first validation step because it tests usefulness against real business judgment instead of only technical correctness.

---

## 15. What Is Still Missing For Production Level

The engine architecture is in place, but production-level outbound requires more than a working batch script.

### Business / Product Gaps

- final ICP, persona, and anti-ICP calibration using real company data
- clearer definition of success metrics for qualification precision and ranking quality
- consistent human review loop for false positives and false negatives
- brand-voice and outreach-context assets for next-step messaging generation

### Data / Enrichment Gaps

- real company and people enrichment APIs
- reliable signal ingestion beyond current CSV exports
- stronger coverage for company size, funding, hiring, and time-in-role
- better handling of missing or partial data

### Workflow Gaps

- CRM integration
- review workflow for SDRs
- account-level ranking rather than only per-lead routing
- outreach generation after qualification

### Reliability / Engineering Gaps

- broader automated test coverage
- monitoring and run-level metrics
- stronger error handling around external enrichments
- secrets and environment hardening
- deployment path beyond local CLI usage

---

## 16. Next Steps

For now, the most important next step is not a giant feature list. It is a disciplined iteration loop with Antti.

### Immediate Operating Model

- collaborate with Antti every day
- show iterative progress based on his inputs
- review where the engine matched or missed his judgment
- refine ICP rules, scoring, signal interpretation, and disqualifiers continuously

### Near-Term Build Priorities

- run the 100-lead pilot and benchmark against Antti's top 10
- tighten prompts and weights based on pilot misses
- replace placeholder business assumptions with actual Seedance targeting logic
- add stronger enrichment for company, person, and momentum signals
- add account-ranking and next-best-action outputs
- generate draft outreach once qualification quality is strong enough

### Medium-Term Priorities

- integrate with CRM and AE workflows
- move from lead-level qualification to daily prioritized account lists
- build reusable context modules for product knowledge, messaging, and historical emails
- pair Claude reasoning with structured enrichment from Clay or similar tools

---

## 17. Estimated Time To Reach Production Level

This depends on access to data, speed of decision-making, and how quickly Antti can review outputs daily. Assuming one focused builder, daily feedback from Antti, and no major procurement blockers, a realistic estimate is:

### Phase 1: Qualification Accuracy And Pilot Readiness

Estimated time: `1-2 weeks`

Focus:

- calibrate ICP and scoring against real leads
- run the 100-lead benchmark
- improve ranking precision
- replace placeholder assumptions with Seedance-specific logic

### Phase 2: Data And Workflow Hardening

Estimated time: `1-2 weeks`

Focus:

- integrate reliable enrichment sources
- improve data completeness
- add clearer review-loop outputs
- define operational metrics and acceptance thresholds

### Phase 3: Production Workflow Enablement

Estimated time: `2-4 weeks`

Focus:

- CRM integration
- outreach generation support
- account-level prioritization
- deployment, monitoring, and operational hardening

### Practical Estimate

- production-ready pilot for internal SDR use: `3-4 weeks`
- stronger production-level lead engine with integrations and operational guardrails: `5-8 weeks`
- broader "AE operator" foundation beyond lead qualification: additional phased work after that

This estimate assumes the team keeps the current daily iteration rhythm with Antti and avoids waiting for a perfectly polished v1 before testing with real leads.

---

## 18. Strategic Direction Beyond This Repo

The current repository is the first useful building block, not the end state.

The broader roadmap is:

- move from prompt engineering to context engineering
- centralize company knowledge so Claude reasons from the same source of truth every time
- combine Claude for reasoning with structured data systems for signal collection
- evolve from lead qualification into a daily outbound operating system for SDRs and AEs

If this project works, the next version should not just answer "is this lead good?"

It should answer:

- which accounts matter most today
- why they matter now
- who to contact
- what signal triggered the recommendation
- what message should be sent first

That is the real "Claude brain" vision this repo is starting to implement.
