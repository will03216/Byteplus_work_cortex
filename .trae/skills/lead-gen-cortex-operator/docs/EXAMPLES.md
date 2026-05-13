# Examples

## Example 1. Score A CSV Directly

User prompt:

`Use the Lead Gen Cortex Operator skill to score this CSV directly and return the qualified, review, and disqualified outputs.`

Preferred execution:

```bash
python3 .trae/skills/lead-gen-cortex-operator/runner/score_csv.py \
  --input "/absolute/path/to/trigify.csv" \
  --output-dir "/absolute/path/to/skill-output" \
  --concurrency 2 \
  --requests-per-minute 20 \
  --provider anthropic \
  --campaign seedance
```

## Example 2. Score Only The First 10 Rows

```bash
python3 .trae/skills/lead-gen-cortex-operator/runner/score_csv.py \
  --input "/absolute/path/to/trigify.csv" \
  --output-dir "/absolute/path/to/skill-output" \
  --campaign seedance \
  --limit 10
```

## Example 3. Skip Website Enrichment

```bash
python3 .trae/skills/lead-gen-cortex-operator/runner/score_csv.py \
  --input "/absolute/path/to/trigify.csv" \
  --output-dir "/absolute/path/to/skill-output" \
  --campaign seedance \
  --disable-web-enrichment
```

## Example 4. Use A Different Campaign

```bash
python3 .trae/skills/lead-gen-cortex-operator/runner/score_csv.py \
  --input "/absolute/path/to/trigify.csv" \
  --output-dir "/absolute/path/to/skill-output" \
  --campaign kling
```

## Example 5. Use An OpenAI-Compatible Endpoint

```bash
python3 .trae/skills/lead-gen-cortex-operator/runner/score_csv.py \
  --input "/absolute/path/to/trigify.csv" \
  --output-dir "/absolute/path/to/skill-output" \
  --provider openai-compatible \
  --api-base "https://your-router.example/v1/chat/completions" \
  --campaign seedance
```

## Example 6. Resume An Interrupted Run

Run the same command again with the same output directory:

```bash
python3 .trae/skills/lead-gen-cortex-operator/runner/score_csv.py \
  --input "/absolute/path/to/trigify.csv" \
  --output-dir "/absolute/path/to/skill-output" \
  --provider anthropic \
  --campaign seedance
```

Behavior:

- completed leads are skipped
- failed leads are retried
- `errors.csv` and `run_manifest.json` are updated

## Example 7. Increase Throughput Carefully

```bash
python3 .trae/skills/lead-gen-cortex-operator/runner/score_csv.py \
  --input "/absolute/path/to/trigify.csv" \
  --output-dir "/absolute/path/to/skill-output" \
  --provider anthropic \
  --concurrency 3 \
  --requests-per-minute 30 \
  --campaign seedance
```

Use this only after confirming the provider and account limits can handle the higher request pace.

## Example 8. Add A New Campaign

Add a new folder under:

`campaigns/<campaign_id>/`

Required files:

- `config.json`
- `product.md`
- `icp.md`
- `signals.md`
- `disqualification.md`
- `qualification.md`

Then run:

```bash
python3 .trae/skills/lead-gen-cortex-operator/runner/score_csv.py \
  --input "/absolute/path/to/trigify.csv" \
  --output-dir "/absolute/path/to/skill-output" \
  --campaign <campaign_id>
```

## Example 9. Run The Benchmark Before Release

```bash
python3 .trae/skills/lead-gen-cortex-operator/runner/evaluate_benchmark.py \
  --benchmark ".trae/skills/lead-gen-cortex-operator/benchmarks/seedance_benchmark.json" \
  --output ".trae/skills/lead-gen-cortex-operator/benchmark_reports/seedance_report.json" \
  --provider anthropic \
  --model "$LLM_MODEL"
```
