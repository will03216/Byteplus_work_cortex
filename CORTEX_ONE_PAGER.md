# Cortex One-Pager

## What Cortex Is

Cortex is an AI-powered lead generation and qualification engine built to turn noisy outbound inputs into a ranked, reviewable, and actionable lead pipeline. In the current implementation, Cortex ingests lead exports from CSV or JSON, normalizes the records, enriches them with external context, runs a two-step LLM reasoning workflow, applies deterministic business rules in Python, and produces lead queues plus auditable artifacts for review.

At a product level, Cortex acts as the reasoning layer for outbound. Instead of relying on manual SDR judgment or static scoring rules alone, it uses a structured understanding of ICP, buyer seniority, pain signals, workflow fit, and commercial fit to determine which leads deserve action now.

## What Cortex Does

Cortex helps teams answer five practical questions for every lead:

- Is this company in the right segment for the campaign?
- Is this person likely to be a buyer, champion, or low-value contact?
- Does the signal suggest real urgency, pain, or strategic relevance?
- Is there enough evidence to act now, or does the lead need review?
- Should the lead be routed to qualified, review, or disqualified?

The current system supports campaign-specific qualification logic through markdown-based knowledge packs under `skills/`. That means Cortex is not hardcoded to one product only. It can load different campaign contexts, score models, and thresholds for products such as Seedance and Kling, making it a reusable outbound decision engine rather than a single prompt script.

## Why Cortex Is Required For Lead Generation

Lead generation teams usually face three persistent problems:

- Too much noise: raw signals such as likes, comments, hiring activity, or scraped lead exports contain weak and inconsistent intent clues.
- Too much inconsistency: different SDRs interpret the same lead differently, which creates unstable pipeline quality.
- Too little auditability: when a lead is accepted or rejected, teams often cannot explain why, improve the logic, or trace model mistakes.

Cortex addresses these problems by separating reasoning from workflow control. The LLM handles pattern recognition and context-based judgment, while Python enforces deterministic thresholds, routing rules, retries, and output integrity. This is important for lead generation because teams need both intelligence and operational trust. A lead engine is only useful if it is explainable, tunable, and safe to operationalize.

In practice, Cortex improves lead generation by:

- Prioritizing higher-value accounts and personas faster
- Reducing manual research time per lead
- Standardizing qualification quality across runs
- Preserving evidence and reasoning for calibration
- Creating clean downstream queues for sales review and outreach

## Architecture And Workflow

Per lead, Cortex follows this workflow:

```text
Lead Input (CSV / JSON)
  -> Normalization
  -> Enrichment
  -> LLM Call 1: Evidence Extraction
  -> LLM Call 2: Qualification Decision
  -> Python Decision Router
  -> CSV Outputs + JSON Artifacts + UI Review
```

### 1. Ingestion And Normalization

Cortex accepts either CSV exports or JSON lead files. It validates and standardizes fields such as `lead_id`, domain, title, LinkedIn URL, and signal text. Duplicate leads are removed by `lead_id`, and invalid rows are skipped instead of breaking the whole batch.

### 2. Enrichment Layer

Before any model reasoning happens, Cortex enriches each lead with additional context:

- company profile data from Apollo when enabled
- website summary from best-effort homepage scraping
- public LinkedIn summary from best-effort profile scraping
- local role seniority heuristics based on title
- recent company signals when available

This step gives the model better context without giving the model direct control over tools or workflow.

### 3. LLM Call 1: Evidence Extraction

The first model call is not a scoring step. It is a structured evidence extraction step. Cortex asks the model to identify:

- target segment
- candidate use case
- signal strength
- observed evidence
- inferred evidence
- unknowns
- hard disqualifiers

This separation is critical because it forces the system to distinguish facts from inference. That makes later decisions easier to inspect and debug.

### 4. LLM Call 2: Qualification Decision

The second model call receives only the Stage 1 evidence object, not the raw lead. It returns:

- overall decision
- total score
- confidence
- five sub-scores: segment fit, buyer fit, pain fit, workflow fit, commercial fit
- qualification reasons
- disqualification reasons
- reasoning summary
- suggested use case

Python then clamps sub-scores to allowed ranges, recomputes totals, and corrects any model output that violates thresholds or score integrity.

### 5. Deterministic Routing

After the two LLM calls, Cortex applies Python-side routing rules:

- hard disqualifiers always force `disqualified`
- `low` signal strength always forces `disqualified`
- otherwise score thresholds determine `qualified`, `review`, or `disqualified`

This gives Cortex a hybrid architecture: LLM for judgment, code for control.

## Why We Built It This Way

Cortex was designed to solve a very specific operational problem: qualify and prioritize leads in a way that is useful to sales teams, explainable to operators, and safe to tune over time. That requirement shaped the architecture.

We did not want a free-form autonomous agent deciding what tools to use, what evidence to trust, how to update memory, and how to score leads in one open-ended loop. In lead generation, that can drift very quickly. An agent can over-explore weak signals, make inconsistent assumptions across leads, or introduce silent variability in how decisions are made from one run to the next.

Instead, we chose a narrower and more deterministic design:

- Python owns orchestration, retries, routing, output writing, and score integrity
- enrichment happens in a fixed and inspectable sequence
- the model is asked to return structured JSON, not execute workflow decisions
- routing thresholds and hard overrides are enforced in code
- every lead produces artifacts that can be reviewed after the run

