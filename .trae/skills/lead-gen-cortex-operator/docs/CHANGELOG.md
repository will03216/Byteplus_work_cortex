# Changelog

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
