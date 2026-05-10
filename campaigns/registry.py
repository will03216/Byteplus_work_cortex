"""
Campaign registry — in-memory store of all loaded campaigns.

Call load_all_campaigns() once at startup.
Then use get_campaign(campaign_id) anywhere in the pipeline.
"""

import logging
from pathlib import Path

from campaigns.context import CampaignContext
from campaigns.loader import CampaignLoader

logger = logging.getLogger(__name__)

_campaigns: dict[str, CampaignContext] = {}


def load_all_campaigns(skills_root: Path) -> None:
    """
    Discover and load every campaign with a skills directory under skills_root.
    Directories named 'common' are skipped (shared assets, not campaigns).
    """
    loader = CampaignLoader(skills_root)
    loaded = []
    failed = []

    for entry in sorted(skills_root.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name == "common":
            continue
        if not (entry / "product.md").exists():
            continue

        try:
            ctx = loader.load(entry.name)
            _campaigns[entry.name] = ctx
            loaded.append(entry.name)
        except Exception as exc:
            logger.error("Failed to load campaign '%s': %s", entry.name, exc)
            failed.append(entry.name)

    logger.info(
        "Campaigns loaded: %s%s",
        ", ".join(loaded) if loaded else "(none)",
        f" | failed: {', '.join(failed)}" if failed else "",
    )


def get_campaign(campaign_id: str) -> CampaignContext:
    if campaign_id not in _campaigns:
        available = ", ".join(_campaigns.keys()) or "(none loaded)"
        raise ValueError(
            f"Unknown campaign '{campaign_id}'. Available: {available}"
        )
    return _campaigns[campaign_id]


def reload_campaign(campaign_id: str, skills_root: Path) -> CampaignContext:
    """Reload a single campaign from disk. Used in no-DB (local dev) mode."""
    loader = CampaignLoader(skills_root)
    ctx = loader.load(campaign_id)
    _campaigns[campaign_id] = ctx
    logger.info("Reloaded campaign '%s' from disk", campaign_id)
    return ctx


def reload_campaign_from_strings(
    campaign_id: str, files: dict[str, str], skills_root: Path
) -> CampaignContext:
    """
    Rebuild a campaign context from a dict of {filename: content} and update
    the registry in-place. Used when PostgreSQL is the source of truth.
    files must include both campaign-specific files and any common files
    (evidence-extraction.md) already merged in by the caller.
    """
    loader = CampaignLoader(skills_root)
    ctx = loader.load_from_strings(campaign_id, files)
    _campaigns[campaign_id] = ctx
    logger.info("Reloaded campaign '%s' from DB content", campaign_id)
    return ctx


def remove_campaign(campaign_id: str) -> None:
    """Remove a campaign from the registry (used when a campaign directory is deleted)."""
    _campaigns.pop(campaign_id, None)


def list_campaign_ids() -> list[str]:
    return list(_campaigns.keys())


def is_loaded() -> bool:
    return bool(_campaigns)
