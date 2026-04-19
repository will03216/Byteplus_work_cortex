"""
Normalization layer: clean, standardize, and deduplicate raw Trigify lead data.
"""

import re
import logging
from typing import Optional
from pydantic import BaseModel, field_validator, model_validator

logger = logging.getLogger(__name__)

# ── Title normalization map ──────────────────────────────────────────────────
_TITLE_MAP = {
    r"\bvice\s+president\b":   "VP",
    r"\bsvp\b":                "SVP",
    r"\bevp\b":                "EVP",
    r"\bceo\b":                "CEO",
    r"\bcto\b":                "CTO",
    r"\bcpo\b":                "CPO",
    r"\bcmo\b":                "CMO",
    r"\bcoo\b":                "COO",
    r"\bcfo\b":                "CFO",
    r"\bmd\b":                 "Managing Director",
    r"\bdir\b\.?":             "Director",
    r"\bmgr\b\.?":             "Manager",
    r"\bsr\.?\s+":             "Senior ",
    r"\bjr\.?\s+":             "Junior ",
}


class LeadInput(BaseModel):
    lead_id:           str
    person_name:       str
    title:             str
    company_name:      str
    company_domain:    str
    linkedin_url:      Optional[str] = None
    signal_text:       str
    signal_type:       str
    signal_timestamp:  str

    @field_validator("company_domain", mode="before")
    @classmethod
    def _normalize_domain(cls, v: str) -> str:
        v = str(v).strip().lower()
        v = re.sub(r"^https?://", "", v)
        v = re.sub(r"^www\.", "", v)
        return v.rstrip("/")

    @field_validator("lead_id", "person_name", "company_name",
                     "signal_text", "signal_type", "signal_timestamp",
                     mode="before")
    @classmethod
    def _strip(cls, v: str) -> str:
        return str(v).strip()

    @field_validator("title", mode="before")
    @classmethod
    def _normalize_title(cls, v: str) -> str:
        v = str(v).strip()
        lower = v.lower()
        for pattern, replacement in _TITLE_MAP.items():
            lower = re.sub(pattern, replacement.lower(), lower, flags=re.IGNORECASE)
        # Re-capitalize words
        return " ".join(word.capitalize() for word in lower.split())

    @field_validator("linkedin_url", mode="before")
    @classmethod
    def _clean_linkedin(cls, v) -> Optional[str]:
        if not v:
            return None
        v = str(v).strip()
        return v if v.startswith("http") else f"https://{v}"

    @model_validator(mode="after")
    def _require_signal_text(self) -> "LeadInput":
        if not self.signal_text:
            raise ValueError(f"lead {self.lead_id}: signal_text is empty")
        return self


def normalize_lead(raw: dict) -> LeadInput:
    """Parse and normalize a single raw lead dict."""
    return LeadInput(**raw)


def normalize_batch(raw_leads: list[dict]) -> list[LeadInput]:
    """
    Normalize a list of raw leads.
    Deduplicates by lead_id (keeps first occurrence).
    Skips and logs invalid leads rather than crashing the batch.
    """
    seen:   set[str]      = set()
    result: list[LeadInput] = []

    for raw in raw_leads:
        lead_id = str(raw.get("lead_id", "")).strip()
        if not lead_id:
            logger.warning("Skipping lead with missing lead_id: %s", raw)
            continue
        if lead_id in seen:
            logger.warning("Duplicate lead_id=%s — skipping", lead_id)
            continue
        try:
            lead = normalize_lead(raw)
            result.append(lead)
            seen.add(lead_id)
        except Exception as exc:
            logger.error("Failed to normalize lead_id=%s: %s", lead_id, exc)

    return result
