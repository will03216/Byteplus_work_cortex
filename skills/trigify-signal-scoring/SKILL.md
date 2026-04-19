# Skill: Trigify Signal Scoring

> Last updated: 2026-04-17 — calibrated against Head of SDR (Antti) meeting

## Purpose
Rules for classifying the strength of Trigify signals and mapping them to intent, pain, and use case.

---

## Signal Engagement Hierarchy (Antti's Priority Order)

This is the most important rule in signal scoring. Antti stated this explicitly:

```
POST    (person published content)      →  HIGH signal
COMMENT (person engaged in discussion)  →  MEDIUM signal
LIKE    (person reacted/liked)          →  MEDIUM-LOW signal  ← NOT low, still actionable
```

**A like from the right persona in the right vertical is STILL worth reviewing.**
Signal strength must be assessed in combination with ICP fit, not in isolation.

| Interaction Type | Base Signal Strength | Notes |
|-----------------|----------------------|-------|
| Posted on LinkedIn | high | They created content — active engagement, top of mind |
| Commented on LinkedIn | medium | They engaged in depth — shows real interest |
| Liked / Reacted on LinkedIn | medium-low | Passive signal, but valid if ICP + persona match |
| Job posting (hiring AI/video roles) | medium–high | Company investing in this workflow |
| Company news / funding | medium | Context-dependent — look for video/AI angle |
| Intent data (browsing AI/video tools) | medium–high | Actively researching solutions |
| Event attendance (AI/video conference) | medium | In the ecosystem |

---

## Signal → Intent → Pain → Use Case

```
Signal (what Trigify captured)
  ↓
Intent (what the person/company is doing or thinking)
  ↓
Pain (what problem they likely have)
  ↓
Use Case (how Seedance addresses it)
```

---

## Signal Strength Classification

| Strength | When to apply | Example |
|----------|---------------|---------|
| **high** | Explicit purchase intent, direct pain stated, active evaluation of video/AI tools, OR posted about video/AI production challenges | "Actively evaluating AI video tools", "our production pipeline is too slow", hiring a Head of AI Content |
| **medium** | Indirect interest, commented on relevant content, company-level signal with video/AI angle | Commented on an AI video post, company launched video initiative, hiring video editors |
| **medium-low** | Liked relevant content from right ICP/persona, adjacent workflow, company signal with weak video angle | Liked a post about AI video tools, general creative industry news |
| **low** | Noise — wrong ICP, wrong persona, and engagement has no video/AI/creative relevance | Finance person liked a generic AI article, junior role liked unrelated content |

**Only assign "low" when both are true: wrong ICP segment AND wrong persona.**
A right-segment or right-persona like should always be medium-low, never low.

---

## Company Momentum Signals

These are additional context clues that boost the relevance of a lead — look for these in the Trigify note or signal text:

| Signal | Strength Boost | Why it matters |
|--------|---------------|----------------|
| Hiring for AI, video, or creative roles | +1 level | Company actively investing in this workflow |
| New CEO, CMO, or Head of Production | +1 level | New leaders make change — high receptivity window |
| Recent funding round | Neutral (context) | Budget available, but only relevant if ICP matches |
| Strategic product launch / expansion | Neutral (context) | Growth mode = content production demand increases |
| Agency pitching / competing for new clients | +1 level | Under commercial pressure to deliver faster |

---

## "New to Role" Signal

Antti flagged this as a high-priority signal: **executives newly appointed (< 12 months)
are prime outreach targets** because they are under pressure to demonstrate results quickly.

If the Trigify note or profile indicates someone recently started their role:
- Flag it as `"New executive — recently appointed"` in `observed_evidence`
- This should boost `buyer_fit` scoring by 3–5 points even for Director-level titles

---

## Transformation Examples

### Example 1 — High Signal (Posted)
Signal: "Creative Director at a film production company posted: 'Our team is spending 75% of time on manual edits. Looking for AI solutions to scale output.'"
- Interaction: Posted → HIGH signal
- Intent: Actively seeking AI video solution
- Pain: Manual editing at scale is the bottleneck
- Use Case: AI video generation to replace manual editing steps

### Example 2 — Medium-Low Signal (Liked) — Right ICP
Signal: "Founder of a short drama production company liked a post about Seedance's AI video capabilities."
- Interaction: Liked → MEDIUM-LOW (not low — founder of short drama = perfect ICP)
- Intent: Aware of the solution space, curious
- Pain: Inferred — short drama production demands high-volume short video output
- Use Case: Short-form AI video generation at scale

### Example 3 — Low Signal (Liked) — Wrong ICP
Signal: "HR Manager at a logistics company liked a post about AI technology trends."
- Interaction: Liked → LOW (wrong ICP + wrong persona + no video relevance)
- Intent: General AI curiosity
- Pain: None identified
- Use Case: None — hard disqualify

---

## Evidence Classification Rules

**OBSERVED** — directly stated in signal or Trigify note:
- Explicit job title and company
- The interaction type (liked, commented, posted)
- Quoted content from the signal
- Stated company industry, size, location
- Explicit "new to role" mention

**INFERRED** — logical deduction:
- "Company likely produces high-volume video because..."
- "Role suggests this person owns the production budget"
- "Short drama company = high-volume short video output by definition"

**UNKNOWNS** — needed but absent:
- Time in current role (unless stated)
- Company headcount (unless stated)
- Current tooling / existing video workflow
- Budget authority confirmation
- Specific pain point (when only inferred from engagement)
