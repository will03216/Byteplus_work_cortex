"""
Tool enrichment layer.

Python orchestrates all tool calls — Claude never calls tools directly.

Enrichment sources (in order of richness):
  - Apollo.io          → company profile, funding, tech stack (via apollo_client)
  - Web scraping       → company homepage summary (see web_enrichment.py)
  - LinkedIn scraping  → public profile / tenure signals (see web_enrichment.py)
  - Local heuristic    → role seniority from title keywords
"""

import asyncio
import logging
from typing import Optional
from pydantic import BaseModel

from normalization.normalizer import LeadInput
from tools.web_enrichment import scrape_company_website, scrape_linkedin_profile
from tools.apollo_client import fetch_company

logger = logging.getLogger(__name__)


# ── Enrichment models ────────────────────────────────────────────────────────

class CompanyProfile(BaseModel):
    description:       Optional[str] = None
    industry:          Optional[str] = None
    company_size:      Optional[str] = None   # e.g. "51-200", "1001+"
    funding_stage:     Optional[str] = None   # e.g. "Series A", "Seed"
    total_funding_usd: Optional[int] = None   # raw USD integer


class RecentSignals(BaseModel):
    recent_activity:  list[str] = []
    hiring_signals:   list[str] = []
    product_updates:  list[str] = []


class EnrichedLead(BaseModel):
    lead:                    LeadInput
    company_profile:         CompanyProfile  = CompanyProfile()
    recent_signals:          RecentSignals   = RecentSignals()
    role_seniority:          str             = ""   # "senior" | "mid" | "junior" | "unknown"
    company_website_summary: str             = ""   # scraped homepage text
    linkedin_summary:        str             = ""   # scraped LinkedIn profile text


# ── Apollo response parsers ───────────────────────────────────────────────────

def _coerce_int(value) -> Optional[int]:
    """Best-effort numeric coercion for Apollo fields that may arrive as strings."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "")
        if not cleaned:
            return None
        try:
            return int(cleaned)
        except ValueError:
            try:
                return int(float(cleaned))
            except ValueError:
                return None
    return None

def _employee_range(count: Optional[int]) -> Optional[str]:
    if not count:
        return None
    if count <= 10:    return "1-10"
    if count <= 50:    return "11-50"
    if count <= 200:   return "51-200"
    if count <= 500:   return "201-500"
    if count <= 1000:  return "501-1000"
    return "1001+"


def _parse_company_profile(data: dict) -> CompanyProfile:
    if not data:
        return CompanyProfile()

    headcount = _coerce_int(data.get("employee_count") or data.get("estimated_num_employees"))
    total_funding = _coerce_int(data.get("total_funding"))

    return CompanyProfile(
        description       = data.get("short_description") or data.get("long_description") or None,
        industry          = data.get("industry") or None,
        company_size      = _employee_range(headcount),
        funding_stage     = data.get("latest_funding_round_type") or None,
        total_funding_usd = total_funding,
    )


def _parse_recent_signals(data: dict) -> RecentSignals:
    if not data:
        return RecentSignals()

    recent_activity: list[str] = []
    hiring_signals:  list[str] = []

    # Funding events → recent_activity
    for ev in (data.get("funding_events") or [])[:3]:
        parts = []
        round_type = ev.get("type") or ""
        amount     = _coerce_int(ev.get("amount"))
        date       = (ev.get("date") or "")[:7]   # YYYY-MM
        if round_type:
            parts.append(round_type)
        if amount:
            parts.append(f"${amount:,}")
        if date:
            parts.append(f"({date})")
        if parts:
            recent_activity.append("Funding: " + " ".join(parts))

    # Tech/product keywords → hiring_signals as context
    keywords = data.get("keywords") or []
    relevant = [
        k for k in keywords
        if any(w in k.lower() for w in ["ai", "video", "creative", "content", "animation", "media"])
    ]
    if relevant:
        hiring_signals.append("Company keywords: " + ", ".join(relevant[:6]))

    return RecentSignals(
        recent_activity = recent_activity,
        hiring_signals  = hiring_signals,
        product_updates = [],
    )


def get_role_seniority(title: str) -> str:
    """
    Lightweight local heuristic.
    Returns: "senior" | "mid" | "junior" | "unknown"
    """
    lower = title.lower()

    senior_keywords = [
        "vp", "vice president", "svp", "evp", "chief", "ceo", "cto", "cpo",
        "cmo", "coo", "cfo", "president", "founder", "co-founder", "partner",
        "managing director", "head of", "director", "principal", "staff",
    ]
    mid_keywords = [
        "manager", "lead", "senior", "sr.", "sr ", "specialist",
        "strategist", "consultant",
    ]
    junior_keywords = [
        "junior", "jr.", "jr ", "associate", "analyst", "coordinator",
        "intern", "assistant",
    ]

    for kw in senior_keywords:
        if kw in lower:
            return "senior"
    for kw in mid_keywords:
        if kw in lower:
            return "mid"
    for kw in junior_keywords:
        if kw in lower:
            return "junior"
    return "unknown"


# ── Orchestrator ─────────────────────────────────────────────────────────────

async def enrich_lead(lead: LeadInput) -> EnrichedLead:
    """
    Runs all enrichment concurrently — Apollo.io, website, and LinkedIn fetches
    happen in parallel via asyncio.gather. All sources are best-effort: any
    failure returns an empty model and the pipeline continues with whatever
    data is available.
    """
    role_seniority = get_role_seniority(lead.title)

    # All three network sources run in parallel
    apollo_data, company_website_summary, linkedin_summary = await asyncio.gather(
        fetch_company(lead.company_domain or "", lead.company_name),
        scrape_company_website(lead.company_domain or ""),
        scrape_linkedin_profile(lead.linkedin_url or ""),
    )

    company_profile = _parse_company_profile(apollo_data)
    recent_signals  = _parse_recent_signals(apollo_data)

    logger.debug(
        "Enriched lead_id=%s | seniority=%s | apollo=%s | size=%s | website_chars=%d | linkedin_chars=%d",
        lead.lead_id,
        role_seniority,
        "hit" if apollo_data else "miss",
        company_profile.company_size or "unknown",
        len(company_website_summary),
        len(linkedin_summary),
    )

    return EnrichedLead(
        lead                    = lead,
        company_profile         = company_profile,
        recent_signals          = recent_signals,
        role_seniority          = role_seniority,
        company_website_summary = company_website_summary,
        linkedin_summary        = linkedin_summary,
    )
