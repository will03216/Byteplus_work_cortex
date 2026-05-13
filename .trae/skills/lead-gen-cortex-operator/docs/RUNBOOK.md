# CSV Runbook

Use this runbook when the user wants the skill package itself to process a CSV file end to end.

## What The Skill Package Can Execute

The skill package now includes a standalone script:

`runner/score_csv.py`

And a benchmark evaluator:

`runner/evaluate_benchmark.py`

This script:

1. reads a Trigify-style CSV
2. normalizes each lead
3. performs lightweight enrichment
4. makes 2 direct LLM API calls per lead
5. applies deterministic score validation and routing in Python
6. writes scored CSV outputs and JSON artifacts
7. tracks resumable run state and failed rows

## Required Inputs

- CSV file path
- `LLM_API_KEY`
- `LLM_MODEL`
- `LLM_PROVIDER` or `--provider`

Optional:

- `LLM_API_BASE`
- `LLM_TIMEOUT_SECONDS`
- `LLM_MAX_RETRIES`
- `LLM_CONCURRENCY`
- `LLM_REQUESTS_PER_MINUTE`
- campaign id such as `seedance` or `kling`

## Default Command

```bash
python3 .trae/skills/lead-gen-cortex-operator/runner/score_csv.py \
  --input "/absolute/path/to/leads.csv" \
  --output-dir "/absolute/path/to/output" \
  --provider anthropic \
  --concurrency 2 \
  --requests-per-minute 20 \
  --campaign seedance
```

## Output Files

The script writes:

- `all_scored_leads.csv`
- `qualified_leads.csv`
- `review_leads.csv`
- `disqualified_leads.csv`
- `errors.csv`
- `summary.json`
- `run_manifest.json`
- `config_snapshot.json`
- `artifacts/{lead_id}_input.json`
- `artifacts/{lead_id}_enrichment.json`
- `artifacts/{lead_id}_evidence.json`
- `artifacts/{lead_id}_decision.json`
- `quality_report.json`

## How It Works

The standalone runner processes the CSV at the `lead signal` level, not at the global `person/company memory` level.

For each row, it:

1. creates a normalized lead record
2. derives a deterministic `lead_id` from person, company, and row position
3. performs lightweight enrichment
4. makes Stage 1 and Stage 2 LLM calls
5. validates the final score and routing in Python
6. writes artifacts and aggregated CSV outputs

The resumability boundary is the output directory.

That means:

- the same `--output-dir` is treated as the same run state
- a different `--output-dir` is treated as a new run

## Duplicate Uploads And Reprocessing

### If You Upload The Same CSV Again

If you run the same CSV again with the same:

- input path
- output directory
- campaign
- provider
- model
- API base

then the runner resumes from `run_manifest.json`.

Behavior:

- completed leads are skipped
- failed leads are retried
- existing outputs are rebuilt from manifest state

### If You Upload The Same CSV To A New Output Directory

The runner treats that as a fresh run.

Behavior:

- all rows are reprocessed
- new artifacts and output CSV files are generated

### If The CSV File Changes But You Reuse The Same Output Directory

Do not rely on resumability in that case.

Current behavior:

- the manifest validates the input path and config
- it does not compare a file checksum or full row-level content hash

Recommended rule:

- if the CSV contents changed, use a new output directory

## Same Person On Another Post / Like / Comment

In the standalone skill, a repeated person with a different interaction signal is processed again.

Current behavior:

- the runner is signal-level, not person-level
- a new row with a new post/like/comment is treated as a new scoring event
- this is true even if the same person and company appear again

Examples:

- same person comments on one post and likes another post -> both rows are scored
- same person appears in a new CSV upload with a different signal -> it is scored again
- same person appears in a fresh run using a new output directory -> it is scored again

This is intentional for the standalone v1 skill because each interaction can carry a different signal strength and qualification context.

## What The Standalone Skill Does Not Do

The standalone runner does not currently provide:

- global deduplication across multiple CSV uploads
- person-level or company-level memory merging
- cross-run entity resolution
- "already scored this person before, skip them" logic

If you want entity-level memory, deduplication, or cross-run history, use the backend memory layer rather than the standalone runner alone.

## Benchmark Check

Run this before a release candidate:

```bash
python3 .trae/skills/lead-gen-cortex-operator/runner/evaluate_benchmark.py \
  --benchmark ".trae/skills/lead-gen-cortex-operator/benchmarks/seedance_benchmark.json" \
  --output ".trae/skills/lead-gen-cortex-operator/benchmark_reports/seedance_report.json" \
  --provider anthropic \
  --model "$LLM_MODEL"
```

## Operational Notes

- The script supports `anthropic` and `openai-compatible`.
- Lead processing can run concurrently, but LLM requests are globally throttled.
- Website enrichment is best-effort only.
- Routing is always revalidated in Python after Stage 2.
- Hard disqualifiers and low signal override the model score.
- Transient provider failures retry automatically with backoff.
- Re-running against the same `--output-dir` resumes completed work and retries failed leads.
- If the existing manifest does not match the current input/campaign/provider/model, use a new output directory.
- Default concurrency and request pacing are intentionally conservative to avoid rate-limit spikes.

## When To Use The Backend Instead

Use the backend only when the user wants:

- persistent memory
- run history
- campaign activation
- email preview/export/send

Do not require the backend when the user only wants:

- CSV scoring
- a scored output file
- per-lead evidence and decision artifacts
