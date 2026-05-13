#!/usr/bin/env python3
"""
Standalone CSV scorer for the Lead Gen Cortex Operator skill.

This script is intentionally self-contained so the skill can be installed in
Claude Code and run without relying on the repository backend APIs.

What it does:
1. Reads a Trigify-style CSV
2. Normalizes each lead
3. Performs lightweight enrichment
4. Makes 2 LLM calls per lead
5. Applies deterministic score validation and routing in Python
6. Writes scored CSV outputs and JSON artifacts

Environment variables:
- LLM_API_KEY   required
- LLM_MODEL     required
- LLM_API_BASE  optional, defaults to OpenAI chat completions endpoint
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_PROVIDER = "openai-compatible"
DEFAULT_OPENAI_API_BASE = "https://api.openai.com/v1/chat/completions"
DEFAULT_ANTHROPIC_API_BASE = "https://api.anthropic.com/v1/messages"
SUPPORTED_PROVIDERS = {"openai-compatible", "anthropic"}
MANIFEST_VERSION = 1
DEFAULT_CONCURRENCY = 2
DEFAULT_REQUESTS_PER_MINUTE = 20

INTERACTION_TYPE_MAP = {
    "liked": "linkedin_engagement",
    "commented": "linkedin_comment",
    "shared": "linkedin_share",
    "posted": "linkedin_post",
    "reacted": "linkedin_engagement",
}

TITLE_MAP = {
    r"\bvice\s+president\b": "VP",
    r"\bsvp\b": "SVP",
    r"\bevp\b": "EVP",
    r"\bceo\b": "CEO",
    r"\bcto\b": "CTO",
    r"\bcpo\b": "CPO",
    r"\bcmo\b": "CMO",
    r"\bcoo\b": "COO",
    r"\bcfo\b": "CFO",
    r"\bmd\b": "Managing Director",
    r"\bdir\b\.?": "Director",
    r"\bmgr\b\.?": "Manager",
    r"\bsr\.?\s+": "Senior ",
    r"\bjr\.?\s+": "Junior ",
}

SENIORITY_RULES = [
    ("executive", [r"\bceo\b", r"\bc[otfp]o\b", r"\bfounder\b", r"\bco[- ]founder\b", r"\bpresident\b"]),
    ("vp_head", [r"\bvp\b", r"\bhead of\b", r"\bmanaging director\b"]),
    ("director", [r"\bdirector\b", r"\bexecutive producer\b"]),
    ("manager_lead", [r"\bmanager\b", r"\blead\b", r"\bprincipal\b"]),
    ("junior", [r"\bintern\b", r"\bjunior\b", r"\bassistant\b", r"\bcoordinator\b", r"\banalyst\b"]),
]

SYSTEM_STAGE1_TEMPLATE = """You are a structured evidence extraction engine for {product_name} - {product_tagline}.

Your job is to analyze a lead signal and extract structured reasoning evidence. You are NOT making a qualification decision here. You are extracting facts, classifying signals, and identifying unknowns.

## Output Contract
You MUST return ONLY a single valid JSON object. No explanation, no markdown, no prose before or after.

{icp_section}

{signals_section}

{disqualification_rules_section}

{disqualification_section}

{evidence_rules_section}

## Required JSON Output Schema
{{
  "lead_id": "string",
  "segment": "string",
  "candidate_use_case": "string",
  "signal_strength": "high|medium|medium-low|low",
  "observed_evidence": ["string"],
  "inferred_evidence": ["string"],
  "unknowns": ["string"],
  "hard_disqualifiers": ["string"]
}}

Return ONLY this JSON object. Nothing else."""

SYSTEM_STAGE2_TEMPLATE = """You are a deterministic lead qualification scoring engine for {product_name} - {product_tagline}.

Your input is ONLY the structured evidence object from Stage 1. You apply a scoring model and return a qualification decision.

## Output Contract
You MUST return ONLY a single valid JSON object. No explanation, no markdown, no prose before or after.

{qualification_section}

## Decision Rules (Python enforces these, but you must follow them)
- score >= {qualified_threshold} -> "qualified"
- score < {disqualified_threshold} -> "disqualified"
- {disqualified_threshold} <= score < {qualified_threshold} -> "review"
- If hard_disqualifiers is non-empty -> "disqualified" (override, score irrelevant)

## Required JSON Output Schema
{{
  "lead_id": "string",
  "decision": "qualified|disqualified|review",
  "score": 0,
  "confidence": "high|medium|low",
  "segment_fit": 0,
  "workflow_fit": 0,
  "pain_fit": 0,
  "buyer_fit": 0,
  "commercial_fit": 0,
  "qualification_reasons": ["string"],
  "disqualification_reasons": ["string"],
  "reasoning_summary": "string",
  "suggested_use_case": "string"
}}

Rules:
- score MUST equal segment_fit + workflow_fit + pain_fit + buyer_fit + commercial_fit
- segment_fit max {w_segment}, buyer_fit max {w_buyer}, pain_fit max {w_pain}, workflow_fit max {w_workflow}, commercial_fit max {w_commercial}
- decision MUST match score thresholds above
- reasoning_summary: 2-3 sentences, factual, reference specific evidence
- qualification_reasons and disqualification_reasons: short evidence-based bullets

