#!/usr/bin/env python3
"""
Benchmark evaluator for the Lead Gen Cortex Operator skill bundle.

Runs a bundled benchmark dataset against the same direct scoring stack used by
score_csv.py and writes a machine-readable report.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from score_csv import (
    DEFAULT_PROVIDER,
    RequestRateLimiter,
    SUPPORTED_PROVIDERS,
    _default_api_base,
    load_campaign,
    normalize_lead,
    score_one_lead,
)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the bundled lead-scoring benchmark.")
    parser.add_argument("--benchmark", required=True, help="Path to a benchmark JSON file")
    parser.add_argument("--output", required=True, help="Path to write the benchmark report JSON")
    parser.add_argument("--provider", default=os.environ.get("LLM_PROVIDER", DEFAULT_PROVIDER), help="LLM provider: anthropic or openai-compatible")
    parser.add_argument("--api-base", default=os.environ.get("LLM_API_BASE", ""), help="Provider API base URL; optional when using defaults")
    parser.add_argument("--model", default=os.environ.get("LLM_MODEL", ""), help="LLM model name")
    parser.add_argument("--api-key", default=os.environ.get("LLM_API_KEY", ""), help="LLM API key")
    parser.add_argument("--timeout-seconds", type=int, default=int(os.environ.get("LLM_TIMEOUT_SECONDS", "120")), help="Per-request LLM timeout")
    parser.add_argument("--max-retries", type=int, default=int(os.environ.get("LLM_MAX_RETRIES", "3")), help="Retries for transient LLM failures")
    parser.add_argument("--requests-per-minute", type=int, default=int(os.environ.get("LLM_REQUESTS_PER_MINUTE", "20")), help="Global LLM request cap for the benchmark")
    parser.add_argument("--disable-web-enrichment", action="store_true", help="Skip best-effort website fetch")
    return parser.parse_args(argv)


def load_benchmark(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def evaluate_case(result: dict[str, Any], expected: dict[str, Any]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    routing = result["routing"]
    score = int(result["decision"]["score"])
    expected_routing = expected.get("routing")
    if expected_routing and routing != expected_routing:
        errors.append(f"routing expected {expected_routing}, got {routing}")
    min_score = expected.get("min_score")
    max_score = expected.get("max_score")
    if min_score is not None and score < int(min_score):
        errors.append(f"score expected >= {min_score}, got {score}")
    if max_score is not None and score > int(max_score):
        errors.append(f"score expected <= {max_score}, got {score}")
    return (len(errors) == 0, errors)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    provider = str(args.provider or "").strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        print(f"Unsupported provider '{args.provider}'. Expected one of: {', '.join(sorted(SUPPORTED_PROVIDERS))}", file=sys.stderr)
        return 2
    if not args.api_key:
        print("Missing LLM_API_KEY or --api-key", file=sys.stderr)
        return 2
    if not args.model:
        print("Missing LLM_MODEL or --model", file=sys.stderr)
        return 2
    if args.timeout_seconds <= 0:
        print("--timeout-seconds must be > 0", file=sys.stderr)
        return 2
    if args.max_retries < 0:
        print("--max-retries must be >= 0", file=sys.stderr)
        return 2
    if args.requests_per_minute < 0:
        print("--requests-per-minute must be >= 0", file=sys.stderr)
        return 2

    benchmark_path = Path(args.benchmark).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    benchmark = load_benchmark(benchmark_path)
    campaign = load_campaign(benchmark["campaign_id"])
    api_base = (args.api_base or "").strip() or _default_api_base(provider)
    rate_limiter = RequestRateLimiter(args.requests_per_minute)

    case_results: list[dict[str, Any]] = []
    passed = 0
    failed = 0
    for case in benchmark.get("cases", []):
        lead = normalize_lead(case["lead"])
        raw_row = case.get("raw_row", {})
        try:
            result = score_one_lead(
                lead,
                raw_row,
                campaign=campaign,
                provider=provider,
                model=args.model,
                api_key=args.api_key,
                api_base=api_base,
                timeout_seconds=args.timeout_seconds,
                max_retries=args.max_retries,
                allow_web=not args.disable_web_enrichment,
                rate_limiter=rate_limiter,
            )
            ok, errors = evaluate_case(result, case.get("expected", {}))
            case_results.append({
                "case_id": case["case_id"],
                "description": case.get("description", ""),
                "ok": ok,
                "errors": errors,
                "routing": result["routing"],
                "score": result["decision"]["score"],
                "signal_strength": result["evidence"]["signal_strength"],
                "segment": result["evidence"]["segment"],
            })
            if ok:
                passed += 1
            else:
                failed += 1
        except Exception as exc:
            failed += 1
            case_results.append({
                "case_id": case["case_id"],
                "description": case.get("description", ""),
                "ok": False,
                "errors": [f"{type(exc).__name__}: {exc}"],
            })

    report = {
        "benchmark_id": benchmark.get("benchmark_id"),
        "campaign_id": benchmark.get("campaign_id"),
        "provider": provider,
        "model": args.model,
        "api_base": api_base,
        "passed": passed,
        "failed": failed,
        "total": passed + failed,
        "pass_rate": round((passed / (passed + failed)) * 100.0, 2) if (passed + failed) else 0.0,
        "cases": case_results,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
