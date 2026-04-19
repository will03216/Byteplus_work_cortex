"""
Tool enrichment layer.

Python orchestrates all tool calls — Claude never calls tools directly.
Each function is a stub or live implementation designed for easy extension.

Extension points:
  - get_company_profile  → Apollo.io / Clearbit / PDL
  - get_recent_signals   → Trigify enrichment API
  - scrape_company_website / scrape_linkedin_profile → see web_enrichment.py
"""

import asyncio
import logging
from typing import Optional
from pydantic import BaseModel

from normalization.normalizer import LeadInput
from tools.web_enrichment import scrape_company_website, scrape_linkedin_profile

logger = logging.getLogger(__name__)


# ── Enrichment models ────────────────────────────────────────────────────────

class CompanyProfile(BaseModel):
    description:    Optional[str] = None
    industry:       Optional[str] = None
    company_size:   Optional[str] = None   # e.g. "51-200", "1000+"


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


# ── Individual tool stubs ────────────────────────────────────────────────────

def get_company_profile(domain: str) -> CompanyProfile:
    """
    Stub: returns empty profile.
    v2: replace body with Apollo.io / Clearbit call.
    """
    logger.debug("get_company_profile(domain=%s) — stub, returning empty", domain)
    return CompanyProfile()


def get_recent_signals(company_name: str) -> RecentSignals:
    """
    Stub: returns empty signals.
    v2: replace body with Trigify enrichment API or LinkedIn scraper.
    """
    logger.debug("get_recent_signals(company=%s) — stub, returning empty", company_name)
    return RecentSignals()


def get_role_seniority(title: str) -> str:
    """
    Lightweight local heuristic for v1.
    v2: replace with Apollo.io people API or PDL.

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
    Runs enrichment tools concurrently — only when the relevant field is present.

    Web scraping (company website + LinkedIn) is performed in parallel via
    asyncio.gather so both network fetches happen simultaneously.
    All enrichment is best-effort: any failure returns empty string and the
    pipeline continues with whatever data is available.
    """
    # Company profile: stub (only fetch when we have a domain to look up)
    company_profile = (
        get_company_profile(lead.company_domain)
        if lead.company_domain
        else CompanyProfile()
    )

    # Recent signals: stub (only fetch when we have a company name to query)
    recent_signals = (
        get_recent_signals(lead.company_name)
        if lead.company_name
        else RecentSignals()
    )

    # Role seniority: local heuristic, no external call — always run
    role_seniority = get_role_seniority(lead.title)

    # Web enrichment: scrape company website + LinkedIn profile concurrently
    website_task = scrape_company_website(lead.company_domain or "")
    linkedin_task = scrape_linkedin_profile(lead.linkedin_url or "")
    company_website_summary, linkedin_summary = await asyncio.gather(
        website_task,
        linkedin_task,
        return_exceptions=False,  # failures handled inside each scraper
    )

    logger.debug(
        "Enriched lead_id=%s | seniority=%s | website_chars=%d | linkedin_chars=%d",
        lead.lead_id,
        role_seniority,
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
