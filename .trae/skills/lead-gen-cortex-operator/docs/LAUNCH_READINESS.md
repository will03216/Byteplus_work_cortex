# Launch Readiness

## Release Candidate

- Package: `lead-gen-cortex-operator`
- Version: `1.0.0-rc1`
- Recommended upload artifact: `dist/lead-gen-cortex-operator-1.0.0-rc1.zip`

## Readiness Verdict

Status: `Conditional Go`

Meaning:

- the package is structurally ready for a v1 release candidate
- the core runner, benchmark harness, packaging, and docs are in place
- one live validation pass is still required before calling it launch-ready in production

## What Is Verified

### Package And Upload Structure

- upload-compatible zip exists
- zip contains `lead-gen-cortex-operator/SKILL.md`
- skill package includes runner, benchmarks, campaign packs, docs, and version metadata

### Runner Integrity

- `runner/score_csv.py` compiles
- `runner/evaluate_benchmark.py` compiles
- deterministic routing and score validation are implemented in Python
- provider support exists for `anthropic` and `openai-compatible`
- resumable runs are implemented via `run_manifest.json`
- partial failures are captured in `errors.csv`
- bounded concurrency and global request throttling are implemented
- post-run `quality_report.json` is generated

### Release Packaging

- `VERSION` file exists
- `CHANGELOG.md` exists
- `INSTALL.md` exists
- `RELEASE_CHECKLIST.md` exists
- versioned release artifact is in `dist/`

## What Is Not Yet Verified Live

These are the remaining launch blockers to verify with real credentials and a real CSV:

### Provider Path

- Anthropic live API run succeeds with the intended production model
- OpenAI-compatible path succeeds if you plan to support it in launch

### Run Path

- a small real CSV completes successfully
- resumable rerun skips completed leads correctly
- forced failure path writes `errors.csv` as expected

### Benchmark Path

- `seedance_benchmark.json` run completes live
- `kling_benchmark.json` run completes live
- benchmark outputs are reviewed for unacceptable routing drift

### Claude Code Upload Path

- upload of `lead-gen-cortex-operator-1.0.0-rc1.zip` succeeds in Claude Code
- first live prompt-driven run succeeds from the uploaded skill

## Recommended Smoke Tests

### 1. Upload Test

Upload:

- `dist/lead-gen-cortex-operator-1.0.0-rc1.zip`

Pass condition:

- skill imports successfully

### 2. Small CSV Test

Run:

```bash
python3 lead-gen-cortex-operator/runner/score_csv.py \
  --input "/absolute/path/to/leads.csv" \
  --output-dir "/absolute/path/to/output" \
  --provider anthropic \
  --concurrency 2 \
  --requests-per-minute 20 \
  --campaign seedance \
  --limit 5
```

Pass condition:

- CSV outputs are created
- artifacts are created
- no unexpected failures

### 3. Resume Test

Run the same command again against the same output directory.

Pass condition:

- completed leads are skipped
- previously failed leads are retried

### 4. Benchmark Test

Run:

```bash
python3 lead-gen-cortex-operator/runner/evaluate_benchmark.py \
  --benchmark "lead-gen-cortex-operator/benchmarks/seedance_benchmark.json" \
  --output "lead-gen-cortex-operator/benchmark_reports/seedance_report.json" \
  --provider anthropic \
  --model "$LLM_MODEL"
```

Pass condition:

- benchmark report is generated
- no unacceptable routing drift relative to expectations

## Recommended Launch Defaults

- provider: `anthropic`
- concurrency: `2`
- requests per minute: `20`
- timeout seconds: `120`
- max retries: `3`
- start with `seedance`

These defaults are intentionally conservative to minimize provider-limit risk during the first live launch.

## Go / No-Go Rule

Go:

- if upload succeeds
- if a small real CSV run succeeds
- if resume behavior works
- if benchmark results are acceptable

No-Go:

- if provider calls fail with the intended launch model
- if resumability corrupts outputs
- if benchmark routing drifts materially from expected outcomes
- if Claude Code cannot reliably access and run the bundled scripts

## Recommendation

Current recommendation: `Proceed to live smoke test`

This package is ready for a controlled v1 launch candidate after one final live verification pass in Claude Code with real provider credentials and a small CSV.
