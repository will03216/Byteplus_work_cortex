"""
Input loaders for Trigify CSV exports and JSON lead files.

Supported input shapes:
  - Trigify CSV rows
  - JSON arrays of normalized lead objects
  - JSON arrays of Trigify-style row objects
"""

import csv
import io
import json
import re
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Map CSV INTERACTION values → signal_type
_INTERACTION_TYPE_MAP = {
    "liked":     "linkedin_engagement",
    "commented": "linkedin_comment",
    "shared":    "linkedin_share",
    "posted":    "linkedin_post",
    "reacted":   "linkedin_engagement",
}

_TRIGIFY_HINT_KEYS = {
    "NAME",
    "JOB TITLE",
    "COMPANY",
    "COMPANY URL",
    "LINKEDIN PROFILE",
    "INTERACTION",
    "POST URL",
    "ICP SCORE",
    "QUALIFICATION REASON",
}


def _make_lead_id(name: str, company: str, index: int) -> str:
    """Generate a stable lead_id from name + company."""
    raw = f"{name}_{company}_{index}"
    return re.sub(r"[^a-z0-9_]", "_", raw.lower().strip())[:64]


def _clean_cell(value) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    while len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'", "`"}:
        text = text[1:-1].strip()
    return text


def _sanitize_row(row: dict) -> dict:
    return {
        key: _clean_cell(value) if isinstance(value, str) or value is None else value
        for key, value in row.items()
    }


def _build_signal_text(row: dict) -> str:
    """
    Construct a rich signal_text from all available CSV fields.
    Claude needs a descriptive, freeform text to reason from.
    """
    parts = []

    name = _clean_cell(row.get("NAME", ""))
    title = _clean_cell(row.get("JOB TITLE", ""))
    company = _clean_cell(row.get("COMPANY", ""))
    interaction = _clean_cell(row.get("INTERACTION", ""))
    post_url = _clean_cell(row.get("POST URL", ""))
    icp_note = _clean_cell(row.get("ICP SCORE", ""))
    qualification_reason = _clean_cell(row.get("QUALIFICATION REASON", ""))
    industry = _clean_cell(row.get("INDUSTRY", ""))
    size = _clean_cell(row.get("COMPANY SIZE", ""))
    location = _clean_cell(row.get("LOCATION", ""))
    hq = _clean_cell(row.get("HQ COMPANY LOCATION", ""))

    # Primary signal: who did what
    if interaction and company:
        parts.append(
            f"{name} ({title} at {company}) {interaction.lower()} a LinkedIn post"
            + (f" (Post: {post_url})" if post_url else "") + "."
        )

    # Trigify's pre-qualification note — this is the richest signal field
    if icp_note:
        parts.append(f"Trigify qualification note: {icp_note}")
    if qualification_reason:
        parts.append(f"Qualification reason: {qualification_reason}")

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


def _looks_like_trigify_row(row: dict) -> bool:
    return bool(_TRIGIFY_HINT_KEYS.intersection(row.keys()))


def _build_trigify_lead(row: dict, index: int, run_timestamp: str) -> Optional[dict]:
    row = _sanitize_row(row)
    name = row.get("NAME", "")
    company = row.get("COMPANY", "")

    if not name or not company:
        logger.warning("Row %d: missing NAME or COMPANY — skipping", index)
        return None

    interaction = row.get("INTERACTION", "").lower()
    signal_type = _INTERACTION_TYPE_MAP.get(interaction, "linkedin_engagement")
    signal_text = _build_signal_text(row)
    if not signal_text:
        title = row.get("JOB TITLE", "") or "unknown role"
        signal_text = f"Profile request for {name} ({title}) at {company}."

    return {
        "lead_id": _make_lead_id(name, company, index),
        "person_name": name,
        "title": row.get("JOB TITLE", ""),
        "company_name": company,
        "company_domain": row.get("COMPANY URL", ""),
        "company_size": row.get("COMPANY SIZE", "") or None,
        "linkedin_url": row.get("LINKEDIN PROFILE", "") or None,
        "signal_text": signal_text,
        "signal_type": signal_type,
        "signal_timestamp": run_timestamp,
    }


def _decode_csv_text(file_path: str) -> tuple[str, str]:
    """
    Decode CSV bytes with a UTF-8-first strategy, then fall back to common
    spreadsheet/export encodings instead of failing the entire run.
    """
    raw_bytes = open(file_path, "rb").read()
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw_bytes.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("utf-8", raw_bytes, 0, 1, "unable to decode CSV with supported encodings")


def load_csv(file_path: str) -> list[dict]:
    """
    Read a Trigify CSV export and return a list of dicts in LeadInput format.
    Each row becomes one lead dict ready for normalize_batch().
    Skips rows with missing NAME or COMPANY and logs a warning.
    """
    leads = []
    run_timestamp = datetime.now(timezone.utc).isoformat()
    csv_text, encoding = _decode_csv_text(file_path)

    reader = csv.DictReader(io.StringIO(csv_text, newline=""), strict=True)
    if not reader.fieldnames:
        raise ValueError("CSV file is empty or missing a header row")
    missing_headers = {"NAME", "COMPANY"} - set(reader.fieldnames)
    if missing_headers:
        raise ValueError(
            f"CSV file is missing required columns: {', '.join(sorted(missing_headers))}"
        )

    for index, row in enumerate(reader, start=1):
        if None in row:
            extras = [_clean_cell(value) for value in row.get(None, []) if _clean_cell(value)]
            raise ValueError(
                f"Malformed CSV row {index}: found extra column values {extras!r}. "
                "Fields containing commas must be quoted."
            )
        lead = _build_trigify_lead(row, index, run_timestamp)
        if lead is not None:
            leads.append(lead)

    logger.info("CSV loader: read %d leads from %s using %s", len(leads), file_path, encoding)
    return leads


def load_json(file_path: str) -> list[dict]:
    """
    Read a JSON array containing either normalized leads or Trigify-style rows.
    """
    with open(file_path, encoding="utf-8") as f:
        payload = json.load(f)

    if not isinstance(payload, list):
        raise ValueError("JSON input must be an array of lead objects")

    run_timestamp = datetime.now(timezone.utc).isoformat()
    leads = []
    for index, row in enumerate(payload, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"JSON item {index} must be an object")
        if _looks_like_trigify_row(row):
            lead = _build_trigify_lead(row, index, run_timestamp)
            if lead is not None:
                leads.append(lead)
            continue
        leads.append(_sanitize_row(row))

    logger.info("JSON loader: read %d leads from %s", len(leads), file_path)
    return leads
