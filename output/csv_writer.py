"""
Output layer: writes CSVs and JSON artifacts.

Three CSVs (append mode, thread-safe via file locks):
  qualified_leads.csv
  disqualified_leads.csv
  review_leads.csv

Four JSON artifacts per lead (in artifacts/):
  {lead_id}_input.json
  {lead_id}_enrichment.json
  {lead_id}_evidence.json
  {lead_id}_decision.json
"""

import csv
import json
import asyncio
import logging
import os
from pathlib import Path
from typing import Literal

import config
from normalization.normalizer import LeadInput
from claude.stage1_evidence import EvidenceOutput
from claude.stage2_decision import DecisionOutput
from tools.enrichment import EnrichedLead

logger = logging.getLogger(__name__)

# Module-level lock so concurrent coroutines don't interleave CSV rows
_csv_lock = asyncio.Lock()


def init_output_dir(output_dir: str) -> None:
    """Create output and artifacts directories, write CSV headers if files are new."""
    base = Path(output_dir)
    artifacts_dir = base / config.ARTIFACTS_DIR

    base.mkdir(parents=True, exist_ok=True)
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    _ensure_csv_header(base / config.QUALIFIED_CSV,    config.QUALIFIED_COLS)
    _ensure_csv_header(base / config.DISQUALIFIED_CSV, config.DISQUALIFIED_COLS)
    _ensure_csv_header(base / config.REVIEW_CSV,       config.REVIEW_COLS)
    logger.info("Output directory initialized: %s", base)


def _ensure_csv_header(path: Path, columns: list[str]) -> None:
    if not path.exists():
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()


def _write_artifact(artifacts_dir: Path, lead_id: str, suffix: str, data: dict) -> None:
    path = artifacts_dir / f"{lead_id}_{suffix}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _build_csv_row(
    lead:     LeadInput,
    enriched: EnrichedLead,
    evidence: EvidenceOutput,
    decision: DecisionOutput,
    routing:  Literal["qualified", "disqualified", "review"],
) -> dict:
    base = {
        "lead_id":      lead.lead_id,
        "person_name":  lead.person_name,
        "title":        lead.title,
        "company_name": lead.company_name,
        "email":        enriched.email or "",
        "email_status": enriched.email_status or "",
        "decision":     routing,
        "score":        decision.score,
        "confidence":   decision.confidence,
        "reasoning_summary": decision.reasoning_summary,
    }

    if routing == "qualified":
        base["segment"]  = evidence.segment
        base["use_case"] = decision.suggested_use_case
        return {k: base[k] for k in config.QUALIFIED_COLS}

    elif routing == "disqualified":
        base["disqualification_reasons"] = "; ".join(decision.disqualification_reasons)
        return {k: base[k] for k in config.DISQUALIFIED_COLS}

    else:  # review
        base["missing_data"] = "; ".join(evidence.unknowns)
        return {k: base[k] for k in config.REVIEW_COLS}


async def write_result(
    lead:       LeadInput,
    enriched:   EnrichedLead,
    evidence:   EvidenceOutput,
    decision:   DecisionOutput,
    routing:    Literal["qualified", "disqualified", "review"],
    output_dir: str,
) -> None:
    """
    Write all outputs for a single lead:
      - One row to the appropriate CSV
      - Four JSON artifact files

    Uses asyncio.Lock to prevent concurrent CSV writes from interleaving.
    """
    base          = Path(output_dir)
    artifacts_dir = base / config.ARTIFACTS_DIR

    # Write JSON artifacts (no lock needed — unique filenames)
    _write_artifact(artifacts_dir, lead.lead_id, "input",    lead.model_dump())
    _write_artifact(artifacts_dir, lead.lead_id, "enrichment", enriched.model_dump())
    _write_artifact(artifacts_dir, lead.lead_id, "evidence", evidence.model_dump())
    _write_artifact(artifacts_dir, lead.lead_id, "decision", decision.model_dump())

    # Write CSV row (locked)
    csv_filename = {
        "qualified":    config.QUALIFIED_CSV,
        "disqualified": config.DISQUALIFIED_CSV,
        "review":       config.REVIEW_CSV,
    }[routing]
    csv_path = base / csv_filename
    columns  = {
        "qualified":    config.QUALIFIED_COLS,
        "disqualified": config.DISQUALIFIED_COLS,
        "review":       config.REVIEW_COLS,
    }[routing]
    row = _build_csv_row(lead, enriched, evidence, decision, routing)

    async with _csv_lock:
        with open(csv_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writerow(row)

    logger.debug("Wrote output for lead_id=%s → %s", lead.lead_id, csv_filename)
