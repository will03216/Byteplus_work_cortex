# Production Readiness Plan

This plan defines the work required to harden the `lead-gen-cortex-operator` skill bundle for a v1 launch.

## Goal

Make the skill package reliable enough that a user can upload the skill into Claude Code, provide a CSV, and consistently get back scored output files with auditable artifacts.

## Launch Standard

For v1 launch, the skill must:

1. support the target model providers used in production
2. survive transient LLM/API failures
3. resume interrupted runs safely
4. write structured outputs and error reports
5. preserve deterministic scoring consistency
6. provide basic regression checks before release

## Workstreams

### Phase 1. Runner Hardening

Priority: `P0`

Scope:

- add native Anthropic support
- keep OpenAI-compatible support
- add provider selection flags
- add retry and backoff for LLM calls
- add clearer startup validation for env vars and inputs
- add consistent request/response error handling

Acceptance criteria:

- runner works with `provider=anthropic`
- runner works with `provider=openai-compatible`
- transient HTTP failures retry automatically
- malformed or empty model output fails with clear diagnostics

### Phase 2. Run Resilience

Priority: `P0`

Scope:

- add `run_manifest.json`
- track per-lead status
- add resumable processing
- write `errors.csv`
- keep partial outputs even when some leads fail

Acceptance criteria:

- interrupted runs can restart without reprocessing completed leads
- failed leads are recorded with specific error messages
- successful leads still produce CSV and artifact outputs

### Phase 3. Throughput And Controls

Priority: `P1`

Scope:

- add bounded concurrency
- add optional rate limiting
- add provider-aware throttling
- keep deterministic output ordering

Acceptance criteria:

- multi-lead scoring is faster than sequential mode
- concurrency does not break output determinism
- rate limits do not cause noisy failures

### Phase 4. Quality Guardrails

Priority: `P1`

Scope:

- add a benchmark dataset
- add regression harness
- add schema validation checks for Stage 1 and Stage 2
- add score distribution sanity checks

Acceptance criteria:

- benchmark can run before release
- known examples retain expected routing
- regressions are visible before shipping a new zip

### Phase 5. Packaging And Release

Priority: `P1`

Scope:

- add release checklist
- add install and operator docs
- add version file and changelog
- define upload zip structure as the release artifact

Acceptance criteria:

- release artifact is always upload-compatible
- operator docs match actual commands
- skill package version is visible and trackable

## Implementation Order

1. Phase 1: provider support and retries
2. Phase 2: resumable runs and error reporting
3. Phase 3: concurrency and rate controls
4. Phase 4: benchmark and regression checks
5. Phase 5: packaging and release polish

## Suggested Tests Per Phase

### After Phase 1

- score a 3-row CSV with OpenAI-compatible endpoint
- score a 3-row CSV with Anthropic endpoint
- simulate bad API key and confirm clear failure

### After Phase 2

- kill a run mid-stream and resume it
- confirm completed leads are skipped
- confirm failed leads land in `errors.csv`

### After Phase 3

- compare sequential and concurrent output consistency
- confirm CSV row order remains stable

### After Phase 4

- run benchmark and compare routing against expected labels

### After Phase 5

- upload release zip into Claude Code
- run the first-run prompt exactly as documented

## Current Step

Phase 5 is now in progress after Phase 4 completion.
