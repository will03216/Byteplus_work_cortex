[OPEN] Debug Session: kling-run-failure

## Bug

- Symptom: `kling` runs `3421f813-abbe-4b7d-a0fe-1378134ca8f0` and `c8e8c7f4-d0b1-4cd8-9296-eb24745e7da6` failed immediately with `lead_count=0`
- Follow-up symptom: after fixing CSV decoding, a later run failed with `✗ lead_id=roziah_budaly_mars_2 FAILED: Cannot specify ',' with 's'.`
- Context: User provided UI URLs for failed runs under `http://localhost:8080/campaign/kling`
- Goal: Identify the runtime failure point, confirm root cause with evidence, apply a minimal fix, and verify

## Hypotheses

1. Rejected: a lead-specific enrichment step throws an exception or returns malformed data that aborts the run.
2. Rejected: the LLM output for one stage does not match the expected schema, causing validation or routing failure.
3. Rejected: the `kling` campaign configuration or skill pack is missing required fields for this run.
4. Rejected: database or artifact persistence fails after scoring, causing the run to be marked failed.
5. Confirmed root cause A: CSV ingestion fails before normalization because the uploaded file is not UTF-8 and `load_csv()` only decodes with `utf-8-sig`.
6. Confirmed root cause B: Apollo enrichment may return numeric fields as strings, and `_parse_recent_signals()` formats funding amounts with `f"${amount:,}"`, which raises `ValueError` for string inputs.

## Plan

1. Inspect run handling code paths and locate logs, run records, and failure states for the supplied run ID.
2. Start the debug server and add minimal instrumentation only if existing evidence is insufficient.
3. Reproduce or inspect the failure path, collect evidence, and confirm or reject hypotheses.
4. Apply the smallest fix supported by evidence.
5. Verify with post-fix evidence before cleanup.

## Evidence

- Live UI/API run list showed both failing `kling` runs with status `error` and `lead_count=0`
- Live progress stream for run `c8e8c7f4-d0b1-4cd8-9296-eb24745e7da6` showed:
  - `main.py -> load_csv(input_path)`
  - `UnicodeDecodeError: 'utf-8' codec can't decode bytes ... invalid continuation byte`
  - Failure occurred before normalization, enrichment, Stage 1, or DB import
- Isolated reproduction for enrichment showed:
  - `_parse_recent_signals({'funding_events': [{'amount': '1200000'}]})`
  - `ValueError: Cannot specify ',' with 's'.`
- User-provided runtime failure identified the later affected lead:
  - `✗ lead_id=roziah_budaly_mars_2 FAILED: Cannot specify ',' with 's'.`

## Fix

- Updated `normalization/csv_loader.py`
- Kept UTF-8-first decoding, but added fallback decoding for `cp1252` and `latin-1`
- Added loader logging so the chosen encoding is visible during future runs
- Updated `tools/enrichment.py`
- Added best-effort integer coercion for Apollo numeric fields such as `employee_count`, `total_funding`, and funding event `amount`
- Preserved the existing output format while preventing string-typed Apollo numerics from crashing enrichment

## Verification

- Rebuilt the Docker app so the running service uses the patched loader
- Uploaded a one-row `cp1252` CSV to the `kling` run endpoint
- Post-fix progress stream showed:
  - `CSV loader: read 1 leads ... using cp1252`
  - `Loaded 1 raw leads ...`
  - `Normalized 1 valid leads ...`
  - Stage 1 started successfully
- Reproduced the Apollo formatting error locally before the fix, then re-ran the same parser path after the fix and confirmed:
  - funding activity renders as `Funding: Series A $1,200,000 (2024-01)`
  - company profile numeric fields are parsed successfully from string values
- Rebuilt the Docker app again so the running service includes the enrichment fix

## Status

- Awaiting user confirmation after re-running the original `Kling_10_social_leads.csv` file
