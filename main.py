"""
Lead Qualification Engine — CLI entrypoint.

Usage:
    python main.py --input leads.json   --output-dir ./output [--concurrency 10] [--verbose]
    python main.py --input leads.csv    --output-dir ./output [--concurrency 10] [--verbose]

Supports both JSON (array of lead objects) and CSV (Trigify export format).
File type is detected automatically from the file extension.

Pipeline per lead:
    raw input → normalize → enrich → Stage 1 (evidence) → Stage 2 (decision) → route → CSV + artifacts
"""

import argparse
import asyncio
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import config
from campaigns import load_all_campaigns, get_campaign, CampaignContext
from llm import LiteLLMClient
from normalization import normalize_batch, LeadInput, load_csv, load_json
from tools import enrich_lead, EnrichedLead
from claude import extract_evidence, EvidenceOutput, make_decision, DecisionOutput
from router import route_decision
from output.csv_writer import write_result, init_output_dir


# ── Logging setup ─────────────────────────────────────────────────────────────

def setup_logging(verbose: bool, output_dir: str) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    log_path = Path(output_dir) / "run.log"
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_path, encoding="utf-8"),
    ]
    logging.basicConfig(
        level   = level,
        format  = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        handlers= handlers,
    )


# ── Run statistics ────────────────────────────────────────────────────────────

@dataclass
class RunStats:
    total:         int = 0
    qualified:     int = 0
    disqualified:  int = 0
    review:        int = 0
    errors:        int = 0
    error_ids:     list[str] = field(default_factory=list)

    def record(self, routing: str) -> None:
        self.total += 1
        if routing == "qualified":
            self.qualified += 1
        elif routing == "disqualified":
            self.disqualified += 1
        else:
            self.review += 1

    def record_error(self, lead_id: str) -> None:
        self.total += 1
        self.errors += 1
        self.error_ids.append(lead_id)

    def summary(self) -> str:
        lines = [
            "─" * 50,
            f"  Total processed : {self.total}",
            f"  Qualified        : {self.qualified}",
            f"  Disqualified     : {self.disqualified}",
            f"  Review           : {self.review}",
            f"  Errors           : {self.errors}",
        ]
        if self.error_ids:
            lines.append(f"  Failed lead IDs  : {', '.join(self.error_ids)}")
        lines.append("─" * 50)
        return "\n".join(lines)


# ── Per-lead pipeline ─────────────────────────────────────────────────────────

async def _run_lead_pipeline(
    client:     LiteLLMClient,
    lead:       LeadInput,
    output_dir: str,
    stats:      RunStats,
    campaign:   CampaignContext = None,
) -> None:
    """Inner pipeline steps for a single lead (no semaphore, no timeout — caller owns those)."""
    logger = logging.getLogger(__name__)

    # Enrichment is already async because it performs concurrent network fetches.
    enriched: EnrichedLead = await enrich_lead(lead)

    # Stage 1: Evidence extraction (campaign-aware)
    evidence: EvidenceOutput = await extract_evidence(client, enriched, campaign=campaign)

    # Stage 2: Qualification decision (campaign-aware)
    decision: DecisionOutput = await make_decision(client, evidence, campaign=campaign)

    # Routing (pure Python, campaign-aware thresholds)
    routing = route_decision(decision, evidence, campaign=campaign)

    # Override decision field in DecisionOutput to match final routing
    decision = decision.model_copy(update={"decision": routing})

    # Write outputs
    await write_result(lead, enriched, evidence, decision, routing, output_dir)

    stats.record(routing)
    logger.info(
        "✓ lead_id=%-20s | %-12s | score=%3d | %s",
        lead.lead_id, routing.upper(), decision.score, lead.company_name,
    )


