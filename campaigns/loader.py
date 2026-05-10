"""
CampaignLoader — reads skills/{campaign_id}/ and assembles a CampaignContext.

Each campaign directory must contain:
  config.json        — name, tagline, score_weights, thresholds
  product.md         — product description and use cases
  icp.md             — target verticals, personas, segment options
  signals.md         — signal engagement rules and buying signals
  disqualification.md — hard disqualifier rules
  qualification.md   — 5-factor scoring rubric

Common skills (shared across all campaigns) live in skills/common/.
"""

import json
import logging
from pathlib import Path

import config as app_config
from campaigns.context import CampaignContext

logger = logging.getLogger(__name__)

# ── Prompt templates (structural skeleton, invariant across campaigns) ─────────

_STAGE1_TEMPLATE = """\
You are a structured evidence extraction engine for {product_name} — {product_tagline}.

Your job is to analyze a Trigify lead signal and extract structured reasoning evidence. You are NOT making a qualification decision here. You are extracting facts, classifying signals, and identifying unknowns.

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

Return ONLY this JSON object. Nothing else.\
"""

_STAGE2_TEMPLATE = """\
You are a deterministic lead qualification scoring engine for {product_name} — {product_tagline}.

Your input is ONLY the structured evidence object from Stage 1. You apply a scoring model and return a qualification decision.

## Output Contract
You MUST return ONLY a single valid JSON object. No explanation, no markdown, no prose before or after.

{qualification_section}

## Decision Rules (Python enforces these, but you must follow them)
- score >= {qualified_threshold} → "qualified"
- score < {disqualified_threshold} → "disqualified"
- {disqualified_threshold} <= score < {qualified_threshold} → "review"
- If hard_disqualifiers is non-empty → "disqualified" (override, score irrelevant)

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
- reasoning_summary: 2–3 sentences, factual, reference specific evidence
- qualification_reasons and disqualification_reasons: short evidence-based bullets

Return ONLY this JSON object. Nothing else.\
"""


class CampaignLoader:
    def __init__(self, skills_root: Path):
        self.skills_root = skills_root
        self._common_dir = skills_root / "common"

    def load(self, campaign_id: str) -> CampaignContext:
        """Load a campaign from disk files. Used for local dev and initial DB seeding."""
        campaign_dir = self.skills_root / campaign_id
        if not campaign_dir.exists():
            raise FileNotFoundError(f"Campaign skills directory not found: {campaign_dir}")

        files = {
            "config.json":              self._read(campaign_dir / "config.json"),
            "icp.md":                   self._read(campaign_dir / "icp.md"),
            "signals.md":               self._read(campaign_dir / "signals.md"),
            "disqualification.md":      self._read(campaign_dir / "disqualification.md"),
            "qualification.md":         self._read(campaign_dir / "qualification.md"),
            "evidence-extraction.md":   self._read(self._common_dir / "evidence-extraction.md"),
            "disqualification-rules.md": self._read(self._common_dir / "disqualification-rules.md"),
        }
        return self.load_from_strings(campaign_id, files)

    def load_from_strings(self, campaign_id: str, files: dict[str, str]) -> CampaignContext:
        """
        Build a CampaignContext from a dict of {filename: content}.
        Used when PostgreSQL is the source of truth for skill files.
        The dict should contain campaign-specific files plus any common files
        (e.g. evidence-extraction.md) already merged in by the caller.
        """
        cfg: dict = {}
        raw_config = files.get("config.json", "")
        if raw_config:
            try:
                cfg = json.loads(raw_config)
            except json.JSONDecodeError:
                logger.warning("Invalid config.json for campaign '%s' — using defaults", campaign_id)

        name = cfg.get("name", campaign_id.capitalize())
        tagline = cfg.get("tagline", "AI video generation platform")
        weights = cfg.get("score_weights", app_config.SCORE_WEIGHTS.copy())
        qualified_threshold = int(cfg.get("qualified_threshold", app_config.QUALIFIED_THRESHOLD))
        disqualified_threshold = int(cfg.get("disqualified_threshold", app_config.DISQUALIFIED_THRESHOLD))

        stage1_prompt = _STAGE1_TEMPLATE.format(
            product_name=name,
            product_tagline=tagline,
            icp_section=files.get("icp.md", "").strip(),
            signals_section=files.get("signals.md", "").strip(),
            disqualification_rules_section=files.get("disqualification-rules.md", "").strip(),
            disqualification_section=files.get("disqualification.md", "").strip(),
            evidence_rules_section=files.get("evidence-extraction.md", "").strip(),
        )

        stage2_prompt = _STAGE2_TEMPLATE.format(
            product_name=name,
            product_tagline=tagline,
            qualification_section=files.get("qualification.md", "").strip(),
            qualified_threshold=qualified_threshold,
            disqualified_threshold=disqualified_threshold,
            w_segment=weights.get("segment_fit", 30),
            w_buyer=weights.get("buyer_fit", 25),
            w_pain=weights.get("pain_fit", 20),
            w_workflow=weights.get("workflow_fit", 15),
            w_commercial=weights.get("commercial_fit", 10),
        )

        logger.info(
            "Loaded campaign '%s' (%s) — stage1 prompt: %d chars, stage2 prompt: %d chars",
            campaign_id, name, len(stage1_prompt), len(stage2_prompt),
        )

        return CampaignContext(
            id=campaign_id,
            name=name,
            stage1_system_prompt=stage1_prompt,
            stage2_system_prompt=stage2_prompt,
            score_weights=weights,
            qualified_threshold=qualified_threshold,
            disqualified_threshold=disqualified_threshold,
        )

    def _read(self, path: Path) -> str:
        if not path.exists():
            logger.warning("Skills file missing: %s", path)
            return ""
        return path.read_text(encoding="utf-8")
