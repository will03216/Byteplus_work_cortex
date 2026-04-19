"""
CSV loader: maps the Trigify CSV export format to LeadInput-compatible dicts.

CSV columns used:
  NAME, JOB TITLE, COMPANY, INDUSTRY, COMPANY SIZE, ICP SCORE,
  COMPANY URL, LINKEDIN PROFILE, LOCATION, HQ COMPANY LOCATION,
  INTERACTION, POST URL

signal_text is constructed from all available signal fields since the CSV
does not contain a raw signal text column.
"""

import csv
import re
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Map CSV INTERACTION values → signal_type
_INTERACTION_TYPE_MAP = {
    "liked":     "linkedin_engagement",
    "commented": "linkedin_comment",
    "shared":    "linkedin_share",
    "posted":    "linkedin_post",
    "reacted":   "linkedin_engagement",
}


def _make_lead_id(name: str, company: str, index: int) -> str:
    """Generate a stable lead_id from name + company."""
    raw = f"{name}_{company}_{index}"
    return re.sub(r"[^a-z0-9_]", "_", raw.lower().strip())[:64]


def _build_signal_text(row: dict) -> str:
    """
    Construct a rich signal_text from all available CSV fields.
    Claude needs a descriptive, freeform text to reason from.
    """
    parts = []

    name        = row.get("NAME", "").strip()
    title       = row.get("JOB TITLE", "").strip()
    company     = row.get("COMPANY", "").strip()
    interaction = row.get("INTERACTION", "").strip()
    post_url    = row.get("POST URL", "").strip()
    icp_note    = row.get("ICP SCORE", "").strip()
    industry    = row.get("INDUSTRY", "").strip()
    size        = row.get("COMPANY SIZE", "").strip()
    location    = row.get("LOCATION", "").strip()
    hq          = row.get("HQ COMPANY LOCATION", "").strip()

    # Primary signal: who did what
    if interaction and company:
        parts.append(
            f"{name} ({title} at {company}) {interaction.lower()} a LinkedIn post"
            + (f" (Post: {post_url})" if post_url else "") + "."
        )

    # Trigify's pre-qualification note — this is the richest signal field
    if icp_note:
        parts.append(f"Trigify qualification note: {icp_note}")

    # Firmographic context
    firm_parts = []
    if industry:
        firm_parts.append(f"industry: {industry}")
    if size:
        firm_parts.append(f"company size: {size} employees")
    if hq and hq != location:
        firm_parts.append(f"HQ: {hq}")
    elif hq:
        firm_parts.append(f"location: {hq}")
    if firm_parts:
        parts.append("Company details — " + ", ".join(firm_parts) + ".")

    return " ".join(parts)


def load_csv(file_path: str) -> list[dict]:
    """
    Read a Trigify CSV export and return a list of dicts in LeadInput format.
    Each row becomes one lead dict ready for normalize_batch().
    Skips rows with missing NAME or COMPANY and logs a warning.
    """
    leads = []
    run_timestamp = datetime.now(timezone.utc).isoformat()

    with open(file_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for index, row in enumerate(reader, start=1):
            name    = row.get("NAME", "").strip()
            company = row.get("COMPANY", "").strip()

            if not name or not company:
                logger.warning("Row %d: missing NAME or COMPANY — skipping", index)
                continue

            interaction = row.get("INTERACTION", "").strip().lower()
            signal_type = _INTERACTION_TYPE_MAP.get(interaction, "linkedin_engagement")

            lead = {
                "lead_id":          _make_lead_id(name, company, index),
                "person_name":      name,
                "title":            row.get("JOB TITLE", "").strip(),
                "company_name":     company,
                "company_domain":   row.get("COMPANY URL", "").strip(),
                "linkedin_url":     row.get("LINKEDIN PROFILE", "").strip() or None,
                "signal_text":      _build_signal_text(row),
                "signal_type":      signal_type,
                "signal_timestamp": run_timestamp,
            }
            leads.append(lead)

    logger.info("CSV loader: read %d leads from %s", len(leads), file_path)
    return leads
