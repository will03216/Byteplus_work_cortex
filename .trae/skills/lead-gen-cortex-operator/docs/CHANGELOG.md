# Changelog

## 1.1.0-rc1

Adds optional Apollo.io firmographic enrichment to the standalone runner.

- new `--enable-apollo` / `--apollo-api-key` / `--apollo-timeout` flags, plus
  `APOLLO_ENABLED` / `APOLLO_API_KEY` / `APOLLO_TIMEOUT_SECONDS` env vars
- stdlib-only, self-contained Apollo organization-enrich client (domain first,
  company-name fallback) with a thread-safe per-run cache (no DB dependency)
- compact firmographic summary attached to each `_enrichment.json` artifact and
  the Stage 1 payload; `apollo_industry` and `apollo_employees` added to result CSVs
- disabled by default; missing data stays absent rather than fabricated

## 1.0.0-rc1

Initial production-ready release candidate for the `lead-gen-cortex-operator` skill bundle.

Included in this release:

- standalone CSV scoring runner
- bundled campaign packs for `seedance` and `kling`
- native Anthropic and OpenAI-compatible provider support
- retry and backoff for transient provider failures
- resumable runs with `run_manifest.json`
- `errors.csv`, `config_snapshot.json`, and `quality_report.json`
- bounded concurrency and global request throttling
- benchmark datasets and evaluator script
- operator runbook, examples, production plan, and release docs
