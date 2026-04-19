"""
Claude Stage 1: Evidence Extraction.

Takes a normalized + enriched lead and returns structured EvidenceOutput.
Retries up to MAX_RETRIES times on parse or API failure.
"""

import json
import logging
from typing import Literal, Any

import anthropic
from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

import config
from claude.prompts import build_stage1_system, build_stage1_messages
from tools.enrichment import EnrichedLead

logger = logging.getLogger(__name__)


# ── Output model ─────────────────────────────────────────────────────────────

class EvidenceOutput(BaseModel):
    lead_id:            str
    segment:            str
    candidate_use_case: str
    signal_strength:    Literal["high", "medium", "medium-low", "low"]
    observed_evidence:  list[str]
    inferred_evidence:  list[str]
    unknowns:           list[str]
    hard_disqualifiers: list[str]


# ── Internal helpers ──────────────────────────────────────────────────────────

_TENTATIVE_DISQUALIFIER_MARKERS = (
    "potential",
    "possible",
    "maybe",
    "may be",
    "might be",
    "unclear",
    "unknown",
    "unresolved",
    "needs review",
    "requires review",
    "determine if",
)


def _normalize_hard_disqualifiers(data: dict, lead_id: str) -> dict:
    """
    Only confirmed negatives should remain in hard_disqualifiers.

    Claude sometimes puts tentative language like "potential competitor" or
    "needs review" into the hard_disqualifiers field. That breaks the contract
    for downstream routing, because any non-empty hard_disqualifiers list
    becomes an automatic disqualification in Python.
    """
    hard_disqualifiers = data.get("hard_disqualifiers") or []
    unknowns = list(data.get("unknowns") or [])

    confirmed: list[str] = []
    for item in hard_disqualifiers:
        text = str(item).strip()
        lower = text.lower()
        if any(marker in lower for marker in _TENTATIVE_DISQUALIFIER_MARKERS):
            logger.warning(
                "lead_id=%s: converting tentative hard disqualifier to unknown: %s",
                lead_id,
                text,
            )
            if text not in unknowns:
                unknowns.append(text)
        else:
            confirmed.append(text)

    data["hard_disqualifiers"] = confirmed
    data["unknowns"] = unknowns
    return data

def _parse_response(raw: str, lead_id: str) -> EvidenceOutput:
    """Parse Claude's raw text response into EvidenceOutput."""
    text = raw.strip()
    # Strip markdown fences if Claude wraps in ```json ... ```
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Stage 1 returned non-JSON for lead_id={lead_id}: {exc}\nRaw: {raw[:500]}") from exc

    # Ensure lead_id matches
    data["lead_id"] = lead_id
    data = _normalize_hard_disqualifiers(data, lead_id)
    return EvidenceOutput(**data)


def _build_enrichment_dict(enriched: EnrichedLead) -> dict[str, Any]:
    d: dict[str, Any] = {
        "company_profile": enriched.company_profile.model_dump(),
        "recent_signals":  enriched.recent_signals.model_dump(),
        "role_seniority":  enriched.role_seniority,
    }
    if enriched.company_website_summary:
        d["company_website_summary"] = enriched.company_website_summary
    if enriched.linkedin_summary:
        d["linkedin_summary"] = enriched.linkedin_summary
    return d


# ── Main function (with retry) ────────────────────────────────────────────────

@retry(
    stop=stop_after_attempt(config.MAX_RETRIES + 1),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((ValueError, anthropic.APIError)),
    reraise=True,
)
async def extract_evidence(
    client:   anthropic.AsyncAnthropic,
    enriched: EnrichedLead,
) -> EvidenceOutput:
    """
    Call Claude Stage 1 to extract structured evidence from a lead.

    Args:
        client:   AsyncAnthropic client instance (shared across batch)
        enriched: Enriched lead (normalized lead + tool enrichment data)

    Returns:
        EvidenceOutput with signal strength, evidence, unknowns, disqualifiers
    """
    lead     = enriched.lead
    lead_id  = lead.lead_id

    lead_dict       = lead.model_dump()
    enrichment_dict = _build_enrichment_dict(enriched)

    logger.info("Stage 1 → lead_id=%s", lead_id)
    logger.debug(
        "Stage 1 input lead_id=%s: %s",
        lead_id,
        json.dumps({"lead": lead_dict, "enrichment": enrichment_dict}, ensure_ascii=False),
    )

    response = await client.messages.create(
        model    = config.CLAUDE_MODEL,
        max_tokens = 1024,
        system   = build_stage1_system(),
        messages = build_stage1_messages(lead_dict, enrichment_dict),
        timeout  = config.CLAUDE_TIMEOUT_SECONDS,
    )

    raw_text = response.content[0].text
    logger.debug("Stage 1 raw response for lead_id=%s: %s", lead_id, raw_text[:300])

    evidence = _parse_response(raw_text, lead_id)
    logger.info(
        "Stage 1 done → lead_id=%s | signal_strength=%s | disqualifiers=%d",
        lead_id, evidence.signal_strength, len(evidence.hard_disqualifiers),
    )
    logger.debug("Stage 1 output lead_id=%s: %s", lead_id, evidence.model_dump_json())
    return evidence
