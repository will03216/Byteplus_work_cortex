# Cortex One-Pager

## Executive Summary

Cortex is an AI-powered lead qualification engine built to turn noisy outbound inputs into a ranked, reviewable, and operationally safe pipeline. It combines LLM-based reasoning with deterministic Python guardrails so teams can identify higher-value leads faster, apply qualification logic consistently, and preserve a full audit trail for every decision.

At a product level, Cortex serves as the decision layer between raw lead signals and sales action. Instead of relying only on manual SDR judgment or static scoring rules, it evaluates lead quality using structured evidence across company fit, buyer relevance, pain signals, workflow fit, and commercial potential.

## The Problem

Lead generation teams typically face three recurring issues:

- Noise: raw signals such as CSV exports, scraped data, likes, comments, and hiring activity contain weak or incomplete intent clues
- Inconsistency: different operators interpret the same lead differently, which creates unstable pipeline quality
- Low auditability: accepted or rejected leads are often difficult to explain, calibrate, or debug later

The result is slow manual research, inconsistent prioritization, and limited confidence in downstream outreach quality.

## The Cortex Solution

Cortex solves this by applying a bounded qualification pipeline to every lead:

- ingest and normalize lead data from CSV or JSON
- enrich records with external company and persona context
- extract structured evidence using an LLM
- score and classify the lead using a second LLM step
- enforce routing rules and score integrity in Python
- output reviewable queues and per-lead artifacts

This gives the business both intelligence and control: the model reasons over context, while the application enforces repeatable operational rules.

## Why The Architecture Matters

The key design choice in Cortex is the separation of reasoning from control.

- LLMs handle context interpretation, evidence extraction, and qualification judgment
- Python owns orchestration, thresholds, validation, retries, routing, and output integrity
- every lead follows the same inspectable workflow
- every decision can be traced back to evidence, score components, and routing logic

This makes Cortex practical for production use because it is explainable, tunable, and safer to calibrate with sales or operations leadership.

## Core Workflow

```text
Lead Input (CSV / JSON)
  -> Normalization
  -> Enrichment
  -> LLM Call 1: Evidence Extraction
  -> LLM Call 2: Qualification Decision
  -> Python Routing + Validation
  -> CSV Queues + JSON Artifacts + UI Review
```

### 1. Ingestion And Normalization

Cortex accepts CSV exports or JSON lead files, standardizes key fields such as `lead_id`, domain, title, LinkedIn URL, and signal text, removes duplicates, and skips invalid rows without breaking the full batch.

### 2. Enrichment

Before scoring, Cortex improves lead context through sources such as:

- Apollo company data when enabled
- best-effort website summarization
- best-effort LinkedIn profile summarization
- title-based seniority heuristics
- recent company signals when available

### 3. LLM Call 1: Evidence Extraction

The first model call creates a structured evidence object, including:

- target segment
- candidate use case
- signal strength
- observed evidence
- inferred evidence
- unknowns
- hard disqualifiers

This step separates facts from inference, which improves auditability and debugging.

### 4. LLM Call 2: Qualification Decision

The second model call receives the structured evidence, not the raw lead. It returns:

- overall decision
- total score and confidence
- sub-scores across segment fit, buyer fit, pain fit, workflow fit, and commercial fit
- qualification and disqualification reasons
- reasoning summary and suggested use case

Python then validates the response, clamps score ranges, recomputes totals where needed, and ensures output integrity.

### 5. Deterministic Routing

After model reasoning, Cortex applies fixed routing rules:

- hard disqualifiers force `disqualified`
- `low` signal strength forces `disqualified`
- otherwise thresholds determine `qualified`, `review`, or `disqualified`

This hybrid model gives Cortex a clear contract: LLM for judgment, code for control.

## Why Two LLM Calls Instead Of One

The two-step design is deliberate.

- Call 1 answers: what do we know, what do we infer, and what remains unknown?
- Call 2 answers: given that evidence, how should this lead be scored and routed?

This improves the system in four ways:

- better auditability because reviewers can inspect evidence before the final decision
- better consistency because scoring operates on normalized evidence instead of noisy raw inputs
- better debugging because errors can be traced to extraction or scoring separately
- better governance because Python can validate final decisions against deterministic rules

## Why Cortex Is Not Fully Agentic

For lead qualification, the primary requirement is consistency, not open-ended autonomy.

A more agentic design would allow dynamic tool use and deeper exploration, but it would also introduce:

- unpredictable runtime and cost
- inconsistent reasoning paths across leads
- weaker comparability during calibration
- harder failure analysis
- lower operational trust for sales teams

Cortex therefore uses a bounded workflow with clear guardrails rather than a free-form autonomous agent.

## Why This Is Better Than An LLM Wiki For This Use Case

An LLM wiki is useful when the goal is to build and maintain a persistent knowledge layer across many sources. Cortex solves a different problem: making repeatable go or no-go lead decisions inside an operational pipeline.

For this use case, Cortex is stronger because it is:

- decision-first rather than knowledge-first
- deterministic rather than open-ended
- directly tied to routing outcomes
- easier to audit and calibrate
- immediately usable by sales and outbound teams

In short, an LLM wiki is optimized for long-horizon synthesis. Cortex is optimized for repeatable lead qualification now.

## Current Product Capabilities

The current implementation already supports:

- batch ingestion from CSV and JSON
- multi-campaign qualification via configurable `skills/` packs
- two-stage LLM qualification workflows
- provider-agnostic model access through LiteLLM
- enrichment from Apollo, websites, and LinkedIn
- deterministic score validation and routing in Python
- qualified, review, and disqualified CSV outputs
- per-lead JSON artifacts for full audit trails
- FastAPI UI for uploads, run history, and lead inspection
- PostgreSQL-backed persistence for campaigns, runs, leads, settings, exclusions, and templates
- server-sent event progress streaming for live monitoring
- email template generation and Instantly integration hooks

## Business Impact

Cortex improves outbound execution by:

- prioritizing higher-value accounts and personas faster
- reducing manual research time per lead
- standardizing qualification quality across runs
- preserving evidence for review and calibration
- producing cleaner downstream queues for outreach and sales review

## Bottom Line

Cortex is not just a lead scoring script. It is an auditable outbound reasoning system that combines AI judgment with deterministic controls, making it suitable for production-grade lead qualification, sales alignment, and future expansion into broader account prioritization and outreach workflows.