Return ONLY this JSON object. Nothing else."""


@dataclass
class Campaign:
    campaign_id: str
    name: str
    tagline: str
    score_weights: dict[str, int]
    qualified_threshold: int
    disqualified_threshold: int
    stage1_system_prompt: str
    stage2_system_prompt: str


@dataclass
class LeadInput:
    lead_id: str
    person_name: str
    title: str
    company_name: str
    company_domain: str
    company_size: Optional[str]
    linkedin_url: Optional[str]
    signal_text: str
    signal_type: str
    signal_timestamp: str


@dataclass
class EnrichedLead:
    lead: dict[str, Any]
    company_domain: str
    title_seniority: str
    interaction_label: str
    website_summary: str
    website_title: str
    csv_context: dict[str, Any]


class RetryableAPIError(RuntimeError):
    pass


class NonRetryableAPIError(RuntimeError):
    pass


class RequestRateLimiter:
    def __init__(self, requests_per_minute: int):
        self.requests_per_minute = max(0, int(requests_per_minute))
        self.min_interval_seconds = 0.0
        if self.requests_per_minute > 0:
            self.min_interval_seconds = 60.0 / float(self.requests_per_minute)
        self._lock = threading.Lock()
        self._next_allowed_at = 0.0

    def wait(self) -> None:
        if self.min_interval_seconds <= 0:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                if now >= self._next_allowed_at:
                    self._next_allowed_at = now + self.min_interval_seconds
                    return
                delay = self._next_allowed_at - now
            time.sleep(delay)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def load_campaign(campaign_id: str) -> Campaign:
    root = Path(__file__).resolve().parents[1] / "campaigns"
    campaign_dir = root / campaign_id
    common_dir = root / "common"
    if not campaign_dir.exists():
        raise FileNotFoundError(f"Campaign not found in skill package: {campaign_id}")

    cfg = json.loads(_read_text(campaign_dir / "config.json") or "{}")
    name = cfg.get("name", campaign_id.capitalize())
    tagline = cfg.get("tagline", "AI video generation platform")
    weights = cfg.get(
        "score_weights",
        {
            "segment_fit": 30,
            "buyer_fit": 25,
            "pain_fit": 20,
            "workflow_fit": 15,
            "commercial_fit": 10,
        },
    )
    qualified_threshold = int(cfg.get("qualified_threshold", 70))
    disqualified_threshold = int(cfg.get("disqualified_threshold", 60))

    stage1 = SYSTEM_STAGE1_TEMPLATE.format(
        product_name=name,
        product_tagline=tagline,
        icp_section=_read_text(campaign_dir / "icp.md").strip(),
        signals_section=_read_text(campaign_dir / "signals.md").strip(),
        disqualification_rules_section=_read_text(common_dir / "disqualification-rules.md").strip(),
        disqualification_section=_read_text(campaign_dir / "disqualification.md").strip(),
        evidence_rules_section=_read_text(common_dir / "evidence-extraction.md").strip(),
    )
    stage2 = SYSTEM_STAGE2_TEMPLATE.format(
        product_name=name,
        product_tagline=tagline,
        qualification_section=_read_text(campaign_dir / "qualification.md").strip(),
        qualified_threshold=qualified_threshold,
        disqualified_threshold=disqualified_threshold,
        w_segment=weights.get("segment_fit", 30),
        w_buyer=weights.get("buyer_fit", 25),
        w_pain=weights.get("pain_fit", 20),
        w_workflow=weights.get("workflow_fit", 15),
        w_commercial=weights.get("commercial_fit", 10),
    )
    return Campaign(
        campaign_id=campaign_id,
        name=name,
        tagline=tagline,
        score_weights=weights,
        qualified_threshold=qualified_threshold,
        disqualified_threshold=disqualified_threshold,
        stage1_system_prompt=stage1,
        stage2_system_prompt=stage2,
    )


def _decode_csv_text(file_path: Path) -> tuple[str, str]:
    raw_bytes = file_path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw_bytes.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("utf-8", raw_bytes, 0, 1, "unable to decode CSV")


def _make_lead_id(name: str, company: str, index: int) -> str:
    raw = f"{name}_{company}_{index}"
    return re.sub(r"[^a-z0-9_]", "_", raw.lower().strip())[:64]


def _build_signal_text(row: dict[str, str]) -> str:
    parts: list[str] = []
    name = row.get("NAME", "").strip()
    title = row.get("JOB TITLE", "").strip()
    company = row.get("COMPANY", "").strip()
    interaction = row.get("INTERACTION", "").strip()
    post_url = row.get("POST URL", "").strip()
    icp_note = row.get("ICP SCORE", "").strip()
    industry = row.get("INDUSTRY", "").strip()
    size = row.get("COMPANY SIZE", "").strip()
    location = row.get("LOCATION", "").strip()
    hq = row.get("HQ COMPANY LOCATION", "").strip()
    if interaction and company:
        parts.append(
            f"{name} ({title} at {company}) {interaction.lower()} a LinkedIn post"
            + (f" (Post: {post_url})" if post_url else "")
            + "."
        )
    if icp_note:
        parts.append(f"Trigify qualification note: {icp_note}")
    firm_parts: list[str] = []
    if industry:
        firm_parts.append(f"industry: {industry}")
    if size:
        firm_parts.append(f"company size: {size} employees")
    if hq and hq != location:
        firm_parts.append(f"HQ: {hq}")
    elif hq:
        firm_parts.append(f"location: {hq}")
    if firm_parts:
        parts.append("Company details - " + ", ".join(firm_parts) + ".")
    return " ".join(parts).strip()


def load_csv(file_path: Path) -> list[tuple[LeadInput, dict[str, str]]]:
    csv_text, _encoding = _decode_csv_text(file_path)
    reader = csv.DictReader(io.StringIO(csv_text, newline=""))
    run_timestamp = datetime.now(timezone.utc).isoformat()
    results: list[tuple[LeadInput, dict[str, str]]] = []
    for index, row in enumerate(reader, start=1):
        name = (row.get("NAME") or "").strip()
        company = (row.get("COMPANY") or "").strip()
        if not name or not company:
            continue
        interaction = (row.get("INTERACTION") or "").strip().lower()
        signal_type = INTERACTION_TYPE_MAP.get(interaction, "linkedin_engagement")
        raw = {
            "lead_id": _make_lead_id(name, company, index),
            "person_name": name,
            "title": row.get("JOB TITLE", "").strip(),
            "company_name": company,
            "company_domain": row.get("COMPANY URL", "").strip(),
            "company_size": row.get("COMPANY SIZE", "").strip() or None,
            "linkedin_url": row.get("LINKEDIN PROFILE", "").strip() or None,
            "signal_text": _build_signal_text(row),
            "signal_type": signal_type,
            "signal_timestamp": run_timestamp,
        }
        results.append((normalize_lead(raw), row))
    return results


def _normalize_domain(value: str) -> str:
    value = str(value or "").strip().lower()
    value = re.sub(r"^https?://", "", value)
    value = re.sub(r"^www\.", "", value)
    return value.rstrip("/")


def _normalize_title(value: str) -> str:
    lower = str(value or "").strip().lower()
    for pattern, replacement in TITLE_MAP.items():
        lower = re.sub(pattern, replacement.lower(), lower, flags=re.IGNORECASE)
    return " ".join(word.capitalize() for word in lower.split())


def normalize_lead(raw: dict[str, Any]) -> LeadInput:
    signal_text = str(raw.get("signal_text") or "").strip()
    if not signal_text:
        raise ValueError("signal_text is required")
    linkedin = raw.get("linkedin_url")
    if linkedin:
        linkedin = str(linkedin).strip()
        if linkedin and not linkedin.startswith("http"):
            linkedin = f"https://{linkedin}"
    return LeadInput(
        lead_id=str(raw.get("lead_id", "")).strip(),
        person_name=str(raw.get("person_name", "")).strip(),
        title=_normalize_title(raw.get("title", "")),
        company_name=str(raw.get("company_name", "")).strip(),
        company_domain=_normalize_domain(raw.get("company_domain", "")),
        company_size=(str(raw.get("company_size", "")).strip() or None),
        linkedin_url=linkedin or None,
        signal_text=signal_text,
        signal_type=str(raw.get("signal_type", "")).strip(),
        signal_timestamp=str(raw.get("signal_timestamp", "")).strip(),
    )


def infer_title_seniority(title: str) -> str:
    text = title.lower()
    for label, patterns in SENIORITY_RULES:
        for pattern in patterns:
            if re.search(pattern, text):
                return label
    if not text:
        return "unknown"
    return "individual_contributor"


def fetch_website_summary(domain: str, timeout: int = 8) -> tuple[str, str]:
    if not domain:
        return "", ""
    urls = []
    if domain.startswith("http://") or domain.startswith("https://"):
        urls.append(domain)
    else:
        urls.extend([f"https://{domain}", f"http://{domain}"])
    for url in urls:
        try:
            req = Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 LeadGenCortexSkill/1.0"},
                method="GET",
            )
            with urlopen(req, timeout=timeout) as resp:
                content_type = resp.headers.get("Content-Type", "")
                if "text/html" not in content_type:
                    continue
                html = resp.read(150_000).decode("utf-8", errors="ignore")
            title_match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
            desc_match = re.search(
                r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
                html,
                flags=re.IGNORECASE | re.DOTALL,
            )
            title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else ""
            desc = re.sub(r"\s+", " ", desc_match.group(1)).strip() if desc_match else ""
            return title[:180], desc[:500]
        except (HTTPError, URLError, TimeoutError, ValueError):
            continue
    return "", ""


def enrich_lead(lead: LeadInput, raw_row: dict[str, str], allow_web: bool) -> EnrichedLead:
    website_title = ""
    website_summary = ""
    if allow_web and lead.company_domain:
        website_title, website_summary = fetch_website_summary(lead.company_domain)
    return EnrichedLead(
        lead=asdict(lead),
        company_domain=lead.company_domain,
        title_seniority=infer_title_seniority(lead.title),
        interaction_label=(raw_row.get("INTERACTION") or "").strip().lower(),
        website_summary=website_summary,
        website_title=website_title,
        csv_context={
            "industry": (raw_row.get("INDUSTRY") or "").strip(),
            "company_size": (raw_row.get("COMPANY SIZE") or "").strip(),
            "icp_note": (raw_row.get("ICP SCORE") or "").strip(),
            "location": (raw_row.get("LOCATION") or "").strip(),
            "hq_company_location": (raw_row.get("HQ COMPANY LOCATION") or "").strip(),
            "post_url": (raw_row.get("POST URL") or "").strip(),
        },
    )


def build_messages(system_prompt: str, payload: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(payload, indent=2, ensure_ascii=False)},
    ]


def _extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if not text:
        raise ValueError("LLM returned empty content")
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        data = json.loads(text[start : end + 1])
        if isinstance(data, dict):
            return data
    raise ValueError("LLM response did not contain a valid JSON object")


def _extract_text_from_openai(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if not choices:
        raise NonRetryableAPIError(f"LLM returned no choices: {payload}")
    message = (choices[0] or {}).get("message") or {}
    content = message.get("content", "")
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        content = "".join(parts)
    if not isinstance(content, str):
        raise NonRetryableAPIError(f"Unexpected content type from LLM: {type(content)!r}")
    return content


def _extract_text_from_anthropic(payload: dict[str, Any]) -> str:
    content = payload.get("content") or []
    if not isinstance(content, list):
        raise NonRetryableAPIError(f"Unexpected Anthropic payload shape: {payload}")
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(item.get("text", ""))
    if not parts:
        raise NonRetryableAPIError(f"Anthropic returned no text blocks: {payload}")
    return "".join(parts)


def _post_json(url: str, headers: dict[str, str], body: dict[str, Any], timeout: int) -> dict[str, Any]:
    body = {
        **body,
    }
    req = Request(
        url,
        headers=headers,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        if exc.code in {408, 409, 425, 429, 500, 502, 503, 504}:
            raise RetryableAPIError(f"LLM HTTP error {exc.code}: {detail}") from exc
        raise NonRetryableAPIError(f"LLM HTTP error {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RetryableAPIError(f"LLM connection error: {exc}") from exc


def _anthropic_request_body(messages: list[dict[str, str]], model: str) -> dict[str, Any]:
    system_parts = [msg["content"] for msg in messages if msg.get("role") == "system"]
    user_messages = [
        {"role": msg["role"], "content": msg["content"]}
        for msg in messages
        if msg.get("role") in {"user", "assistant"}
    ]
    if not user_messages:
        raise NonRetryableAPIError("Anthropic request requires at least one user/assistant message")
    return {
        "model": model,
        "max_tokens": 4096,
        "temperature": 0,
        "system": "\n\n".join(system_parts).strip(),
        "messages": user_messages,
    }


def _openai_request_body(messages: list[dict[str, str]], model: str) -> dict[str, Any]:
    return {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }


def chat_completion(
    messages: list[dict[str, str]],
    *,
    provider: str,
    model: str,
    api_key: str,
    api_base: str,
    timeout: int = 120,
    max_retries: int = 3,
    rate_limiter: Optional[RequestRateLimiter] = None,
) -> dict[str, Any]:
    provider = provider.strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise NonRetryableAPIError(f"Unsupported provider: {provider}")

    attempt = 0
    while True:
        attempt += 1
        try:
            if rate_limiter is not None:
                rate_limiter.wait()
            if provider == "anthropic":
                payload = _post_json(
                    api_base,
                    headers={
                        "Content-Type": "application/json",
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                    },
                    body=_anthropic_request_body(messages, model),
                    timeout=timeout,
                )
                return _extract_json_object(_extract_text_from_anthropic(payload))

            payload = _post_json(
                api_base,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                body=_openai_request_body(messages, model),
                timeout=timeout,
            )
            return _extract_json_object(_extract_text_from_openai(payload))
        except RetryableAPIError:
            if attempt > max_retries:
                raise
            sleep_seconds = min(20.0, (2 ** (attempt - 1)) + random.uniform(0, 0.5))
            time.sleep(sleep_seconds)


def sanitize_stage1(data: dict[str, Any], lead_id: str) -> dict[str, Any]:
    signal_strength = str(data.get("signal_strength") or "medium-low").strip().lower()
    if signal_strength not in {"high", "medium", "medium-low", "low"}:
        signal_strength = "medium-low"
    return {
        "lead_id": str(data.get("lead_id") or lead_id),
        "segment": str(data.get("segment") or "unknown").strip(),
        "candidate_use_case": str(data.get("candidate_use_case") or "").strip(),
        "signal_strength": signal_strength,
        "observed_evidence": [str(x).strip() for x in (data.get("observed_evidence") or []) if str(x).strip()],
        "inferred_evidence": [str(x).strip() for x in (data.get("inferred_evidence") or []) if str(x).strip()],
        "unknowns": [str(x).strip() for x in (data.get("unknowns") or []) if str(x).strip()],
        "hard_disqualifiers": [str(x).strip() for x in (data.get("hard_disqualifiers") or []) if str(x).strip()],
    }


def validate_stage1_contract(data: dict[str, Any]) -> None:
    required = {
        "lead_id": str,
        "segment": str,
        "candidate_use_case": str,
        "signal_strength": str,
        "observed_evidence": list,
        "inferred_evidence": list,
        "unknowns": list,
        "hard_disqualifiers": list,
    }
    for key, expected_type in required.items():
        if key not in data:
            raise ValueError(f"Stage 1 output missing required key: {key}")
        if not isinstance(data[key], expected_type):
            raise ValueError(f"Stage 1 key '{key}' must be {expected_type.__name__}")


def _coerce_int(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def sanitize_stage2(data: dict[str, Any], lead_id: str) -> dict[str, Any]:
    return {
        "lead_id": str(data.get("lead_id") or lead_id),
        "decision": str(data.get("decision") or "review").strip().lower(),
        "score": _coerce_int(data.get("score")),
        "confidence": str(data.get("confidence") or "medium").strip().lower(),
        "segment_fit": _coerce_int(data.get("segment_fit")),
        "workflow_fit": _coerce_int(data.get("workflow_fit")),
        "pain_fit": _coerce_int(data.get("pain_fit")),
        "buyer_fit": _coerce_int(data.get("buyer_fit")),
        "commercial_fit": _coerce_int(data.get("commercial_fit")),
        "qualification_reasons": [str(x).strip() for x in (data.get("qualification_reasons") or []) if str(x).strip()],
        "disqualification_reasons": [str(x).strip() for x in (data.get("disqualification_reasons") or []) if str(x).strip()],
        "reasoning_summary": str(data.get("reasoning_summary") or "").strip(),
        "suggested_use_case": str(data.get("suggested_use_case") or "").strip(),
    }


def validate_stage2_contract(data: dict[str, Any]) -> None:
    required = {
        "lead_id": str,
        "decision": str,
        "score": (int, float, str),
        "confidence": str,
        "segment_fit": (int, float, str),
        "workflow_fit": (int, float, str),
        "pain_fit": (int, float, str),
        "buyer_fit": (int, float, str),
        "commercial_fit": (int, float, str),
        "qualification_reasons": list,
        "disqualification_reasons": list,
        "reasoning_summary": str,
        "suggested_use_case": str,
    }
    for key, expected_type in required.items():
        if key not in data:
            raise ValueError(f"Stage 2 output missing required key: {key}")
        if not isinstance(data[key], expected_type):
            if isinstance(expected_type, tuple):
                names = ", ".join(t.__name__ for t in expected_type)
                raise ValueError(f"Stage 2 key '{key}' must be one of: {names}")
            raise ValueError(f"Stage 2 key '{key}' must be {expected_type.__name__}")


def validate_and_route(
    evidence: dict[str, Any], decision: dict[str, Any], campaign: Campaign
) -> tuple[dict[str, Any], str]:
    weights = campaign.score_weights
    decision["segment_fit"] = min(max(decision["segment_fit"], 0), int(weights.get("segment_fit", 30)))
    decision["buyer_fit"] = min(max(decision["buyer_fit"], 0), int(weights.get("buyer_fit", 25)))
    decision["pain_fit"] = min(max(decision["pain_fit"], 0), int(weights.get("pain_fit", 20)))
    decision["workflow_fit"] = min(max(decision["workflow_fit"], 0), int(weights.get("workflow_fit", 15)))
    decision["commercial_fit"] = min(max(decision["commercial_fit"], 0), int(weights.get("commercial_fit", 10)))
    decision["score"] = (
        decision["segment_fit"]
        + decision["buyer_fit"]
        + decision["pain_fit"]
        + decision["workflow_fit"]
        + decision["commercial_fit"]
    )

    if evidence.get("hard_disqualifiers"):
        routing = "disqualified"
    elif evidence.get("signal_strength") == "low":
        routing = "disqualified"
    elif decision["score"] >= campaign.qualified_threshold:
        routing = "qualified"
    elif decision["score"] < campaign.disqualified_threshold:
        routing = "disqualified"
    else:
        routing = "review"
    decision["decision"] = routing
    return decision, routing


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _result_fields() -> list[str]:
    return [
        "lead_id",
        "person_name",
        "title",
        "company_name",
        "company_domain",
        "company_size",
        "linkedin_url",
        "decision",
        "score",
        "confidence",
        "segment",
        "signal_strength",
        "candidate_use_case",
        "suggested_use_case",
        "qualification_reasons",
        "disqualification_reasons",
        "reasoning_summary",
    ]


def _error_fields() -> list[str]:
    return [
        "lead_id",
        "person_name",
        "company_name",
        "status",
        "attempts",
        "error_type",
        "error_message",
        "last_failed_at",
    ]


def build_export_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "lead_id": row["lead"]["lead_id"],
        "person_name": row["lead"]["person_name"],
        "title": row["lead"]["title"],
        "company_name": row["lead"]["company_name"],
        "company_domain": row["lead"]["company_domain"],
        "company_size": row["lead"]["company_size"] or "",
        "linkedin_url": row["lead"]["linkedin_url"] or "",
        "decision": row["routing"],
        "score": row["decision"]["score"],
        "confidence": row["decision"]["confidence"],
        "segment": row["evidence"]["segment"],
        "signal_strength": row["evidence"]["signal_strength"],
        "candidate_use_case": row["evidence"]["candidate_use_case"],
        "suggested_use_case": row["decision"]["suggested_use_case"],
        "qualification_reasons": " | ".join(row["decision"]["qualification_reasons"]),
        "disqualification_reasons": " | ".join(row["decision"]["disqualification_reasons"]),
        "reasoning_summary": row["decision"]["reasoning_summary"],
    }


def artifact_paths(output_dir: Path, lead_id: str) -> dict[str, Path]:
    artifact_dir = output_dir / "artifacts"
    return {
        "input": artifact_dir / f"{lead_id}_input.json",
        "enrichment": artifact_dir / f"{lead_id}_enrichment.json",
        "evidence": artifact_dir / f"{lead_id}_evidence.json",
        "decision": artifact_dir / f"{lead_id}_decision.json",
    }


def write_lead_artifacts(output_dir: Path, row: dict[str, Any]) -> None:
    paths = artifact_paths(output_dir, row["lead"]["lead_id"])
    write_json(paths["input"], row["lead"])
    write_json(paths["enrichment"], row["enrichment"])
    write_json(paths["evidence"], row["evidence"])
    write_json(paths["decision"], row["decision"])


def _manifest_path(output_dir: Path) -> Path:
    return output_dir / "run_manifest.json"


def _config_snapshot_path(output_dir: Path) -> Path:
    return output_dir / "config_snapshot.json"


def _update_manifest_counts(manifest: dict[str, Any]) -> None:
    leads = manifest.get("leads", {})
    counts = {"pending": 0, "running": 0, "done": 0, "failed": 0}
    routing = {"qualified": 0, "review": 0, "disqualified": 0}
    for state in leads.values():
        status = state.get("status", "pending")
        if status in counts:
            counts[status] += 1
        if status == "done":
            result_row = state.get("result_row") or {}
            decision = result_row.get("decision")
            if decision in routing:
                routing[decision] += 1
    manifest["counts"] = {
        **counts,
        **routing,
        "total": len(leads),
    }
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat()


def write_manifest(output_dir: Path, manifest: dict[str, Any]) -> None:
    _update_manifest_counts(manifest)
    write_json(_manifest_path(output_dir), manifest)


def load_manifest(output_dir: Path) -> Optional[dict[str, Any]]:
    path = _manifest_path(output_dir)
    if not path.exists():
        return None
    return read_json(path)


def make_config_snapshot(args: argparse.Namespace, input_path: Path, api_base: str, campaign: Campaign) -> dict[str, Any]:
    return {
        "manifest_version": MANIFEST_VERSION,
        "input_path": str(input_path),
        "campaign_id": campaign.campaign_id,
        "campaign_name": campaign.name,
        "provider": str(args.provider).strip().lower(),
        "model": args.model,
        "api_base": api_base,
        "disable_web_enrichment": bool(args.disable_web_enrichment),
        "limit": int(args.limit or 0),
        "sleep_ms": int(args.sleep_ms or 0),
        "timeout_seconds": int(args.timeout_seconds),
        "max_retries": int(args.max_retries),
        "concurrency": int(args.concurrency),
        "requests_per_minute": int(args.requests_per_minute),
        "qualified_threshold": campaign.qualified_threshold,
        "disqualified_threshold": campaign.disqualified_threshold,
        "score_weights": campaign.score_weights,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def init_or_resume_manifest(
    output_dir: Path,
    config_snapshot: dict[str, Any],
    lead_rows: list[tuple[LeadInput, dict[str, str]]],
) -> dict[str, Any]:
    ensure_dir(output_dir)
    ensure_dir(output_dir / "artifacts")
    existing = load_manifest(output_dir)
    if existing is None:
        manifest = {
            "manifest_version": MANIFEST_VERSION,
            "status": "running",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "config": config_snapshot,
            "leads": {},
        }
    else:
        existing_config = existing.get("config") or {}
        for key in ("input_path", "campaign_id", "provider", "model", "api_base"):
            if existing_config.get(key) != config_snapshot.get(key):
                raise RuntimeError(
                    f"Existing run manifest does not match current configuration for '{key}'. "
                    "Use a new output directory for a fresh run."
                )
        manifest = existing
        manifest["status"] = "running"
        manifest["config"] = config_snapshot

    leads = manifest.setdefault("leads", {})
    for index, (lead, _raw_row) in enumerate(lead_rows, start=1):
        state = leads.get(lead.lead_id) or {}
        state.setdefault("index", index)
        state.setdefault("lead_id", lead.lead_id)
        state.setdefault("person_name", lead.person_name)
        state.setdefault("company_name", lead.company_name)
        state.setdefault("status", "pending")
        state.setdefault("attempts", 0)
        leads[lead.lead_id] = state
    write_json(_config_snapshot_path(output_dir), config_snapshot)
    write_manifest(output_dir, manifest)
    return manifest


def materialize_outputs(output_dir: Path, manifest: dict[str, Any]) -> None:
    ensure_dir(output_dir)
    ensure_dir(output_dir / "artifacts")
    fields = _result_fields()
    all_path = output_dir / "all_scored_leads.csv"
    qualified_path = output_dir / "qualified_leads.csv"
    review_path = output_dir / "review_leads.csv"
    disqualified_path = output_dir / "disqualified_leads.csv"
    errors_path = output_dir / "errors.csv"
    lead_states = sorted(
        manifest.get("leads", {}).values(),
        key=lambda state: (int(state.get("index", 0)), str(state.get("lead_id", ""))),
    )
    with all_path.open("w", newline="", encoding="utf-8") as all_fp, \
         qualified_path.open("w", newline="", encoding="utf-8") as q_fp, \
         review_path.open("w", newline="", encoding="utf-8") as r_fp, \
         disqualified_path.open("w", newline="", encoding="utf-8") as d_fp, \
         errors_path.open("w", newline="", encoding="utf-8") as e_fp:
        writers = {
            "all": csv.DictWriter(all_fp, fieldnames=fields),
            "qualified": csv.DictWriter(q_fp, fieldnames=fields),
            "review": csv.DictWriter(r_fp, fieldnames=fields),
            "disqualified": csv.DictWriter(d_fp, fieldnames=fields),
            "errors": csv.DictWriter(e_fp, fieldnames=_error_fields()),
        }
        for writer in writers.values():
            writer.writeheader()
        for state in lead_states:
            status = state.get("status")
            if status == "done":
                export_row = state.get("result_row") or {}
                writers["all"].writerow(export_row)
                if export_row.get("decision") in {"qualified", "review", "disqualified"}:
                    writers[export_row["decision"]].writerow(export_row)
            elif status == "failed":
                writers["errors"].writerow({
                    "lead_id": state.get("lead_id", ""),
                    "person_name": state.get("person_name", ""),
                    "company_name": state.get("company_name", ""),
                    "status": status,
                    "attempts": state.get("attempts", 0),
                    "error_type": state.get("error_type", ""),
                    "error_message": state.get("error_message", ""),
                    "last_failed_at": state.get("last_failed_at", ""),
                })

    counts = manifest.get("counts", {})
    summary = {
        "lead_count": counts.get("total", 0),
        "qualified": counts.get("qualified", 0),
        "review": counts.get("review", 0),
        "disqualified": counts.get("disqualified", 0),
        "failed": counts.get("failed", 0),
        "pending": counts.get("pending", 0),
        "running": counts.get("running", 0),
        "status": manifest.get("status", "running"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    write_json(output_dir / "summary.json", summary)
    write_json(output_dir / "quality_report.json", build_quality_report(manifest))


def build_quality_report(manifest: dict[str, Any]) -> dict[str, Any]:
    lead_states = list((manifest.get("leads") or {}).values())
    done_states = [state for state in lead_states if state.get("status") == "done"]
    scores: list[int] = []
    low_signal_count = 0
    unknown_segment_count = 0
    for state in done_states:
        result_row = state.get("result_row") or {}
        try:
            scores.append(int(result_row.get("score", 0)))
        except Exception:
            pass
        evidence = state.get("evidence_snapshot") or {}
        if evidence.get("signal_strength") == "low":
            low_signal_count += 1
        if str(result_row.get("segment", "")).strip().lower() in {"", "unknown"}:
            unknown_segment_count += 1

    total_done = len(done_states)
    counts = manifest.get("counts", {})
    avg_score = round(sum(scores) / total_done, 2) if total_done else 0.0
    warnings: list[str] = []
    if counts.get("failed", 0) > 0:
        warnings.append(f"{counts.get('failed', 0)} leads failed and require review")
    if total_done and counts.get("qualified", 0) / total_done > 0.75:
        warnings.append("Qualified rate is unusually high; review campaign thresholds and scoring drift")
    if total_done and counts.get("disqualified", 0) / total_done > 0.9:
        warnings.append("Disqualified rate is unusually high; review signal quality or campaign fit")
    if total_done and unknown_segment_count / total_done > 0.4:
        warnings.append("Too many leads were scored with unknown segment classification")

    return {
        "status": manifest.get("status", "unknown"),
        "counts": counts,
        "average_score": avg_score,
        "low_signal_done_count": low_signal_count,
        "unknown_segment_done_count": unknown_segment_count,
        "warnings": warnings,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def score_one_lead(
    lead: LeadInput,
    raw_row: dict[str, str],
    *,
    campaign: Campaign,
    provider: str,
    model: str,
    api_key: str,
    api_base: str,
    timeout_seconds: int,
    max_retries: int,
    allow_web: bool,
    rate_limiter: Optional[RequestRateLimiter],
) -> dict[str, Any]:
    enriched = enrich_lead(lead, raw_row, allow_web=allow_web)
    stage1_payload = {"lead": asdict(lead), "enrichment": asdict(enriched)}
    raw_stage1 = chat_completion(
        build_messages(campaign.stage1_system_prompt, stage1_payload),
        provider=provider,
        model=model,
        api_key=api_key,
        api_base=api_base,
        timeout=timeout_seconds,
        max_retries=max_retries,
        rate_limiter=rate_limiter,
    )
    validate_stage1_contract(raw_stage1)
    evidence = sanitize_stage1(
        raw_stage1,
        lead.lead_id,
    )
    raw_stage2 = chat_completion(
        build_messages(campaign.stage2_system_prompt, evidence),
        provider=provider,
        model=model,
        api_key=api_key,
        api_base=api_base,
        timeout=timeout_seconds,
        max_retries=max_retries,
        rate_limiter=rate_limiter,
    )
    validate_stage2_contract(raw_stage2)
    decision = sanitize_stage2(
        raw_stage2,
        lead.lead_id,
    )
    decision, routing = validate_and_route(evidence, decision, campaign)
    return {
        "lead": asdict(lead),
        "enrichment": asdict(enriched),
        "evidence": evidence,
        "decision": decision,
        "routing": routing,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Score a Trigify CSV with 2 direct LLM calls per lead.")
    parser.add_argument("--input", required=True, help="Path to the CSV file")
    parser.add_argument("--output-dir", required=True, help="Directory to write scored outputs")
    parser.add_argument("--campaign", default="seedance", help="Bundled campaign id to use")
    parser.add_argument("--provider", default=os.environ.get("LLM_PROVIDER", DEFAULT_PROVIDER), help="LLM provider: anthropic or openai-compatible")
    parser.add_argument("--limit", type=int, default=0, help="Optional limit on number of rows to process")
    parser.add_argument("--sleep-ms", type=int, default=0, help="Optional pause between leads")
    parser.add_argument("--concurrency", type=int, default=int(os.environ.get("LLM_CONCURRENCY", str(DEFAULT_CONCURRENCY))), help="Maximum number of leads to process concurrently")
    parser.add_argument("--requests-per-minute", type=int, default=int(os.environ.get("LLM_REQUESTS_PER_MINUTE", str(DEFAULT_REQUESTS_PER_MINUTE))), help="Global LLM request cap across all workers; 0 disables throttling")
    parser.add_argument("--timeout-seconds", type=int, default=int(os.environ.get("LLM_TIMEOUT_SECONDS", "120")), help="Per-request LLM timeout")
    parser.add_argument("--max-retries", type=int, default=int(os.environ.get("LLM_MAX_RETRIES", "3")), help="Retries for transient LLM failures")
    parser.add_argument("--disable-web-enrichment", action="store_true", help="Skip best-effort website fetch")
    parser.add_argument("--api-base", default=os.environ.get("LLM_API_BASE", ""), help="Provider API base URL; optional when using defaults")
    parser.add_argument("--model", default=os.environ.get("LLM_MODEL", ""), help="LLM model name")
    parser.add_argument("--api-key", default=os.environ.get("LLM_API_KEY", ""), help="LLM API key")
    return parser.parse_args(argv)


def _default_api_base(provider: str) -> str:
    if provider == "anthropic":
        return DEFAULT_ANTHROPIC_API_BASE
    return DEFAULT_OPENAI_API_BASE


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
    if args.max_retries < 0:
        print("--max-retries must be >= 0", file=sys.stderr)
        return 2
    if args.concurrency <= 0:
        print("--concurrency must be > 0", file=sys.stderr)
        return 2
    if args.requests_per_minute < 0:
        print("--requests-per-minute must be >= 0", file=sys.stderr)
        return 2
    if args.timeout_seconds <= 0:
        print("--timeout-seconds must be > 0", file=sys.stderr)
        return 2

    input_path = Path(args.input).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    if not input_path.exists():
        print(f"Input CSV not found: {input_path}", file=sys.stderr)
        return 2
    api_base = (args.api_base or "").strip() or _default_api_base(provider)
    campaign = load_campaign(args.campaign)
    lead_rows = load_csv(input_path)
    if args.limit and args.limit > 0:
        lead_rows = lead_rows[: args.limit]
    config_snapshot = make_config_snapshot(args, input_path, api_base, campaign)
    manifest = init_or_resume_manifest(output_dir, config_snapshot, lead_rows)
    rate_limiter = RequestRateLimiter(args.requests_per_minute)

    had_failures = False
    pending_items: list[tuple[int, LeadInput, dict[str, str]]] = []
    for index, (lead, raw_row) in enumerate(lead_rows, start=1):
        lead_state = manifest["leads"][lead.lead_id]
        if lead_state.get("status") == "done":
            print(f"[{index}/{len(lead_rows)}] skipping completed {lead.lead_id} - {lead.person_name} @ {lead.company_name}")
            continue
        lead_state["attempts"] = int(lead_state.get("attempts", 0)) + 1
        lead_state["status"] = "running"
        lead_state["last_started_at"] = datetime.now(timezone.utc).isoformat()
        write_manifest(output_dir, manifest)
        pending_items.append((index, lead, raw_row))
        if args.sleep_ms > 0:
            time.sleep(args.sleep_ms / 1000.0)

    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        future_map = {
            executor.submit(
                score_one_lead,
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
            ): (index, lead)
            for index, lead, raw_row in pending_items
        }
        for future in as_completed(future_map):
            index, lead = future_map[future]
            lead_state = manifest["leads"][lead.lead_id]
            try:
                row = future.result()
                write_lead_artifacts(output_dir, row)
                lead_state["status"] = "done"
                lead_state["completed_at"] = datetime.now(timezone.utc).isoformat()
                lead_state["error_type"] = ""
                lead_state["error_message"] = ""
                lead_state["last_failed_at"] = ""
                lead_state["result_row"] = build_export_row(row)
                lead_state["evidence_snapshot"] = row["evidence"]
                lead_state["decision_snapshot"] = row["decision"]
                print(f"[{index}/{len(lead_rows)}] completed {lead.lead_id} - {lead.person_name} @ {lead.company_name}")
            except Exception as exc:
                had_failures = True
                lead_state["status"] = "failed"
                lead_state["error_type"] = type(exc).__name__
                lead_state["error_message"] = str(exc)
                lead_state["last_failed_at"] = datetime.now(timezone.utc).isoformat()
                print(
                    f"[{index}/{len(lead_rows)}] failed {lead.lead_id} - {lead.person_name} @ {lead.company_name}: {exc}",
                    file=sys.stderr,
                )
            write_manifest(output_dir, manifest)
            materialize_outputs(output_dir, manifest)

    manifest["status"] = "completed_with_errors" if had_failures or manifest.get("counts", {}).get("failed", 0) else "completed"
    write_manifest(output_dir, manifest)
    materialize_outputs(output_dir, manifest)
    print(
        json.dumps(
            {
                "ok": manifest["status"] in {"completed", "completed_with_errors"},
                "output_dir": str(output_dir),
                "lead_count": manifest.get("counts", {}).get("total", 0),
                "failed": manifest.get("counts", {}).get("failed", 0),
                "status": manifest["status"],
            },
            indent=2,
        )
    )
    return 0 if manifest["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
