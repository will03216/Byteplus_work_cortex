"""
Global configuration: scoring weights, decision thresholds, Claude model, concurrency settings.
"""

# ── Claude ──────────────────────────────────────────────────────────────────
CLAUDE_MODEL = "claude-haiku-4-5-20251001"

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
QUALIFIED_THRESHOLD    = 75   # score >= 75 → qualified
DISQUALIFIED_THRESHOLD = 60   # score <  60 → disqualified  (else → review)

# ── Concurrency & reliability ────────────────────────────────────────────────
DEFAULT_CONCURRENCY    = 10
MAX_RETRIES            = 2
CLAUDE_TIMEOUT_SECONDS = 30   # per individual Claude API call
LEAD_TIMEOUT_SECONDS   = 90   # entire per-lead pipeline deadline (enrich + stage1 + stage2 + retries)

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