async def process_lead(
    client:     LiteLLMClient,
    lead:       LeadInput,
    output_dir: str,
    sem:        asyncio.Semaphore,
    stats:      RunStats,
    campaign:   CampaignContext = None,
) -> None:
    """Full pipeline for a single lead, bounded by semaphore and per-lead deadline."""
    logger = logging.getLogger(__name__)

    async with sem:
        try:
            await asyncio.wait_for(
                _run_lead_pipeline(client, lead, output_dir, stats, campaign=campaign),
                timeout=config.LEAD_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            stats.record_error(lead.lead_id)
            logger.error(
                "✗ lead_id=%s TIMED OUT after %ds",
                lead.lead_id, config.LEAD_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            stats.record_error(lead.lead_id)
            logger.error("✗ lead_id=%s FAILED: %s", lead.lead_id, exc, exc_info=True)


# ── Main batch runner ─────────────────────────────────────────────────────────

async def run_batch(
    input_path:  str,
    output_dir:  str,
    concurrency: int,
    campaign_id: str = config.DEFAULT_CAMPAIGN,
) -> RunStats:
    logger = logging.getLogger(__name__)

    # Load campaign context — must happen before processing
    skills_root = Path(__file__).parent / "skills"
    load_all_campaigns(skills_root)

    # If campaign not found on disk (e.g. created via UI and stored in DB only),
    # fall back to loading it from PostgreSQL.
    try:
        campaign = get_campaign(campaign_id)
    except ValueError:
        import os
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            raise
        import asyncpg
        from campaigns.loader import CampaignLoader
        from campaigns.registry import reload_campaign_from_strings
        logger.info("Campaign '%s' not on disk — loading from DB", campaign_id)
        pool = await asyncpg.create_pool(database_url, min_size=1, max_size=2)
        try:
            rows = await pool.fetch(
                "SELECT filename, content FROM skill_files WHERE campaign_id = $1", campaign_id
            )
            common = await pool.fetch(
                "SELECT filename, content FROM skill_files WHERE campaign_id = 'common'"
            )
        finally:
            await pool.close()
        if not rows:
            raise ValueError(f"Campaign '{campaign_id}' not found on disk or in DB")
        files = {r["filename"]: r["content"] for r in common}
        files.update({r["filename"]: r["content"] for r in rows})
        campaign = CampaignLoader(skills_root).load_from_strings(campaign_id, files)
    logger.info("Campaign: %s (%s)", campaign.name, campaign.id)

    # Load leads — auto-detect format from file extension
    suffix = Path(input_path).suffix.lower()
    if suffix == ".csv":
        raw_leads: list[dict] = load_csv(input_path)
    elif suffix == ".json":
        raw_leads = load_json(input_path)
    else:
        raise ValueError(f"Unsupported input format '{suffix}'. Use .json or .csv")
    # #region debug-point D:raw-load
    try: import urllib.request; urllib.request.urlopen(urllib.request.Request("http://host.docker.internal:7777/event", data=json.dumps({"sessionId":"run-import-missing","runId":"pre-fix","hypothesisId":"D","location":"main.py:run_batch:raw","msg":"[DEBUG] raw leads loaded","data":{"input_path":input_path,"suffix":suffix,"raw_lead_count":len(raw_leads)}}).encode(), headers={"Content-Type":"application/json"}), timeout=2).read()
    except Exception: pass
    # #endregion
    logger.info("Loaded %d raw leads from %s", len(raw_leads), input_path)

    # Normalize + deduplicate
    leads = normalize_batch(raw_leads)
    # #region debug-point D:normalized-load
    try: import urllib.request; urllib.request.urlopen(urllib.request.Request("http://host.docker.internal:7777/event", data=json.dumps({"sessionId":"run-import-missing","runId":"pre-fix","hypothesisId":"D","location":"main.py:run_batch:normalized","msg":"[DEBUG] leads normalized","data":{"raw_lead_count":len(raw_leads),"normalized_count":len(leads),"skipped_count":len(raw_leads) - len(leads)}}).encode(), headers={"Content-Type":"application/json"}), timeout=2).read()
    except Exception: pass
    # #endregion
    logger.info("Normalized %d valid leads (skipped %d)", len(leads), len(raw_leads) - len(leads))

    if not leads:
        logger.warning("No valid leads to process.")
        return RunStats()

    # Initialize output directories and CSV headers
    init_output_dir(output_dir)

    # Shared LiteLLM client. Model/provider selection comes from .env.
    if not config.LLM_API_KEY:
        raise RuntimeError("LLM_API_KEY environment variable is not set")
    if not config.LLM_MODEL:
        raise RuntimeError("LLM_MODEL environment variable is not set")

    stats = RunStats()
    sem   = asyncio.Semaphore(concurrency)
    client = LiteLLMClient(
        model           = config.LLM_MODEL,
        api_key         = config.LLM_API_KEY,
        provider        = config.LLM_PROVIDER,
        api_base        = config.LLM_API_BASE,
        timeout_seconds = config.LLM_TIMEOUT_SECONDS,
    )

    tasks = [
        process_lead(client, lead, output_dir, sem, stats, campaign=campaign)
        for lead in leads
    ]
    await asyncio.gather(*tasks, return_exceptions=False)

    return stats


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Lead Qualification Engine — Seedance",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--input", "-i",
        required=True,
        help="Path to input CSV or JSON file",
    )
    parser.add_argument(
        "--output-dir", "-o",
        default="./output",
        help="Directory for CSV outputs and artifacts (default: ./output)",
    )
    parser.add_argument(
        "--concurrency", "-c",
        type=int,
        default=config.DEFAULT_CONCURRENCY,
        choices=range(5, 21),
        metavar="[5-20]",
        help=f"Number of concurrent leads to process, 5–20 (default: {config.DEFAULT_CONCURRENCY})",
    )
    parser.add_argument(
        "--campaign",
        default=config.DEFAULT_CAMPAIGN,
        help=f"Campaign to run (default: {config.DEFAULT_CAMPAIGN}). Must match a directory under skills/",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    setup_logging(args.verbose, args.output_dir)
    logger = logging.getLogger(__name__)

    logger.info("=" * 50)
    logger.info("Lead Qualification Engine")
    logger.info("  Campaign   : %s", args.campaign)
    logger.info("  Input      : %s", args.input)
    logger.info("  Output dir : %s", args.output_dir)
    logger.info("  Concurrency: %d", args.concurrency)
    logger.info("  Provider   : %s", config.LLM_PROVIDER or "auto")
    logger.info("  Model      : %s", config.LLM_MODEL)
    logger.info("=" * 50)

    start = time.monotonic()

    stats = asyncio.run(
        run_batch(
            input_path  = args.input,
            output_dir  = args.output_dir,
            concurrency = args.concurrency,
            campaign_id = args.campaign,
        )
    )

    elapsed = time.monotonic() - start
    logger.info("Completed in %.1fs", elapsed)
    print(stats.summary())


if __name__ == "__main__":
    main()