This makes Cortex easier to calibrate with sales leadership because a bad outcome can be traced to a specific layer: source data quality, enrichment gaps, evidence extraction, scoring logic, or routing thresholds.

## Why Two LLM Calls Are Better Than One

The two-call design is deliberate, not accidental.

### Call 1: Evidence Extraction

The first call answers: "What do we know, what do we infer, and what is still unknown?"

That forces the model to separate:

- observed facts
- inferred conclusions
- missing information
- hard disqualifiers

### Call 2: Qualification Decision

The second call answers: "Given only that structured evidence, how should this lead be scored and routed?"

This split is valuable for several reasons:

- Better auditability: reviewers can inspect the evidence object before looking at the final score
- Better consistency: Stage 2 reasons over a normalized evidence format instead of noisy raw inputs
- Better debugging: if the model over-qualifies a lead, we can tell whether the mistake came from extraction or scoring
- Better control: Python can validate the final score and decision against fixed thresholds
- Better prompt discipline: each model call does one job well instead of mixing extraction, interpretation, and routing into one large prompt

If we used a single LLM call, the model would have to read messy input, enrich its own interpretation, score the lead, explain the result, and implicitly decide which facts matter, all in one step. That tends to reduce transparency and makes it much harder to diagnose mistakes.

## Why We Did Not Make It More Agentic

An agentic design sounds attractive because, in theory, the model could research each account more deeply, choose tools dynamically, explore multiple signals, and update long-lived memory as it learns. For this product stage, that creates more risk than value.

For lead generation, the main failure mode is not lack of autonomy. It is lack of consistency.

We intentionally avoided a more agentic architecture because:

- Scope can expand too quickly: an agent may chase low-signal pages, irrelevant entities, or weak correlations
- Runtime becomes unpredictable: different leads may trigger very different tool paths and response times
- Costs become harder to control: open-ended tool use can increase token and network usage sharply
- Decisions become harder to compare: if each lead follows a different reasoning path, calibration gets harder
- Failure analysis becomes weaker: when an agent mixes retrieval, interpretation, memory updates, and scoring, it is difficult to isolate what went wrong
- Operational trust drops: sales teams need repeatable behavior more than creative exploration

That is why Cortex uses a bounded workflow rather than a fully autonomous agent. It still uses LLM reasoning, but inside a narrow contract with deterministic guardrails.

## Why This Is Better Than An LLM Wiki For This Use Case

An LLM wiki approach is powerful for building a persistent, compounding knowledge base from many sources over time. It is well suited to research, team knowledge management, competitive intelligence, and domains where the main goal is to synthesize and maintain evolving context.

Cortex solves a different problem.

The goal here is not to build an open-ended knowledge repository first and answer questions from it later. The goal is to make repeatable go/no-go lead decisions inside an operational pipeline.

For lead generation, Cortex is better than an LLM wiki approach in several important ways:

- Decision-first architecture: Cortex is optimized to produce qualification outcomes, not just maintain knowledge pages
- Deterministic workflow: every lead follows the same bounded path from ingestion to routing
- Stronger control plane: Python enforces thresholds, score ranges, and disqualification overrides
- Easier auditing: artifacts are tied directly to each lead and each stage of reasoning
- Lower drift risk: the system does not rely on a large evolving wiki that may accumulate stale synthesis or ambiguous cross-references
- Better calibration: sales leaders can tune scoring logic and inspect misses against a fixed pipeline
- Better operational fit: output is immediately usable as qualified, review, and disqualified queues

The LLM wiki approach is designed to compile knowledge into a persistent intermediate layer between raw documents and later reasoning. That is valuable when the main challenge is long-horizon synthesis across many sources.

In Cortex, the main challenge is different:

- interpret a lead signal
- apply campaign-specific qualification logic
- produce a reliable decision now
- explain that decision clearly

For that use case, a structured per-lead pipeline is a better fit than a continuously maintained wiki.

That said, the two ideas are not opposites. Cortex could later borrow some wiki-like patterns for campaign knowledge management, such as maintaining persistent summaries of ICP changes, persona definitions, or historical signal learnings. But for v1 lead qualification, a deterministic pipeline is stronger than a continuously self-updating knowledge layer.

## Functional Features

The implementation already includes the following product features:

- Batch lead ingestion from CSV and JSON
- Multi-campaign support through configurable `skills/` packs
- Two-stage LLM lead qualification pipeline
- Provider-agnostic LLM support through LiteLLM
- Lead enrichment from Apollo, company websites, and LinkedIn
- Deterministic score validation and routing in Python
- Qualified, review, and disqualified CSV outputs
- Per-lead JSON artifacts for full audit trails
- FastAPI web UI for uploads, run history, and lead inspection
- PostgreSQL-backed persistence for campaigns, runs, leads, settings, exclusions, and templates
- Server-sent event progress streaming for live run monitoring
- Email template generation and Instantly integration hooks for outreach workflows

## Why The Design Matters

The most important product design choice in Cortex is the two-call LLM architecture. One call answers, "What do we know?" The second answers, "Given what we know, how should we score and route this lead?" That structure makes the system more reliable for lead generation because teams can isolate whether a miss came from weak source data, bad evidence extraction, or flawed qualification logic.

In short, Cortex is not just a lead scoring script. It is an auditable outbound reasoning system designed to help lead generation teams find better opportunities faster, apply qualification logic consistently, and build a reusable AI operating layer for future account prioritization and outreach.
