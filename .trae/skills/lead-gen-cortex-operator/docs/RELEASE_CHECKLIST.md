# Release Checklist

Use this checklist before publishing a new upload zip.

## Package Integrity

- `VERSION` file updated
- `CHANGELOG.md` updated
- `SKILL.md` description still accurate
- upload zip contains `lead-gen-cortex-operator/SKILL.md`

## Runner Validation

- `score_csv.py` compiles
- `evaluate_benchmark.py` compiles
- `score_csv.py --help` works
- `evaluate_benchmark.py --help` works

## Provider Validation

- Anthropic path tested
- OpenAI-compatible path tested if supported for release
- retries/backoff behavior still present

## Run Validation

- small CSV run completes successfully
- resumable rerun skips completed leads
- `errors.csv` is produced correctly on forced failure case
- `quality_report.json` is generated

## Benchmark Validation

- `seedance_benchmark.json` runs
- `kling_benchmark.json` runs
- benchmark reports are reviewed
- no unexpected routing drift before release

## Upload Validation

- upload-compatible zip rebuilt
- zip structure verified
- Claude Code upload test passes

## Release Output

- release zip copied to `dist/`
- final versioned artifact name recorded
- operator knows which command is the canonical run command
