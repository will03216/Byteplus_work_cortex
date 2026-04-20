"""
LLM Stage 2: Qualification Decision.

Input is ONLY the Stage 1 EvidenceOutput — no raw lead data is passed here.
Applies the 100-point scoring model and returns a deterministic DecisionOutput.
Python enforces score integrity and decision thresholds after parsing.
"""

import json
import logging
from typing import Literal

from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

import config
from claude.prompts import build_stage2_system, build_stage2_messages
from claude.stage1_evidence import EvidenceOutput
from llm import LiteLLMClient, LLMError

logger = logging.getLogger(__name__)


# ── Output model ─────────────────────────────────────────────────────────────

class DecisionOutput(BaseModel):
    lead_id:                  str
    decision:                 Literal["qualified", "disqualified", "review"]
    score:                    int
    confidence:               Literal["high", "medium", "low"]
    segment_fit:              int
    workflow_fit:             int
    pain_fit:                 int
    buyer_fit:                int
    commercial_fit:           int
    qualification_reasons:    list[str]
    disqualification_reasons: list[str]
    reasoning_summary:        str
    suggested_use_case:       str


# ── Validation helpers ────────────────────────────────────────────────────────

def _enforce_score_integrity(data: dict, lead_id: str) -> dict:
    """
    Python-side guard: clamp each sub-score to its configured maximum, re-sum,
    and re-derive decision. Guarantees score is always in [0, 100] and the
    decision always matches the threshold, regardless of what the model returned.
    """
    # Step 1: clamp each sub-score to [0, max_weight]
    for field_name, max_val in config.SCORE_WEIGHTS.items():
        raw = data.get(field_name, 0)
        clamped = max(0, min(int(raw), max_val))
        if clamped != raw:
            logger.warning(
                "lead_id=%s: %s=%s out of bounds [0, %d] — clamping to %d",
                lead_id, field_name, raw, max_val, clamped,
            )
        data[field_name] = clamped

    # Step 2: re-sum from clamped values
    sub_scores = sum(data[f] for f in config.SCORE_WEIGHTS)

    if data.get("score") != sub_scores:
        logger.warning(
            "lead_id=%s: model score=%d doesn't match sub-scores=%d — correcting",
            lead_id, data.get("score", -1), sub_scores,
        )
        data["score"] = sub_scores

    score = data["score"]
    if score >= config.QUALIFIED_THRESHOLD:
        correct_decision = "qualified"
    elif score < config.DISQUALIFIED_THRESHOLD:
        correct_decision = "disqualified"
    else:
        correct_decision = "review"

    if data.get("decision") != correct_decision:
        logger.warning(
            "lead_id=%s: model decision=%s doesn't match score=%d — correcting to %s",
            lead_id, data.get("decision"), score, correct_decision,
        )
        data["decision"] = correct_decision

    return data


def _parse_response(raw: str, lead_id: str, hard_disqualifiers: list[str]) -> DecisionOutput:
    """Parse and validate the Stage 2 response."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Stage 2 returned non-JSON for lead_id={lead_id}: {exc}\nRaw: {raw[:500]}") from exc

    data["lead_id"] = lead_id

    # Apply hard disqualifier override BEFORE score enforcement
    if hard_disqualifiers:
        data["decision"] = "disqualified"
        if not data.get("disqualification_reasons"):
            data["disqualification_reasons"] = hard_disqualifiers

    # Enforce score integrity
    data = _enforce_score_integrity(data, lead_id)

    return DecisionOutput(**data)


# ── Main function (with retry) ────────────────────────────────────────────────

@retry(
    stop=stop_after_attempt(config.MAX_RETRIES + 1),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((ValueError, LLMError)),
    reraise=True,
)
async def make_decision(
    client:   LiteLLMClient,
    evidence: EvidenceOutput,
) -> DecisionOutput:
    """
    Call the configured LLM for Stage 2 scoring and qualification.

    Args:
        client:   Shared LiteLLM client instance
        evidence: Stage 1 EvidenceOutput — this is the ONLY input to Stage 2

    Returns:
        DecisionOutput with score, sub-scores, decision, and reasoning
    """
    lead_id      = evidence.lead_id
    evidence_dict = evidence.model_dump()

    logger.info("Stage 2 → lead_id=%s", lead_id)
    logger.debug(
        "Stage 2 input lead_id=%s: %s",
        lead_id,
        json.dumps(evidence_dict, ensure_ascii=False),
    )

    raw_text = await client.generate(
        system_prompt = build_stage2_system(),
        messages      = build_stage2_messages(evidence_dict),
        max_tokens    = 1024,
    )
    logger.debug("Stage 2 raw response for lead_id=%s: %s", lead_id, raw_text[:300])

    decision = _parse_response(raw_text, lead_id, evidence.hard_disqualifiers)
    logger.info(
        "Stage 2 done → lead_id=%s | decision=%s | score=%d | confidence=%s",
        lead_id, decision.decision, decision.score, decision.confidence,
    )
    logger.debug("Stage 2 output lead_id=%s: %s", lead_id, decision.model_dump_json())
    return decision
