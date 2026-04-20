# SDR Brief: What This Repo Does

This repo is the first version of our internal lead-generation and qualification engine for Seedance.

Its job is simple:

- take a batch of leads and outbound signals
- enrich them with a bit more context
- score how good they look for Seedance
- route them into `qualified`, `review`, or `disqualified`

The goal is not just to automate lead scoring. The bigger goal is to build a reusable "brain" for outbound so SDRs and AEs can spend less time manually reading weak signals and more time working the best accounts.

## How It Works

For each lead, the system runs a simple pipeline:

1. ingest the lead from CSV or JSON
2. normalize the data so titles, domains, and URLs are cleaner
3. enrich the lead with website, LinkedIn, and role context where possible
4. run Stage 1 to extract evidence
5. run Stage 2 to score and make a decision
6. route the lead into an action queue

Important detail:

- Stage 1 does **not** score the lead. It only extracts structured evidence such as segment, signal strength, unknowns, and hard disqualifiers.
- Stage 2 uses that evidence to apply the scoring model.
- Python then double-checks the score and enforces the final routing rules.

This makes the system easier to audit. If a lead is wrong, we can see whether the problem came from bad evidence, bad scoring, or bad business rules.

## How Scoring Works

The current scoring model totals 100 points:

| Factor         | Weight | What it means                                                                   |
| -------------- | -----: | ------------------------------------------------------------------------------- |
| Segment Fit    |     30 | Is this the right type of company for Seedance?                                 |
| Buyer Fit      |     25 | Is this person likely to have buying power or strong influence?                 |
| Pain Fit       |     20 | Do we see signs of real pain around speed, scale, cost, or production workflow? |
| Workflow Fit   |     15 | Do they actually have a relevant video, AI, or creative workflow?               |
| Commercial Fit |     10 | Does the company look viable in size, stage, and momentum?                      |

<br />

V-stats tool (integration) - b2c customer for MAUs (add a custom rule for this)

ecomm c-level

Rules - \
\- 25% website traffic, 

<br />

Current routing thresholds:

```text
75+     = qualified
60-74   = review
below 60 = disqualified
```

Two important overrides matter even more than score:

- any confirmed hard disqualifier means `disqualified`
- `low` signal strength means `disqualified`

## What The Model Tries To Prioritize

Right now the engine is designed to favor:

- strong ICP matches like film, production, broadcast, short drama, AI-video tooling, and video-heavy creative businesses
- senior buyers and strong champions
- clear production pain or workflow pressure
- higher-quality signals like posts and comments

Signal quality is handled carefully:

- post = strongest
- comment = good
- like = still useful, but weaker

A like from the right company and right persona is still worth looking at. The system should only call something `low` signal when both the ICP and the persona are poor.

## What The Outputs Mean For SDRs

Each run produces three queues:

- `qualified`: leads we should actively work
- `review`: leads that may be interesting but need human judgment or more data
- `disqualified`: leads that are very unlikely to be worth time

It also saves JSON artifacts for every lead so we can inspect:

- the input
- the extracted evidence
- the final decision

That audit trail is useful because it lets us improve the model based on real misses, not just opinions.

## Where Your Help Matters

The most important business knowledge in this repo lives in the `skills/` folder:

- `skills/seedance-icp/SKILL.md`
- `skills/lead-qualification/SKILL.md`
- `skills/trigify-signal-scoring/SKILL.md`
- `skills/disqualification-rules/SKILL.md`

These files are the written rules the model uses to reason about:

- who our best customers are
- which personas matter most
- which signals are strong or weak
- what should be disqualified immediately

If these skills are vague, outdated, or incomplete, the model will score leads badly.

## What We Need From You

Your help would be most valuable in making these skills more accurate and more specific.

Please help by marking up:

- which verticals are true ICP vs adjacent vs waste of time
- which job titles are real buyers, strong champions, or too junior
- which signals should be treated as high, medium, medium-low, or low intent
- which edge cases are currently scored wrong
- which companies or personas should be hard-disqualified
- which real examples from recent outbound would make the rules sharper

The best input is concrete input, for example:

- "Head of Production at a 20-person studio should score higher than Creative Director at a generic marketing agency"
- "A like from a founder in short drama should stay reviewable, not be disqualified"
- "Product Managers at adtech companies should only score well if they clearly own creative workflow"

## Bottom Line

This repo is our first attempt at turning SDR judgment into a repeatable system.

The engine is already useful, but its quality depends heavily on how well we write the skills. The more precisely we capture real-world SDR judgment in those files, the better the lead ranking will get.
