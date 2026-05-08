"""
Decision Router (pure Python).

Applies final routing logic after Claude Stage 2.
This is the authoritative routing step — Claude's decision field is used,
but Python has already enforced score integrity in stage2_decision.py.

Additional override: non-empty hard_disqualifiers always → disqualified.
"""

import logging
from typing import TYPE_CHECKING, Literal, Optional

import config
from claude.stage2_decision import DecisionOutput
from claude.stage1_evidence import EvidenceOutput

if TYPE_CHECKING:
    from campaigns.context import CampaignContext

logger = logging.getLogger(__name__)

Decision = Literal["qualified", "disqualified", "review"]


def route_decision(
    decision: DecisionOutput,
    evidence: EvidenceOutput,
    campaign: Optional["CampaignContext"] = None,
) -> Decision:
    """
    Final Python-authoritative routing.

    Order of precedence:
    1. Hard disqualifiers present → disqualified (always overrides)
    2. Low signal strength → disqualified (no reliable evidence base)
    3. Score-based threshold routing

    Args:
        decision: Stage 2 output (score already integrity-checked)
        evidence: Stage 1 output (used for hard disqualifier check)
        campaign: Optional CampaignContext; if None uses config defaults

    Returns:
        Final routing decision string
    """
    qt = campaign.qualified_threshold if campaign is not None else config.QUALIFIED_THRESHOLD
    dt = campaign.disqualified_threshold if campaign is not None else config.DISQUALIFIED_THRESHOLD

    # Override 1: hard disqualifiers
    if evidence.hard_disqualifiers:
        logger.info(
            "lead_id=%s → disqualified (hard disqualifiers: %s)",
            decision.lead_id, evidence.hard_disqualifiers,
        )
        return "disqualified"

    # Override 2: signal too weak — low signal is a hard disqualifier per PRD.
    # Score is irrelevant: a low-signal lead has no reliable evidence base,
    # so it cannot be trusted regardless of how Claude scored it.
    if evidence.signal_strength == "low":
        logger.info(
            "lead_id=%s → disqualified (low signal strength, score=%d ignored)",
            decision.lead_id, decision.score,
        )
        return "disqualified"

    # Score-based routing
    score = decision.score
    if score >= qt:
        result: Decision = "qualified"
    elif score < dt:
        result = "disqualified"
    else:
        result = "review"

    logger.info(
        "lead_id=%s → %s (score=%d, signal=%s)",
        decision.lead_id, result, score, evidence.signal_strength,
    )
    return result
