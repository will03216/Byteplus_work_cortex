# Installation Guide

## Upload Artifact

Use the upload-compatible zip artifact that contains:

- `lead-gen-cortex-operator/SKILL.md`

That zip should be produced from the skill folder root, not from `.trae/skills/`.

## What To Upload

Recommended artifact name:

- `lead-gen-cortex-operator-1.0.0-rc1.zip`

## After Upload

The skill package expects:

- Python 3 available in Claude Code execution context
- access to the uploaded CSV file
- environment variables for the selected provider

Required:

- `LLM_API_KEY`
- `LLM_MODEL`
- `LLM_PROVIDER`

Optional:

- `LLM_API_BASE`
- `LLM_TIMEOUT_SECONDS`
- `LLM_MAX_RETRIES`
- `LLM_CONCURRENCY`
- `LLM_REQUESTS_PER_MINUTE`

## First CSV Run

```bash
python3 lead-gen-cortex-operator/runner/score_csv.py \
  --input "/absolute/path/to/leads.csv" \
  --output-dir "/absolute/path/to/output" \
  --provider anthropic \
  --concurrency 2 \
  --requests-per-minute 20 \
  --campaign seedance
```

## First Benchmark Check

```bash
python3 lead-gen-cortex-operator/runner/evaluate_benchmark.py \
  --benchmark "lead-gen-cortex-operator/benchmarks/seedance_benchmark.json" \
  --output "lead-gen-cortex-operator/benchmark_reports/seedance_report.json" \
  --provider anthropic \
  --model "$LLM_MODEL"
```

## If Upload Fails

Common cause:

- `SKILL.md` is nested too deep in the zip

Correct layout:

- `lead-gen-cortex-operator/SKILL.md`

Incorrect layout:

- `.trae/skills/lead-gen-cortex-operator/SKILL.md`
