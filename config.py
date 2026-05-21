"""
Global configuration: model settings, scoring weights, decision thresholds,
and concurrency settings.
"""

import os


def _normalize_model_name(raw_model: str) -> str:
    """
    Preserve backward compatibility for old Claude-only config values while
    allowing provider-prefixed LiteLLM model names for any vendor.
    """
    if "/" in raw_model:
        return raw_model
    if raw_model.startswith("claude"):
        return f"anthropic/{raw_model}"
    return raw_model


# ── Apollo.io enrichment ─────────────────────────────────────────────────────
APOLLO_ENABLED      = os.getenv("APOLLO_ENABLED", "false").lower() in ("true", "1", "yes")
APOLLO_API_KEY      = os.getenv("APOLLO_API_KEY") or None
APOLLO_TIMEOUT      = int(os.getenv("APOLLO_TIMEOUT_SECONDS", "10"))

# ── LLM ─────────────────────────────────────────────────────────────────────
DEFAULT_LLM_MODEL   = "anthropic/claude-haiku-4-5-20251001"
LLM_PROVIDER        = os.getenv("LLM_PROVIDER") or None
LLM_MODEL           = _normalize_model_name(os.getenv("LLM_MODEL") or DEFAULT_LLM_MODEL)
LLM_API_KEY         = os.getenv("LLM_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
LLM_API_BASE        = os.getenv("LLM_API_BASE") or None
LLM_TIMEOUT_SECONDS = int(os.getenv("LLM_TIMEOUT_SECONDS", "30"))
LLM_SSL_VERIFY      = os.getenv("LLM_SSL_VERIFY", "true").lower() not in ("false", "0", "no")

# ── Scoring weights (must sum to 100) ───────────────────────────────────────
# Calibrated against Antti (Head of SDR) meeting — 2026-04-17
# Priority order: vertical fit → persona/seniority → pain → signal quality → company size
SCORE_WEIGHTS = {
    "segment_fit":    30,   # Primary filter — film studio/production/short drama/AI tools
    "buyer_fit":      25,   # Seniority + new-to-role is critical
    "pain_fit":       20,   # Explicit or implied production pain
    "workflow_fit":   15,   # Signal engagement quality + video/creative workflow
    "commercial_fit": 10,   # Company size + momentum signals
}

# ── Decision thresholds ──────────────────────────────────────────────────────
QUALIFIED_THRESHOLD    = 70   # score >= 70 → qualified
DISQUALIFIED_THRESHOLD = 60   # score <  60 → disqualified  (else → review)

# ── Concurrency & reliability ────────────────────────────────────────────────
DEFAULT_CONCURRENCY    = 10
MAX_RETRIES            = 2
LEAD_TIMEOUT_SECONDS = int(os.getenv("LEAD_TIMEOUT_SECONDS", "240"))  # enrich + stage1 + stage2 + retries

# ── Campaign defaults ────────────────────────────────────────────────────────
DEFAULT_CAMPAIGN = os.getenv("DEFAULT_CAMPAIGN", "seedance")
SKILLS_DIR       = os.getenv("SKILLS_DIR", "skills")          # relative to project root

# ── Output paths (relative to --output-dir) ──────────────────────────────────
QUALIFIED_CSV    = "qualified_leads.csv"
DISQUALIFIED_CSV = "disqualified_leads.csv"
REVIEW_CSV       = "review_leads.csv"
ARTIFACTS_DIR    = "artifacts"

# ── CSV column schemas ───────────────────────────────────────────────────────
QUALIFIED_COLS = [
    "lead_id", "person_name", "title", "company_name",
    "decision", "score", "confidence",
    "segment", "use_case", "reasoning_summary",
]

DISQUALIFIED_COLS = [
    "lead_id", "person_name", "title", "company_name",
    "decision", "score", "confidence",
    "disqualification_reasons", "reasoning_summary",
]

REVIEW_COLS = [
    "lead_id", "person_name", "title", "company_name",
    "decision", "score", "confidence",
    "missing_data", "reasoning_summary",
]
