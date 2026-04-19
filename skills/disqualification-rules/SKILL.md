# Skill: Disqualification Rules

> Last updated: 2026-04-17 — calibrated against Head of SDR (Antti) meeting

## Purpose
Hard filters that trigger immediate DISQUALIFIED status — regardless of score.
These override all scoring when present.

---

## Hard Disqualifiers

Apply these as binary flags in Stage 1 `hard_disqualifiers` array.
If ANY of these are present, the lead is DISQUALIFIED.

### 1. Wrong Industry — No Video / AI / Creative Workflow
Company operates in an industry with zero plausible video production need.
Examples: logistics, insurance, accounting, legal services, clinical healthcare, government

Be specific: "Company is a logistics firm — no video production workflow identified"

### 2. Junior Role — No Buying Power
Person's title indicates they are too junior to influence a purchasing decision.
Apply when title contains: intern, junior, assistant, coordinator, analyst (without senior prefix)
AND there is no seniority indicator elsewhere in the signal.

Antti was explicit: "junior contacts with limited buying power" are not targets.
Be specific: "Junior Animator — no purchasing authority"

### 3. Irrelevant Role Function
Person works in a department completely unrelated to content, video, AI, or creative production.
Examples: accountant, recruiter, legal counsel, HR business partner, finance analyst, DevOps engineer (unless AI/ML context)

### 4. No Plausible Use Case
Even with generous inference, there is no scenario where this company would use Seedance.
Examples: dentist office, plumbing company, physical retail with no digital presence, government agency

### 5. Wrong Company Type (Confirmed)
- Pure B2C consumer app with no content production team
- Government / public sector entity
- Non-profit or academic institution (unless AI research lab)
- Freelancer with no team and no commercial scale

### 6. Explicit Competitor Signal
Person works at a direct AI video competitor: Runway, Sora, Kling, Pika, Luma, Stability AI video, etc.

---

## What Is NOT a Hard Disqualifier

### ❌ A "Like" interaction is NOT a hard disqualifier
A LinkedIn like from a Creative Director at a film studio is a medium-low signal — it goes into
scoring, not auto-disqualification. Only disqualify on likes when BOTH conditions are true:
- Wrong ICP segment (not film, not creative, not video)
- Wrong persona (junior or irrelevant function)

If either condition is false, score it — don't hard-disqualify it.

### ❌ Small company size is NOT a hard disqualifier
A 5-person film production studio is a better lead than a 500-person insurance company.
Size is a soft factor in commercial_fit scoring only.

### ❌ Indirect or inferred pain is NOT a hard disqualifier
Not having explicit pain stated is normal — most leads engage passively. Score it lower,
don't disqualify it.

---

## Soft Disqualifiers (reduce score, do not hard-disqualify)

These lower scoring but don't trigger immediate disqualification:
- Like interaction only (right ICP but weak signal) → lower workflow_fit
- Individual contributor with no seniority in right department → lower buyer_fit
- Company too small to scale (<5 employees, solo operation) → lower commercial_fit
- No company momentum signals present → lower commercial_fit
- Geographic market with no Seedance availability → lower commercial_fit
- Time in role unknown → flag as unknown, don't penalise

---

## Application Rules

1. Check hard disqualifiers FIRST in Stage 1 before any scoring
2. If `hard_disqualifiers` list is non-empty → Python forces `decision = "disqualified"`
3. List each disqualifier as a separate specific string in the array
4. Be precise: "Junior Animator at ad agency — no buying power" not just "junior role"
5. Do NOT hard-disqualify on unknowns — only on confirmed negative signals
6. When in doubt between hard-disqualify and low score: use low score (review bucket is safer than lost leads)
