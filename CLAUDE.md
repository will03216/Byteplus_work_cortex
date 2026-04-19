# CLAUDE.md — Seedance Lead Qualification Engine

## What This Project Does
Two-stage Claude pipeline that qualifies B2B leads for Seedance (ByteDance AI video generation).
Trigify exports a CSV of LinkedIn signals → Python normalizes → enriches → Stage 1 (evidence extraction) → Stage 2 (qualification scoring) → routes to qualified / review / disqualified CSVs.

---

## Core Architecture Rule — Do Not Break
**Python orchestrates everything. Claude never calls tools directly.**

- Stage 1: Claude extracts structured evidence (observed facts, inferences, unknowns, disqualifiers)
- Stage 2: Claude scores the evidence (100-point model) and returns a decision
- Python re-derives all scores and decisions independently via `_enforce_score_integrity()` in `claude/stage2_decision.py`
- Claude's output is validated input, not trusted authority

Do not change this pattern without explicit instruction.

---

## How to Run

Install dependencies (first time only):
```bash
pip install -r requirements.txt
```

Run on a single batch (recommended for testing):
```bash
python main.py --input "12 gen leads.csv" --output-dir ./output --concurrency 5 --verbose
```

Run silently on larger batch:
```bash
python main.py --input "100 gen leads.csv" --output-dir ./output --concurrency 10
```

Output lands in `./output/`:
- `qualified_leads.csv`
- `review_leads.csv`
- `disqualified_leads.csv`
- `artifacts/<lead_id>_evidence.json` — Stage 1 output
- `artifacts/<lead_id>_decision.json` — Stage 2 output
- `artifacts/<lead_id>_result.json` — final routed result
- `run.log`

---

## Do Not Change Without Asking

| What | Where | Why |
|------|-------|-----|
| `SCORE_WEIGHTS` | `config.py` | Calibrated against Antti (Head of SDR) meeting 2026-04-17 |
| `QUALIFIED_THRESHOLD = 75` | `config.py` | Agreed decision boundary |
| `DISQUALIFIED_THRESHOLD = 60` | `config.py` | Agreed decision boundary |
| Stage 1 system prompt | `claude/prompts.py` | ICP verticals and signal hierarchy agreed with Antti |
| Stage 2 system prompt | `claude/prompts.py` | Scoring rubric aligned to SCORE_WEIGHTS |
| Hard disqualifier rules | `skills/disqualification-rules/SKILL.md` | Antti explicitly defined these |

---

## Scoring Model (100 points)

| Dimension | Max | What it measures |
|-----------|-----|-----------------|
| `segment_fit` | 30 | Company vertical — film studio / short drama / AI tools score highest |
| `buyer_fit` | 25 | Persona seniority — C-suite / Founder / new executive score highest |
| `pain_fit` | 20 | Explicit or implied video production pain |
| `workflow_fit` | 15 | Signal type (post > comment > like) + relevant workflow |
| `commercial_fit` | 10 | Company size + momentum signals |

Decision thresholds: `≥75 = qualified`, `60–79 = review`, `<60 = disqualified`

Hard disqualifiers in Stage 1 always override score → disqualified, regardless of total.

Signal strength hierarchy: `post = high`, `comment = medium`, `like = medium-low`, `noise = low`.
Only `"low"` triggers auto-disqualification in the router. `"medium-low"` goes to scoring.

---

## Project File Map

```
config.py                        — all constants (model, weights, thresholds, timeouts)
main.py                          — CLI entrypoint, async batch runner, per-lead pipeline
normalization/
  normalizer.py                  — LeadInput Pydantic model, normalize_batch()
  csv_loader.py                  — Trigify CSV → LeadInput dicts
tools/
  enrichment.py                  — EnrichedLead model, enrich_lead() orchestrator
  web_enrichment.py              — async scrape_company_website() + scrape_linkedin_profile()
claude/
  prompts.py                     — Stage 1 + Stage 2 system prompts with cache_control
  stage1_evidence.py             — extract_evidence(), EvidenceOutput model
  stage2_decision.py             — make_decision(), DecisionOutput, score integrity enforcement
router/
  decision_router.py             — route_decision() — Python-authoritative final routing
output/
  csv_writer.py                  — write_result(), init_output_dir(), async CSV writes
skills/
  seedance-icp/SKILL.md          — target verticals, personas, company momentum signals
  lead-qualification/SKILL.md   — full scoring rubric
  trigify-signal-scoring/SKILL.md — signal hierarchy, evidence classification rules
  disqualification-rules/SKILL.md — hard vs soft disqualifiers
```

---

## In-Progress Work

### Completed (this session)
- `tools/web_enrichment.py` — async scrapers written (httpx + beautifulsoup4)
- `tools/enrichment.py` — `enrich_lead()` made async, `company_website_summary` + `linkedin_summary` fields added to `EnrichedLead`, both scrapers called concurrently
- `signal_strength` Literal bug fixed in `claude/stage1_evidence.py` — now includes `"medium-low"`
- `_build_enrichment_dict()` updated to include new web enrichment fields
- `requirements.txt` updated with `httpx>=0.27.0` and `beautifulsoup4>=4.12.0`

### Pending — main.py not yet updated
`main.py` still calls `enrich_lead` via `run_in_executor` (treating it as sync). It must be changed to `await enrich_lead(lead)` directly since `enrich_lead` is now async.

### Approved but not started — Agentic Orchestration Plan
Three-change plan approved by user. See `.claude/plans/soft-rolling-cerf.md` for the web enrichment plan and the conversation for the agentic plan detail. Summary:

1. **Pre-flight knockout** (`router/preflight.py`) — Python rule-check before enrichment; obvious wrong industry/junior title → skip all enrichment and Claude calls entirely
2. **Tiered enrichment** (`tools/enrichment.py`) — assign `full` / `website_only` / `none` tier per lead based on seniority + available URLs; don't scrape LinkedIn for every lead
3. **Unknown-driven re-enrichment loop** (`main.py`) — after Stage 1, if signal ≥ medium and unknowns are resolvable, fetch missing data and re-run Stage 1 once (hard cap: 1 iteration)

---

## ICP Summary (Antti, Head of SDR — 2026-04-17)

**Priority verticals:**
1. Film studios and film production companies
2. Short drama / short-form vertical video producers
3. AI tools companies building products with video generation
4. Creative agencies (video-focused)
5. High-volume content creators and influencer studios
6. Advertising agencies with video at scale

**Target personas (priority order):**
- Founder, CEO, Managing Director — full budget authority
- Head of Production, Head of Content, VP/Director of Creative — workflow owners
- CTO / CPO — if building AI products with video
- Creative Director, Senior Producer — strong champion
- Senior Designer, Senior Motion Designer, Lead Editor — influencers

**New executive signal:** Person recently started role (<12 months) → high priority, boost buyer_fit.

**Hard disqualifiers:** Wrong industry (logistics, insurance, legal, finance, gov, healthcare), junior role (intern, coordinator, assistant, analyst), explicit competitor (Runway, Sora, Kling, Pika, Luma).
