# Skill: Lead Qualification

> Last updated: 2026-04-17 — calibrated against Head of SDR (Antti) meeting

## Purpose
Scoring rubric and decision rules for qualifying leads for Seedance (ByteDance AI video generation).

---

## Scoring Rubric — 100 Points Total

### Segment Fit (30 pts) — PRIMARY FILTER
*Is this company in one of Seedance's priority verticals?*

| Score | Criteria |
|-------|----------|
| 28–30 | Perfect match: film studio, film production company, broadcast network, television production company, major media production organization, short drama producer, AI tools company building with video |
| 20–27 | Strong match: creative agency (video-focused), advertising agency, performance marketing / creative-performance company, adtech or martech company with clear video or creative-generation workflow, content creator at scale |
| 10–19 | Adjacent: general marketing team with high video output, publisher/media company without clear production ownership, adtech/martech company without confirmed creative or video workflow ownership |
| 0–9   | No fit: B2C, SaaS with no content angle, finance, legal, government, healthcare, education |

---

### Buyer Fit (25 pts) — CRITICAL
*Is this person a decision maker, champion, or influencer with buying power?*
*Newly appointed executives are HIGH PRIORITY — under pressure to make changes.*

| Score | Criteria |
|-------|----------|
| 22–25 | C-suite, Founder, VP, Head of Production/Content/Creative, Executive Producer, Head of Programming Production, Head of Product / Product Lead for creative-generation or ad-creative workflows at target company **OR** any senior role newly appointed (< 12 months) — change appetite is high |
| 15–21 | Director, Television Director, Broadcast Director, Creative Director, Senior Producer, Senior Manager, Lead, Product Manager / Group Product Manager in adtech, martech, creative-performance, or AI-creative tooling — strong champion with budget influence |
| 6–14  | Individual contributor in right department: designer, editor, animator, producer (no seniority) |
| 0–5   | Junior, intern, coordinator, assistant, analyst — no buying power; do not qualify |

---

### Pain Fit (20 pts)
*Is there explicit or implied pain around video production cost, speed, or scale?*

| Score | Criteria |
|-------|----------|
| 18–20 | Explicit pain: production is too slow, too expensive, or can't scale to demand |
| 12–17 | Implied pain: content volume pressure, team bandwidth constraints, cost reduction signals, ad-creative iteration needs, localization needs, or performance-testing pressure |
| 5–11  | Generic growth signal with a loose connection to video (e.g. company expanding, hiring) |
| 0–4   | No pain identified or pain is clearly unrelated to video/creative production |

---

### Workflow Fit (15 pts)
*Does the signal show an active video/creative workflow AND quality of engagement?*
*This factor encodes both: what kind of signal (post > comment > like) AND whether a relevant workflow exists.*

| Score | Criteria |
|-------|----------|
| 13–15 | Person **posted** content about video/AI/creative topics OR active video production workflow explicitly confirmed |
| 8–12  | Person **commented** on video/AI content OR adjacent workflow confirmed (content production, social media, creative ops) OR person **liked highly relevant Seedance content** and their company/role strongly maps to creative, ad, or video workflow ownership |
| 3–7   | Person **liked** video/AI content OR general marketing/creative workflow inferred but not confirmed |
| 0–2   | No relevant workflow evidence and/or signal engagement has zero video/AI connection |

---

### Commercial Fit (10 pts)
*Is this company the right size and stage? Are there momentum signals?*

| Score | Criteria |
|-------|----------|
| 9–10 | Established company (50–5,000 employees), VC-backed startup, or recognised studio/agency with real revenue |
| 6–8  | SMB (<50 employees) with clear content operation, early-stage funded startup |
| 2–5  | Very small company or unclear funding/scale |
| 0–1  | Solo freelancer, nano-company, no commercial viability |

**Boost commercial_fit by 1–2 pts if any company momentum signal is present:**
- Actively hiring for AI/video roles
- New leadership (CEO, CMO, Head of Production) recently appointed
- Recent funding round
- Strategic expansion announced

---

## Decision Thresholds

```
score >= 70  →  QUALIFIED
score < 60   →  DISQUALIFIED
60 ≤ score < 70  →  REVIEW
```

Hard disqualifiers (stage 1 flags) ALWAYS override score → DISQUALIFIED.
Low signal_strength ALWAYS overrides score → DISQUALIFIED.

---

## Confidence Levels

- **high**: signal is direct (post/comment), ICP segment clear, persona is decision maker
- **medium**: signal is a like OR persona is an influencer not a decision maker, some unknowns
- **low**: signal is weak, significant data gaps, inferences outnumber observations

---

## Output Requirements

- `score` MUST equal the exact sum of all five sub-scores
- Each sub-score MUST be within its maximum: segment≤30, buyer≤25, pain≤20, workflow≤15, commercial≤10
- `decision` MUST match the threshold applied to `score`
- `reasoning_summary`: 2–3 sentences max, factual, reference specific evidence
- `qualification_reasons` and `disqualification_reasons`: short evidence-based bullets
- `suggested_use_case`: match to the segment → use case table in seedance-icp SKILL
