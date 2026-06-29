"""
Seedance Lead Qualification Engine — Web UI
Run locally:  python ui/server.py
In Docker:    set DATABASE_URL env var, then docker-compose up
"""

import asyncio
import csv
import io
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import uuid
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

import secrets

from dotenv import load_dotenv
load_dotenv()

import httpx
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.middleware.sessions import SessionMiddleware

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT       = Path(__file__).parent.parent
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", str(ROOT / "output")))
RUNS_DIR   = OUTPUT_DIR / "runs"

sys.path.insert(0, str(ROOT))
import db as store
import config
from campaigns import get_campaign, load_all_campaigns, list_campaign_ids
from campaigns.registry import reload_campaign_from_strings
from claude import extract_evidence, make_decision
from llm import LiteLLMClient
from normalization import normalize_lead
from router import route_decision
from tools import enrich_lead

logger = logging.getLogger("seedance.ui")

# ── Skills root ───────────────────────────────────────────────────────────────
SKILLS_ROOT = ROOT / "skills"

# ── In-memory job state (SSE streaming only) ──────────────────────────────────
# Keyed by run_id. Cleared after results are imported to DB.
_jobs: dict = {}

# ── DB pool (set in lifespan) ─────────────────────────────────────────────────
_pool = None

# ── Settings, exclusions, and template cache (loaded from DB at startup) ───────
_settings: dict = {"qualified_threshold": 70, "disqualified_threshold": 60, "apollo_enabled": False}
_exclusions: list = []
_templates: list = []  # all email_templates rows, refreshed on add/delete

DATABASE_URL              = os.getenv("DATABASE_URL", "")
APP_USERNAME              = os.getenv("APP_USERNAME", "admin")
APP_PASSWORD              = os.getenv("APP_PASSWORD", "changeme")
SESSION_SECRET            = os.getenv("SESSION_SECRET", secrets.token_hex(32))
INSTANTLY_API_KEY         = os.getenv("INSTANTLY_API_KEY", "")
INSTANTLY_CAMPAIGN_ID     = os.getenv("INSTANTLY_CAMPAIGN_ID", "")
INSTANTLY_SCORE_THRESHOLD = int(os.getenv("INSTANTLY_SCORE_THRESHOLD") or "75")
PLACEHOLDER_EMAIL         = os.getenv("PLACEHOLDER_EMAIL", "mittulmadaan91@gmail.com")


# ── DB-backed campaign reload ─────────────────────────────────────────────────

async def _reload_all_campaigns_from_db() -> None:
    """Reload every registered campaign's prompt context from skill_files in the DB."""
    for campaign_id in list_campaign_ids():
        try:
            files = await store.get_skill_files_for_loader(_pool, campaign_id)
            if files:
                reload_campaign_from_strings(campaign_id, files, SKILLS_ROOT)
        except Exception as exc:
            logger.error("Failed to reload campaign '%s' from DB: %s", campaign_id, exc)


async def _reload_campaign_from_db(campaign_id: str) -> list[str]:
    """
    Reload one campaign (or all if campaign_id is 'common') from the DB.
    Returns a list of campaign IDs that were successfully reloaded.
    """
    reloaded = []
    targets = list_campaign_ids() if campaign_id == "common" else [campaign_id]
    for cid in targets:
        try:
            files = await store.get_skill_files_for_loader(_pool, cid)
            if files:
                reload_campaign_from_strings(cid, files, SKILLS_ROOT)
                reloaded.append(cid)
        except Exception as exc:
            logger.warning("Hot-reload failed for campaign '%s': %s", cid, exc)
    return reloaded


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pool
    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    # Load all campaign contexts from disk first (works in no-DB mode too)
    try:
        load_all_campaigns(SKILLS_ROOT)
    except Exception as exc:
        logger.warning("Campaign loading from disk failed: %s", exc)

    if DATABASE_URL:
        try:
            _pool = await store.create_pool(DATABASE_URL)
            await store.create_tables(_pool)
            await store.mark_stale_runs(_pool)
            await store.seed_campaigns(_pool)
            s = await store.get_settings(_pool)
            _settings["qualified_threshold"]    = int(s.get("qualified_threshold", 70))
            _settings["disqualified_threshold"] = int(s.get("disqualified_threshold", 60))
            _settings["apollo_enabled"]         = s.get("apollo_enabled", "false").lower() in ("true", "1", "yes")
            os.environ["APOLLO_ENABLED"] = "true" if _settings["apollo_enabled"] else "false"
            _exclusions[:] = await store.get_exclusions(_pool)
            await store.seed_email_templates(_pool)
            _templates[:] = await store.get_email_templates(_pool)

            # Seed skill_files from disk — DO NOTHING on conflict so DB rows always win
            await store.seed_skill_files_from_disk(_pool, SKILLS_ROOT)
            # Reload every campaign from DB content (DB is now source of truth)
            await _reload_all_campaigns_from_db()

            logger.info("PostgreSQL connected: %s", DATABASE_URL.split("@")[-1])
        except Exception as exc:
            logger.error("DB connection failed: %s — running without persistence", exc)
            _pool = None
    else:
        logger.warning("DATABASE_URL not set — results will not persist across restarts")
        # In no-DB mode, read apollo_enabled directly from env (loaded from .env above)
        _settings["apollo_enabled"] = os.getenv("APOLLO_ENABLED", "false").lower() in ("true", "1", "yes")

    yield

    if _pool:
        await _pool.close()


app = FastAPI(lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, max_age=60 * 60 * 8)


# ── Health check (no auth — used by ALB and k8s probes) ──────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "db": bool(DATABASE_URL)}


# ── Debug client-event logging ────────────────────────────────────────────────

@app.get("/api/debug/client-event")
async def api_debug_client_event(request: Request):
    _require_auth(request)
    stage = request.query_params.get("stage", "unknown")
    detail = request.query_params.get("detail", "")
    campaign_id = request.query_params.get("campaign_id", "")
    name = request.query_params.get("name", "")
    filename = request.query_params.get("filename", "")
    size = request.query_params.get("size", "")
    err = request.query_params.get("err", "")
    logger.warning(
        "CLIENT_DEBUG stage=%s detail=%s campaign_id=%s name=%s filename=%s size=%s err=%s online=%s cookies=%s origin=%s referer=%s ua=%s",
        stage,
        detail,
        campaign_id,
        name,
        filename,
        size,
        err,
        request.query_params.get("online", ""),
        request.query_params.get("cookies", ""),
        request.headers.get("origin"),
        request.headers.get("referer"),
        request.headers.get("user-agent"),
    )
    return JSONResponse({"ok": True})


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _is_authenticated(request: Request) -> bool:
    return request.session.get("authenticated") is True


def _require_auth(request: Request):
    if not _is_authenticated(request):
        # #region debug-point C:auth-rejected
        try:
            import json, urllib.request; urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:7777/event", data=json.dumps({"sessionId":"skill-upload-403","runId":"pre-fix","hypothesisId":"C","location":"ui/server.py:_require_auth","msg":"[DEBUG] auth rejected","data":{"path":str(request.url.path),"method":request.method,"has_cookie":bool(request.headers.get("cookie")),"origin":request.headers.get("origin"),"referer":request.headers.get("referer"),"authenticated":False}}).encode(), headers={"Content-Type":"application/json"}), timeout=0.4).read()
        except Exception:
            pass
        # #endregion
        raise HTTPException(status_code=401)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _apply_thresholds(leads: list) -> list:
    qt = _settings["qualified_threshold"]
    dt = _settings["disqualified_threshold"]
    result = []
    for lead in leads:
        l = dict(lead)
        if l.get("hard_disqualifiers"):
            l["decision"] = "disqualified"
        elif l["score"] >= qt:
            l["decision"] = "qualified"
        elif l["score"] < dt:
            l["decision"] = "disqualified"
        else:
            l["decision"] = "review"
        result.append(l)
    return result


def _normalize_url(url: str) -> str:
    url = (url or "").strip().lower()
    url = re.sub(r'^https?://', '', url)
    url = re.sub(r'^www\.', '', url)
    return url.split('/')[0].rstrip('.')


def _excl_url_set() -> set:
    return {_normalize_url(e["value"]) for e in _exclusions if e.get("value")}


def _is_excluded(lead: dict) -> bool:
    urls = _excl_url_set()
    if not urls:
        return False
    lead_url = _normalize_url(lead.get("company_website") or "")
    return bool(lead_url and lead_url in urls)


def _mark_exclusions(leads: list) -> list:
    urls = _excl_url_set()
    if not urls:
        return leads
    result = []
    for lead in leads:
        l = dict(lead)
        l["excluded"] = _normalize_url(l.get("company_website") or "") in urls
        result.append(l)
    return result


def _read_run_files(run_dir: Path) -> list:
    """Parse output CSVs + artifact JSONs from a completed run directory."""
    leads = []
    def _extract_source_url(signal_text: str) -> str:
        match = re.search(r"\(Post:\s*(https?://[^\s)]+)\)", signal_text or "")
        return match.group(1) if match else ""

    for fname, decision in [
        ("qualified_leads.csv",    "qualified"),
        ("review_leads.csv",       "review"),
        ("disqualified_leads.csv", "disqualified"),
    ]:
        path = run_dir / fname
        if not path.exists():
            continue
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                lid = row.get("lead_id", "")
                art, ev, enrichment = {}, {}, {}
                input_payload = {}
                art_p = run_dir / "artifacts" / f"{lid}_decision.json"
                ev_p  = run_dir / "artifacts" / f"{lid}_evidence.json"
                enrichment_p = run_dir / "artifacts" / f"{lid}_enrichment.json"
                input_p = run_dir / "artifacts" / f"{lid}_input.json"
                if art_p.exists():
                    art = json.loads(art_p.read_text())
                if ev_p.exists():
                    ev = json.loads(ev_p.read_text())
                if enrichment_p.exists():
                    enrichment = json.loads(enrichment_p.read_text())
                if input_p.exists():
                    input_payload = json.loads(input_p.read_text())
                leads.append({
                    "lead_id":        lid,
                    "name":           row.get("person_name", ""),
                    "company":        row.get("company_name", ""),
                    "company_size":   input_payload.get("company_size", ""),
                    "industry":       ev.get("segment", ""),
                    "linkedin_url":   input_payload.get("linkedin_url", ""),
                    "company_website": input_payload.get("company_domain", ""),
                    "role":           row.get("title", ""),
                    "signal_type":    input_payload.get("signal_type", ""),
                    "source_url":     _extract_source_url(input_payload.get("signal_text", "")),
                    "decision":       decision,
                    "score":          int(float(row.get("score", 0))),
                    "conf":           row.get("confidence", ""),
                    "segment":        art.get("segment_fit", 0),
                    "workflow":       art.get("workflow_fit", 0),
                    "pain":           art.get("pain_fit", 0),
                    "buyer":          art.get("buyer_fit", 0),
                    "commercial":     art.get("commercial_fit", 0),
                    "reasons":        art.get("qualification_reasons", []),
                    "disreasons":     art.get("disqualification_reasons", []),
                    "usecase":        art.get("suggested_use_case") or row.get("use_case", ""),
                    "signal_strength": ev.get("signal_strength", ""),
                    "observed":       ev.get("observed_evidence", []),
                    "inferred":       ev.get("inferred_evidence", []),
                    "unknowns":       ev.get("unknowns", []),
                    "hard_disqualifiers": ev.get("hard_disqualifiers", []),
                    "reasoning":      art.get("reasoning_summary") or row.get("reasoning_summary", ""),
                    "email":          enrichment.get("email") or row.get("email", "") or "",
                    "email_status":   enrichment.get("email_status") or "",
                    "raw_input":      input_payload,
                    "enrichment_snapshot": enrichment,
                    "evidence_snapshot": ev,
                    "decision_snapshot": art,
                })
    leads.sort(key=lambda x: x["score"], reverse=True)
    return leads


def _summarize_run_dir(run_dir: Path) -> dict:
    leads = _read_run_files(run_dir)
    counts = {"qualified": 0, "review": 0, "disqualified": 0}
    for lead in leads:
        if lead["decision"] in counts:
            counts[lead["decision"]] += 1
    meta: dict = {}
    meta_path = run_dir / "run_meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "run_id": run_dir.name,
        "filename": run_dir.name,
        "status": "done",
        "lead_count": len(leads),
        "n_qualified": counts["qualified"],
        "n_review": counts["review"],
        "n_disqualified": counts["disqualified"],
        "created_at": datetime.fromtimestamp(run_dir.stat().st_mtime).isoformat(),
        "apollo_enabled": bool(meta.get("apollo_enabled", False)),
    }


def _require_db():
    if not _pool:
        raise HTTPException(status_code=400, detail="Database required")


def _memory_history_summary(memories: list[dict]) -> dict:
    counts = {"qualified": 0, "review": 0, "disqualified": 0}
    for memory in memories:
        routing = memory.get("routing")
        if routing in counts:
            counts[routing] += 1
    return {
        "times_seen": len(memories),
        "qualified_count": counts["qualified"],
        "review_count": counts["review"],
        "disqualified_count": counts["disqualified"],
        "last_score": (memories[0].get("decision_snapshot", {}) or {}).get("score") if memories else None,
    }


def _make_profile_signal_text(company_name: str, person_name: str, title: str) -> str:
    company = (company_name or "Unknown company").strip()
    person = (person_name or "Unknown person").strip()
    role = (title or "unknown role").strip()
    return f"Profile request for {person} ({role}) at {company}. Determine fit based on available company and persona context."


def _make_profile_lead_payload(body: dict) -> dict:
    company_name = (body.get("company_name") or body.get("company") or "").strip()
    person_name = (body.get("person_name") or body.get("name") or "").strip()
    title = (body.get("title") or body.get("role") or "Unknown").strip()
    company_domain = (body.get("domain") or body.get("company_domain") or "").strip()
    linkedin_url = (body.get("linkedin_url") or "").strip() or None
    signal_text = (body.get("signal_text") or "").strip()
    if not signal_text:
        signal_text = _make_profile_signal_text(company_name, person_name, title)
    return {
        "lead_id": str(uuid.uuid4()),
        "person_name": person_name or "Unknown Person",
        "title": title or "Unknown",
        "company_name": company_name or "Unknown Company",
        "company_domain": company_domain,
        "company_size": (body.get("company_size") or "").strip() or None,
        "linkedin_url": linkedin_url,
        "signal_text": signal_text,
        "signal_type": (body.get("signal_type") or "profile_request").strip(),
        "signal_timestamp": datetime.utcnow().isoformat(),
    }


def _fit_response(decision, evidence, routing: str, memory_used: list[str], history: list[dict]) -> dict:
    return {
        "verdict": routing,
        "confidence": decision.confidence,
        "score": decision.score,
        "signal_strength": evidence.signal_strength,
        "segment": evidence.segment,
        "why_for": decision.qualification_reasons,
        "why_against": decision.disqualification_reasons,
        "reasoning_summary": decision.reasoning_summary,
        "suggested_use_case": decision.suggested_use_case,
        "memory_used": memory_used,
        "history_summary": _memory_history_summary(history),
    }


async def _get_effective_versions(campaign_id: str) -> tuple[str, str]:
    if not _pool:
        return "v1", "v1"
    active_campaign = await store.get_active_campaign_version(_pool, campaign_id)
    active_policy = await store.get_active_policy_version(_pool, campaign_id)
    return (
        active_campaign["version"] if active_campaign else "v1",
        active_policy["policy_version"] if active_policy else "v1",
    )


def _safe_json_object(text: str, fallback: Optional[dict] = None) -> dict:
    fallback = fallback or {}
    raw = (text or "").strip()
    if not raw:
        return dict(fallback)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return dict(fallback)
    return value if isinstance(value, dict) else dict(fallback)


def _get_supported_upload_suffix(upload: UploadFile) -> str:
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix in {".csv", ".json"}:
        return suffix
    raise HTTPException(status_code=400, detail="Unsupported file type. Upload a .csv or .json file.")


async def _sync_seedance_threshold_cache(campaign_id: str, qt: int, dt: int) -> None:
    if campaign_id == "seedance":
        _settings["qualified_threshold"] = qt
        _settings["disqualified_threshold"] = dt


async def _apply_campaign_version_runtime(version_row: dict) -> None:
    campaign_id = version_row["campaign_id"]
    file_map = {
        "config.json": version_row.get("config_json", ""),
        "product.md": version_row.get("product_md", ""),
        "icp.md": version_row.get("icp_md", ""),
        "signals.md": version_row.get("signals_md", ""),
        "disqualification.md": version_row.get("disqualification_md", ""),
        "qualification.md": version_row.get("qualification_md", ""),
    }
    for filename, content in file_map.items():
        await store.upsert_skill_file(_pool, campaign_id, filename, content)

    cfg = _safe_json_object(version_row.get("config_json", ""))
    qt = int(cfg.get("qualified_threshold", 70))
    dt = int(cfg.get("disqualified_threshold", 60))
    await store.save_settings(_pool, {
        "qualified_threshold": str(qt),
        "disqualified_threshold": str(dt),
    }, campaign_id=campaign_id)
    await _sync_seedance_threshold_cache(campaign_id, qt, dt)
    await _reload_campaign_from_db(campaign_id)


async def _apply_policy_version_runtime(campaign_id: str, policy_row: dict) -> dict:
    current_config = await store.get_skill_file(_pool, campaign_id, "config.json") or "{}"
    cfg = _safe_json_object(current_config)
    cfg["score_weights"] = policy_row.get("score_weights") or cfg.get("score_weights", {})
    cfg["qualified_threshold"] = int(policy_row["qualified_threshold"])
    cfg["disqualified_threshold"] = int(policy_row["disqualified_threshold"])
    updated_config = json.dumps(cfg, indent=2, ensure_ascii=False)

    await store.upsert_skill_file(_pool, campaign_id, "config.json", updated_config)
    await store.save_settings(_pool, {
        "qualified_threshold": str(policy_row["qualified_threshold"]),
        "disqualified_threshold": str(policy_row["disqualified_threshold"]),
    }, campaign_id=campaign_id)
    await _sync_seedance_threshold_cache(
        campaign_id,
        int(policy_row["qualified_threshold"]),
        int(policy_row["disqualified_threshold"]),
    )
    await _reload_campaign_from_db(campaign_id)
    return cfg


# ── Email helpers ─────────────────────────────────────────────────────────────

# Maps LLM segment values → template vertical keys
_SEGMENT_TO_VERTICAL = {
    "ai_tools":            "ai_tools",
    "film_studio":         "film_production",
    "film_production":     "film_production",
    "broadcast_network":   "film_production",
    "television_production": "film_production",
    "media_production_org": "film_production",
    "vfx_production":      "vfx_production",
    "animation":           "animation",
    "short_drama":         "short_drama",
    "creative_agency":     "ecommerce",
    "creative_performance": "ecommerce",
    "content_creator":     "ecommerce",
    "advertising":         "ecommerce",
    "marketing_scale":     "ecommerce",
    "unknown":             "ai_generic",
}


def _lead_vertical(lead: dict) -> str:
    """Resolve a lead's vertical key from its segment/industry classification."""
    industry = (lead.get("industry") or "").strip().lower()
    return _SEGMENT_TO_VERTICAL.get(industry, "ai_generic")


def _resolve_instantly_campaign(lead: dict, routing_map: dict) -> str:
    """Pick the Instantly campaign for a lead.

    Priority: per-lead override → vertical routing map → global default env var.
    """
    override = (lead.get("instantly_campaign_id") or "").strip()
    if override:
        return override
    mapped = routing_map.get(_lead_vertical(lead)) if routing_map else None
    return mapped or INSTANTLY_CAMPAIGN_ID


def _build_email(lead: dict) -> dict:
    first_name = (lead.get("name") or "there").split()[0]
    company    = lead.get("company") or "your company"
    vertical   = _lead_vertical(lead)

    # Find matching template from cache (first match for vertical wins)
    tmpl = next((t for t in _templates if t["vertical"] == vertical), None)
    if tmpl is None:
        # Fall back to ai_generic
        tmpl = next((t for t in _templates if t["vertical"] == "ai_generic"), None)

    if tmpl:
        subject = tmpl["subject"].replace("{first_name}", first_name).replace("{company}", company)
        body    = tmpl["body"].replace("{first_name}", first_name).replace("{company}", company)
    else:
        # Hardcoded fallback if DB has no templates yet
        subject = f"Cortex 2.0 + {company}?"
        body    = f"Hi {first_name},\n\nWould this be a good time to explore ByteDance models and potential partnerships?\n\n/Antti\nBook 30min: https://bytedance.sg.larkoffice.com/scheduler/d18e3ecd70eae8c6"

    return {"subject": subject, "body": body, "vertical": vertical}


def _is_unresolvable_email(email: str) -> bool:
    """Pre-filter domains known to produce esp_code=999 in Instantly."""
    domain = email.split("@")[-1].lower() if "@" in email else ""
    # Corporate domains that Instantly can't resolve or that block external mail
    _BLOCKED = {"bytedance.com", "byteplus.com", "tiktok.com"}
    return domain in _BLOCKED


def _instantly_log(entry: dict) -> None:
    """Append a timestamped JSON entry to the Instantly debug log."""
    import datetime
    log_dir = OUTPUT_DIR / "instantly_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "instantly_debug.jsonl"
    entry["ts"] = datetime.datetime.utcnow().isoformat()
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


async def _instantly_call(client: httpx.AsyncClient, method: str, url: str, **kwargs) -> httpx.Response:
    """Make an Instantly API call and log the full request/response."""
    resp = await client.request(method, url, **kwargs)
    _instantly_log({
        "method": method,
        "url": url,
        "request_body": kwargs.get("json"),
        "status": resp.status_code,
        "response": resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text[:500],
    })
    return resp


async def _send_to_instantly(leads: list, campaign_id: str = "") -> list:
    """Add leads to one Instantly campaign and ensure it is active.
    Returns list of sent lead_ids. Falls back to the global default campaign."""
    campaign_id = (campaign_id or INSTANTLY_CAMPAIGN_ID or "").strip()
    if not campaign_id:
        logger.warning("No Instantly campaign id for send batch — skipping")
        return []
    ssl_verify = os.getenv("LLM_SSL_VERIFY", "true").lower() not in ("false", "0", "no")
    sent_ids = []
    instantly_id_map: dict[str, str] = {}

    async with httpx.AsyncClient(
        timeout=30,
        verify=ssl_verify,
        headers={"Authorization": f"Bearer {INSTANTLY_API_KEY}"},
    ) as client:

        # Step 1: Check campaign state before adding leads
        try:
            r = await _instantly_call(client, "GET",
                f"https://api.instantly.ai/api/v2/campaigns/{campaign_id}")
            pre_status = r.json().get("status")
            pre_nss = r.json().get("not_sending_status")
            logger.info("Instantly campaign pre-send: status=%s not_sending_status=%s",
                        pre_status, pre_nss)
        except Exception as exc:
            logger.warning("Could not fetch campaign state: %s", exc)
            pre_status = None

        # Step 2: Add eligible leads
        for lead in leads:
            email = (lead.get("email") or "").strip()
            if not email:
                logger.warning("Skipping lead %s — no email address", lead.get("lead_id"))
                continue
            if _is_unresolvable_email(email):
                logger.warning("Skipping %s — domain blocked (esp_code=999 domain)", email)
                continue
            name = (lead.get("name") or "").strip()
            parts = name.split(None, 1)
            built = _build_email(lead)
            payload = {
                "email":           email,
                "first_name":      parts[0] if parts else "",
                "last_name":       parts[1] if len(parts) > 1 else "",
                "company_name":    lead.get("company") or "",
                "website":         lead.get("company_website") or "",
                "personalization": built["body"],
                # Cortex-rendered per-vertical subject — forwarded as a custom
                # variable, referenced as {{email_subject}} in the Instantly sequence.
                "custom_variables": {"email_subject": built["subject"]},
                "campaign":        campaign_id,
            }
            resp = await _instantly_call(client, "POST",
                "https://api.instantly.ai/api/v2/leads", json=payload)
            if not resp.is_success:
                logger.error("Instantly API error %d for %s: %s",
                             resp.status_code, email, resp.text)
                resp.raise_for_status()
            data = resp.json()
            instantly_lead_id = data.get("id", "")
            if data.get("esp_code") == 999:
                if instantly_lead_id:
                    try:
                        await _instantly_call(client, "DELETE",
                            f"https://api.instantly.ai/api/v2/leads/{instantly_lead_id}")
                    except Exception:
                        pass
                logger.warning("Skipping %s — esp_code=999 on response", email)
                continue
            sent_ids.append(lead["lead_id"])
            if instantly_lead_id:
                instantly_id_map[instantly_lead_id] = lead["lead_id"]
            logger.info("Sent to Instantly: %s <%s> (esp=%s)", lead.get("name"), email, data.get("esp_code"))

        if not sent_ids:
            return sent_ids

        # Step 3: Activate if not already active (draft=0, paused=2, completed=3)
        try:
            r = await _instantly_call(client, "GET",
                f"https://api.instantly.ai/api/v2/campaigns/{campaign_id}")
            status = r.json().get("status")
            if status == 1:
                logger.info("Campaign already active — %d lead(s) queued", len(sent_ids))
            else:
                logger.info("Campaign status=%s — activating with %d lead(s)", status, len(sent_ids))
                act = await _instantly_call(client, "POST",
                    f"https://api.instantly.ai/api/v2/campaigns/{campaign_id}/activate")
                logger.info("Activate response: status=%s not_sending_status=%s",
                            act.json().get("status"), act.json().get("not_sending_status"))
        except Exception as exc:
            logger.warning("Could not manage Instantly campaign status: %s", exc)

        # Step 4: Wait 5s for async ESP resolution, purge any 999s
        await asyncio.sleep(5)
        try:
            r = await _instantly_call(client, "POST",
                "https://api.instantly.ai/api/v2/leads/list",
                json={"campaign_id": campaign_id, "limit": 50})
            for il in r.json().get("items", []):
                if il.get("esp_code") == 999 and il["id"] in instantly_id_map:
                    our_lead_id = instantly_id_map[il["id"]]
                    await _instantly_call(client, "DELETE",
                        f"https://api.instantly.ai/api/v2/leads/{il['id']}")
                    sent_ids = [lid for lid in sent_ids if lid != our_lead_id]
                    logger.warning("Removed async esp_code=999 lead: %s", il.get("email"))
        except Exception as exc:
            logger.warning("Post-send esp cleanup failed: %s", exc)

    return sent_ids


# ── API: Instantly debug log ─────────────────────────────────────────────────

@app.get("/api/instantly/debug-log")
async def api_instantly_debug_log(request: Request, lines: int = 50):
    _require_auth(request)
    log_file = OUTPUT_DIR / "instantly_logs" / "instantly_debug.jsonl"
    if not log_file.exists():
        return JSONResponse({"entries": [], "message": "No log yet — trigger a Send to Instantly first"})
    entries = []
    with open(log_file, encoding="utf-8") as f:
        all_lines = f.readlines()
    for line in all_lines[-lines:]:
        try:
            entries.append(json.loads(line))
        except Exception:
            pass
    return JSONResponse({"entries": entries, "total_lines": len(all_lines)})


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return HTMLResponse(_LOGIN_HTML)


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if secrets.compare_digest(username, APP_USERNAME) and secrets.compare_digest(password, APP_PASSWORD):
        request.session["authenticated"] = True
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(_LOGIN_HTML.replace("<!--ERROR-->", '<p class="login-error">Invalid username or password</p>'), status_code=401)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ── API: run pipeline ─────────────────────────────────────────────────────────

@app.post("/api/run")
async def run_pipeline(request: Request, file: UploadFile = File(...), concurrency: int = Form(10),
                       campaign: str = Form("seedance")):
    _require_auth(request)
    run_id  = str(uuid.uuid4())
    content = await file.read()
    suffix = _get_supported_upload_suffix(file)

    # Save upload to a temp file for main.py to read
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(content)
    tmp.close()

    # Per-run isolated output directory
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # Persist run record with campaign_id
    apollo_on = _settings.get("apollo_enabled", False)
    if _pool:
        campaign_version, policy_version = await _get_effective_versions(campaign)
        await store.insert_run(
            _pool, run_id, file.filename or f"upload{suffix}", concurrency,
            campaign_id=campaign, campaign_version=campaign_version, policy_version=policy_version,
            apollo_enabled=apollo_on,
        )
    (run_dir / "run_meta.json").write_text(
        json.dumps({"apollo_enabled": apollo_on, "campaign_id": campaign}), encoding="utf-8"
    )

    _jobs[run_id] = {"lines": [], "status": "running", "returncode": None, "campaign_id": campaign}
    asyncio.create_task(_run_job(run_id, tmp.name, run_dir, concurrency, campaign_id=campaign))

    return JSONResponse({"run_id": run_id})


# ── Campaign-scoped upload endpoint ──────────────────────────────────────────

@app.post("/api/campaigns/{campaign_id}/run")
async def run_pipeline_campaign(request: Request, campaign_id: str,
                                file: UploadFile = File(...), concurrency: int = Form(10)):
    """Campaign-scoped upload — identical to /api/run but campaign is in the URL path."""
    _require_auth(request)
    known = list_campaign_ids()
    if known and campaign_id not in known:
        return JSONResponse({"error": f"Unknown campaign '{campaign_id}'. Available: {', '.join(known)}"}, status_code=400)

    run_id  = str(uuid.uuid4())
    content = await file.read()
    suffix = _get_supported_upload_suffix(file)

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(content)
    tmp.close()

    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    apollo_on = _settings.get("apollo_enabled", False)
    if _pool:
        campaign_version, policy_version = await _get_effective_versions(campaign_id)
        await store.insert_run(
            _pool, run_id, file.filename or f"upload{suffix}", concurrency,
            campaign_id=campaign_id, campaign_version=campaign_version, policy_version=policy_version,
            apollo_enabled=apollo_on,
        )
    (run_dir / "run_meta.json").write_text(
        json.dumps({"apollo_enabled": apollo_on, "campaign_id": campaign_id}), encoding="utf-8"
    )

    _jobs[run_id] = {"lines": [], "status": "running", "returncode": None, "campaign_id": campaign_id}
    asyncio.create_task(_run_job(run_id, tmp.name, run_dir, concurrency, campaign_id=campaign_id))

    return JSONResponse({"run_id": run_id, "campaign_id": campaign_id})


async def _run_job(run_id: str, csv_path: str, run_dir: Path, concurrency: int, campaign_id: str = "seedance"):
    cmd = [
        sys.executable, str(ROOT / "main.py"),
        "--input",       csv_path,
        "--output-dir",  str(run_dir),
        "--concurrency", str(max(5, min(20, concurrency))),
        "--campaign",    campaign_id,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(ROOT),
        )
        async for raw in proc.stdout:
            _jobs[run_id]["lines"].append(raw.decode().rstrip())
        await proc.wait()

        rc = proc.returncode
        _jobs[run_id]["returncode"] = rc
        _jobs[run_id]["status"]     = "done" if rc == 0 else "error"

        # Import results into PostgreSQL, then clean up temp files
        if _pool:
            await _import_to_db(run_id, run_dir, rc, campaign_id=campaign_id)
        # Remove temp run directory — data is now in DB (or kept if no DB)
        if _pool and run_dir.exists():
            shutil.rmtree(run_dir, ignore_errors=True)

    except Exception as exc:
        _jobs[run_id]["lines"].append(f"ERROR: {exc}")
        _jobs[run_id]["status"]     = "error"
        _jobs[run_id]["returncode"] = 1
        if _pool:
            await store.update_run(_pool, run_id, "error", returncode=1)
    finally:
        try:
            os.unlink(csv_path)
        except OSError:
            pass


async def _import_to_db(run_id: str, run_dir: Path, returncode: int, campaign_id: str = "seedance"):
    leads = _read_run_files(run_dir)
    routing_map = await store.get_routing_map(_pool)
    for lead in leads:
        await store.upsert_lead(_pool, run_id, lead)
        # Persist only an explicit vertical-rule match. Leads with no rule stay
        # NULL so they resolve to the live map/default at send time — meaning a
        # routing rule added *after* the run still applies to them.
        mapped = routing_map.get(_lead_vertical(lead)) if routing_map else None
        if mapped:
            await store.set_lead_instantly_campaign(
                _pool, run_id, lead.get("lead_id", ""), mapped)
            lead["instantly_campaign_id"] = mapped
        fit_summary = {
            "decision": lead.get("decision"),
            "score": lead.get("score", 0),
            "confidence": lead.get("conf", ""),
            "signal_strength": lead.get("signal_strength", ""),
        }
        company_profile = await store.upsert_company_profile(
            _pool,
            lead.get("company", ""),
            lead.get("company_website", ""),
            structured_profile={
                "company_size": lead.get("company_size", ""),
                "industry": lead.get("industry", ""),
                "company_website": lead.get("company_website", ""),
            },
            summary=lead.get("reasoning", ""),
            campaign_id=campaign_id,
            fit_summary=fit_summary,
            run_id=run_id,
        )
        person_profile = await store.upsert_person_profile(
            _pool,
            lead.get("name", ""),
            linkedin_url=lead.get("linkedin_url", ""),
            company_profile_id=company_profile["id"] if company_profile else None,
            title=lead.get("role", ""),
            summary=lead.get("reasoning", ""),
            campaign_id=campaign_id,
            fit_summary=fit_summary,
            run_id=run_id,
            known_email=lead.get("email") or None,
        )
        # Back-fill email from person profile if Apollo didn't find one
        if not lead.get("email") and person_profile and person_profile.get("known_email"):
            await _pool.execute(
                "UPDATE leads SET email=$1 WHERE run_id=$2 AND lead_id=$3",
                person_profile["known_email"], run_id, lead.get("lead_id", ""),
            )
            lead["email"] = person_profile["known_email"]
            logger.info("Back-filled email for %s from person profile: %s",
                        lead.get("name"), person_profile["known_email"])
        await store.insert_lead_memory(
            _pool,
            run_id=run_id,
            lead_id=lead.get("lead_id", ""),
            campaign_id=campaign_id,
            routing=lead.get("decision", "review"),
            campaign_version="v1",
            policy_version="v1",
            company_profile_id=company_profile["id"] if company_profile else None,
            person_profile_id=person_profile["id"] if person_profile else None,
            raw_input=lead.get("raw_input", {}),
            enrichment_snapshot=lead.get("enrichment_snapshot", {}),
            evidence_snapshot=lead.get("evidence_snapshot", {}),
            decision_snapshot=lead.get("decision_snapshot", {}),
        )
    await store.update_run(
        _pool, run_id,
        status="done" if returncode == 0 else "error",
        returncode=returncode,
        lead_count=len(leads),
    )


# ── API: SSE progress stream ──────────────────────────────────────────────────

@app.get("/api/progress/{run_id}")
async def progress(request: Request, run_id: str):
    _require_auth(request)
    if run_id not in _jobs:
        return JSONResponse({"error": "not found"}, status_code=404)

    async def _stream():
        sent = 0
        while True:
            job = _jobs[run_id]
            while sent < len(job["lines"]):
                yield f"data: {json.dumps({'line': job['lines'][sent]})}\n\n"
                sent += 1
            if job["status"] != "running":
                yield f"data: {json.dumps({'done': True, 'returncode': job['returncode']})}\n\n"
                break
            await asyncio.sleep(0.15)

    return StreamingResponse(_stream(), media_type="text/event-stream")


# ── API: results ──────────────────────────────────────────────────────────────

@app.get("/api/results/{run_id}")
async def get_results(request: Request, run_id: str):
    _require_auth(request)
    if _pool:
        leads = await store.get_leads(_pool, run_id)
    elif (RUNS_DIR / run_id).exists():
        leads = _read_run_files(RUNS_DIR / run_id)
    else:
        return JSONResponse([], status_code=404)
    return JSONResponse(_mark_exclusions(_apply_thresholds(leads)))


# ── API: agent memory and profiling ───────────────────────────────────────────

@app.get("/api/agent/runs/{run_id}")
async def api_agent_get_run(request: Request, run_id: str):
    _require_auth(request)
    _require_db()
    run = await store.get_run(_pool, run_id)
    if run is None:
        return JSONResponse({"error": "Run not found"}, status_code=404)
    return JSONResponse(run)


@app.get("/api/agent/runs/{run_id}/results")
async def api_agent_get_run_results(
    request: Request,
    run_id: str,
    decision: str = "",
    min_score: int = 0,
    max_score: int = 100,
    limit: int = 100,
    offset: int = 0,
):
    _require_auth(request)
    _require_db()
    run = await store.get_run(_pool, run_id)
    if run is None:
        return JSONResponse({"error": "Run not found"}, status_code=404)

    leads = await store.get_leads(_pool, run_id)
    leads = _mark_exclusions(_apply_thresholds(leads))
    if decision:
        leads = [l for l in leads if l.get("decision") == decision]
    leads = [
        l for l in leads
        if min_score <= int(l.get("score", 0)) <= max_score
    ]
    start = max(0, offset)
    end = start + max(1, min(limit, 500))
    return JSONResponse({
        "run": run,
        "count": len(leads),
        "results": leads[start:end],
    })


@app.get("/api/agent/runs/{run_id}/artifacts/{lead_id}")
async def api_agent_get_run_artifacts(request: Request, run_id: str, lead_id: str):
    _require_auth(request)
    _require_db()
    run = await store.get_run(_pool, run_id)
    if run is None:
        return JSONResponse({"error": "Run not found"}, status_code=404)

    artifact = await store.get_lead_memory_item(_pool, run_id, lead_id)
    if artifact is None:
        return JSONResponse({"error": "Lead artifacts not found"}, status_code=404)
    return JSONResponse({
        "run": run,
        "artifact": artifact,
    })


@app.post("/api/agent/profile/company")
async def api_agent_profile_company(request: Request):
    _require_auth(request)
    _require_db()
    body = await request.json()
    campaign_id = (body.get("campaign_id") or "seedance").strip()
    company_name = (body.get("company_name") or "").strip()
    domain = (body.get("domain") or "").strip()
    refresh_enrichment = bool(body.get("refresh_enrichment", False))

    profile = None
    if domain:
        profile = await store.get_company_profile_by_domain(_pool, domain)
    if profile is None and company_name:
        matches = await store.search_company_profiles(_pool, company_name, limit=1)
        profile = matches[0] if matches else None
    if profile is None and (company_name or domain):
        profile = await store.upsert_company_profile(
            _pool,
            company_name or domain,
            domain,
            summary="",
            campaign_id=campaign_id,
            fit_summary={},
        )

    if profile is None:
        return JSONResponse({"error": "company_name or domain is required"}, status_code=400)

    history = await store.get_lead_memory_for_company(_pool, profile["id"], campaign_id=campaign_id)
    return JSONResponse({
        "company_profile": profile,
        "history": _memory_history_summary(history),
        "fit_summary": {
            "campaign_id": campaign_id,
            "last_known_fit": history[0]["routing"] if history else None,
            "last_score": (history[0].get("decision_snapshot", {}) or {}).get("score") if history else None,
        },
        "refresh_enrichment_requested": refresh_enrichment,
    })


@app.post("/api/agent/profile/person")
async def api_agent_profile_person(request: Request):
    _require_auth(request)
    _require_db()
    body = await request.json()
    campaign_id = (body.get("campaign_id") or "seedance").strip()
    person_name = (body.get("person_name") or "").strip()
    linkedin_url = (body.get("linkedin_url") or "").strip()
    company_domain = (body.get("company_domain") or "").strip()

    company_profile = None
    if company_domain:
        company_profile = await store.get_company_profile_by_domain(_pool, company_domain)

    profile = await store.get_person_profile(
        _pool,
        person_name=person_name,
        linkedin_url=linkedin_url,
        company_profile_id=company_profile["id"] if company_profile else None,
    )
    if profile is None and (person_name or linkedin_url):
        profile = await store.upsert_person_profile(
            _pool,
            person_name or linkedin_url,
            linkedin_url=linkedin_url,
            company_profile_id=company_profile["id"] if company_profile else None,
            campaign_id=campaign_id,
            fit_summary={},
        )

    if profile is None:
        return JSONResponse({"error": "person_name or linkedin_url is required"}, status_code=400)

    history = await store.get_lead_memory_for_person(_pool, profile["id"], campaign_id=campaign_id)
    return JSONResponse({
        "person_profile": profile,
        "history": _memory_history_summary(history),
        "fit_summary": {
            "campaign_id": campaign_id,
            "last_known_fit": history[0]["routing"] if history else None,
            "last_score": (history[0].get("decision_snapshot", {}) or {}).get("score") if history else None,
        },
    })


@app.post("/api/agent/profile/fit")
async def api_agent_profile_fit(request: Request):
    _require_auth(request)
    _require_db()
    body = await request.json()
    campaign_id = (body.get("campaign_id") or "seedance").strip()
    use_memory = bool(body.get("use_memory", True))
    refresh_enrichment = bool(body.get("refresh_enrichment", False))

    try:
        campaign = get_campaign(campaign_id)
    except Exception as exc:
        return JSONResponse({"error": f"Unknown campaign '{campaign_id}': {exc}"}, status_code=400)

    if not config.LLM_API_KEY:
        return JSONResponse({"error": "LLM_API_KEY environment variable is not set"}, status_code=400)
    if not config.LLM_MODEL:
        return JSONResponse({"error": "LLM_MODEL environment variable is not set"}, status_code=400)

    lead_payload = _make_profile_lead_payload(body)
    lead = normalize_lead(lead_payload)
    enriched = await enrich_lead(lead)
    evidence = await extract_evidence(
        LiteLLMClient(
            model=config.LLM_MODEL,
            api_key=config.LLM_API_KEY,
            provider=config.LLM_PROVIDER,
            api_base=config.LLM_API_BASE,
            timeout_seconds=config.LLM_TIMEOUT_SECONDS,
            thinking_budget=config.LLM_THINKING_BUDGET,
        ),
        enriched,
        campaign=campaign,
    )
    decision = await make_decision(
        LiteLLMClient(
            model=config.LLM_MODEL,
            api_key=config.LLM_API_KEY,
            provider=config.LLM_PROVIDER,
            api_base=config.LLM_API_BASE,
            timeout_seconds=config.LLM_TIMEOUT_SECONDS,
            thinking_budget=config.LLM_THINKING_BUDGET,
        ),
        evidence,
        campaign=campaign,
    )
    routing = route_decision(decision, evidence, campaign=campaign)
    decision = decision.model_copy(update={"decision": routing})

    company_profile = None
    person_profile = None
    memory_used: list[str] = []
    company_history: list[dict] = []
    person_history: list[dict] = []
    if use_memory:
        if lead.company_domain:
            company_profile = await store.get_company_profile_by_domain(_pool, lead.company_domain)
        if company_profile is None and lead.company_name:
            matches = await store.search_company_profiles(_pool, lead.company_name, limit=1)
            company_profile = matches[0] if matches else None
        if company_profile:
            company_history = await store.get_lead_memory_for_company(_pool, company_profile["id"], campaign_id=campaign_id)
            memory_used.append(f"{len(company_history)} company history items")

        person_profile = await store.get_person_profile(
            _pool,
            person_name=lead.person_name,
            linkedin_url=lead.linkedin_url or "",
            company_profile_id=company_profile["id"] if company_profile else None,
        )
        if person_profile:
            person_history = await store.get_lead_memory_for_person(_pool, person_profile["id"], campaign_id=campaign_id)
            memory_used.append(f"{len(person_history)} person history items")

    history = company_history or person_history
    return JSONResponse({
        "campaign_id": campaign_id,
        "input": lead_payload,
        "current_evaluation": {
            "lead": lead.model_dump(),
            "enrichment": enriched.model_dump(),
            "evidence": evidence.model_dump(),
            "decision": decision.model_dump(),
            **_fit_response(decision, evidence, routing, memory_used, history),
        },
        "memory": {
            "enabled": use_memory,
            "company_profile": company_profile,
            "person_profile": person_profile,
            "company_history_count": len(company_history),
            "person_history_count": len(person_history),
        },
        "refresh_enrichment_requested": refresh_enrichment,
    })


@app.post("/api/agent/review-feedback")
async def api_agent_review_feedback(request: Request):
    _require_auth(request)
    _require_db()
    body = await request.json()
    lead_id = (body.get("lead_id") or "").strip()
    campaign_id = (body.get("campaign_id") or "").strip()
    feedback_type = (body.get("feedback_type") or "").strip()
    if not lead_id or not campaign_id or not feedback_type:
        return JSONResponse({"error": "lead_id, campaign_id, and feedback_type are required"}, status_code=400)

    feedback = await store.add_review_feedback(
        _pool,
        run_id=(body.get("run_id") or "").strip() or None,
        lead_id=lead_id,
        campaign_id=campaign_id,
        feedback_type=feedback_type,
        feedback_reason=(body.get("feedback_reason") or "").strip(),
        corrected_decision=(body.get("corrected_decision") or "").strip() or None,
        fit_outcome=(body.get("fit_outcome") or "").strip() or None,
        owner=(body.get("owner") or "").strip(),
    )
    return JSONResponse({"ok": True, "feedback_id": feedback["id"], "feedback": feedback})


@app.get("/api/agent/review-feedback")
async def api_agent_list_review_feedback(
    request: Request,
    campaign_id: str = "",
    feedback_type: str = "",
    limit: int = 50,
    offset: int = 0,
):
    _require_auth(request)
    _require_db()
    rows = await store.list_review_feedback(
        _pool,
        campaign_id=campaign_id or None,
        feedback_type=feedback_type or None,
        limit=limit,
        offset=offset,
    )
    return JSONResponse(rows)


@app.post("/api/agent/campaigns/{campaign_id}/versions")
async def api_agent_create_campaign_version(request: Request, campaign_id: str):
    _require_auth(request)
    _require_db()
    body = await request.json()
    version = (body.get("version") or "").strip()
    if not version:
        return JSONResponse({"error": "version is required"}, status_code=400)

    await store.ensure_campaign(
        _pool,
        campaign_id,
        (body.get("campaign_name") or campaign_id.capitalize()).strip(),
        (body.get("campaign_description") or "").strip(),
    )

    current_files = await store.get_skill_files_for_loader(_pool, campaign_id)
    row = await store.create_campaign_version(
        _pool,
        campaign_id,
        version,
        config_json=(body.get("config_json") or current_files.get("config.json") or "").strip(),
        product_md=body.get("product_md") or current_files.get("product.md") or "",
        icp_md=body.get("icp_md") or current_files.get("icp.md") or "",
        signals_md=body.get("signals_md") or current_files.get("signals.md") or "",
        disqualification_md=body.get("disqualification_md") or current_files.get("disqualification.md") or "",
        qualification_md=body.get("qualification_md") or current_files.get("qualification.md") or "",
        created_by=(body.get("created_by") or "").strip(),
        status=(body.get("status") or "draft").strip() or "draft",
    )
    return JSONResponse(row)


@app.get("/api/agent/campaigns/{campaign_id}/versions")
async def api_agent_list_campaign_versions(request: Request, campaign_id: str):
    _require_auth(request)
    _require_db()
    return JSONResponse(await store.list_campaign_versions(_pool, campaign_id))


@app.post("/api/agent/campaigns/{campaign_id}/versions/{version}/activate")
async def api_agent_activate_campaign_version(request: Request, campaign_id: str, version: str):
    _require_auth(request)
    _require_db()
    body = await request.json()
    approved_by = (body.get("approved_by") or "").strip()
    if not approved_by:
        return JSONResponse({"error": "approved_by is required"}, status_code=400)

    row = await store.activate_campaign_version(_pool, campaign_id, version, approved_by)
    if row is None:
        return JSONResponse({"error": "Campaign version not found"}, status_code=404)
    await _apply_campaign_version_runtime(row)
    return JSONResponse({"ok": True, "campaign_version": row})


@app.post("/api/agent/campaigns/{campaign_id}/policies")
async def api_agent_create_policy_version(request: Request, campaign_id: str):
    _require_auth(request)
    _require_db()
    body = await request.json()
    policy_version = (body.get("policy_version") or "").strip()
    if not policy_version:
        return JSONResponse({"error": "policy_version is required"}, status_code=400)

    current_config = _safe_json_object(await store.get_skill_file(_pool, campaign_id, "config.json") or "{}")
    current_settings = await store.get_settings(_pool, campaign_id=campaign_id)
    qt = int(body.get("qualified_threshold", current_settings.get("qualified_threshold", current_config.get("qualified_threshold", 70))))
    dt = int(body.get("disqualified_threshold", current_settings.get("disqualified_threshold", current_config.get("disqualified_threshold", 60))))
    if dt >= qt:
        return JSONResponse({"error": "disqualified_threshold must be below qualified_threshold"}, status_code=400)

    row = await store.create_policy_version(
        _pool,
        campaign_id,
        policy_version,
        score_weights=body.get("score_weights") or current_config.get("score_weights") or {},
        qualified_threshold=qt,
        disqualified_threshold=dt,
        routing_rules=body.get("routing_rules") or {},
        created_by=(body.get("created_by") or "").strip(),
        status=(body.get("status") or "draft").strip() or "draft",
    )
    return JSONResponse(row)


@app.get("/api/agent/campaigns/{campaign_id}/policies")
async def api_agent_list_policy_versions(request: Request, campaign_id: str):
    _require_auth(request)
    _require_db()
    return JSONResponse(await store.list_policy_versions(_pool, campaign_id))


@app.post("/api/agent/campaigns/{campaign_id}/policies/{policy_version}/activate")
async def api_agent_activate_policy_version(request: Request, campaign_id: str, policy_version: str):
    _require_auth(request)
    _require_db()
    body = await request.json()
    approved_by = (body.get("approved_by") or "").strip()
    if not approved_by:
        return JSONResponse({"error": "approved_by is required"}, status_code=400)

    row = await store.activate_policy_version(_pool, campaign_id, policy_version, approved_by)
    if row is None:
        return JSONResponse({"error": "Policy version not found"}, status_code=404)
    applied_config = await _apply_policy_version_runtime(campaign_id, row)
    return JSONResponse({"ok": True, "policy_version": row, "applied_config": applied_config})


# ── API: campaign registry ────────────────────────────────────────────────────

@app.get("/api/campaigns")
async def api_list_campaigns(request: Request):
    _require_auth(request)
    if _pool:
        return JSONResponse(await store.list_campaigns(_pool))
    campaign_ids = list_campaign_ids()
    return JSONResponse([{"id": cid, "name": cid.capitalize()} for cid in campaign_ids])


# ── API: campaign-scoped runs ─────────────────────────────────────────────────

@app.get("/api/campaigns/{campaign_id}/runs")
async def api_list_campaign_runs(request: Request, campaign_id: str):
    _require_auth(request)
    if _pool:
        return JSONResponse(await store.list_runs(_pool, campaign_id=campaign_id))
    # Fallback: filter by campaign_id stored in _jobs
    if not RUNS_DIR.exists():
        return JSONResponse([])
    dirs = sorted(RUNS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    return JSONResponse([_summarize_run_dir(d) for d in dirs])


@app.get("/api/campaigns/{campaign_id}/runs/{run_id}")
async def api_get_campaign_run(request: Request, campaign_id: str, run_id: str):
    _require_auth(request)
    if _pool:
        leads = await store.get_leads(_pool, run_id)
    elif (RUNS_DIR / run_id).exists():
        leads = _read_run_files(RUNS_DIR / run_id)
    else:
        return JSONResponse([], status_code=404)
    return JSONResponse(_mark_exclusions(_apply_thresholds(leads)))


# ── API: campaign-scoped settings ─────────────────────────────────────────────

@app.get("/api/campaigns/{campaign_id}/settings")
async def api_get_campaign_settings(request: Request, campaign_id: str):
    _require_auth(request)
    if _pool:
        s = await store.get_settings(_pool, campaign_id=campaign_id)
        return JSONResponse(s)
    return JSONResponse(_settings)


@app.post("/api/campaigns/{campaign_id}/settings")
async def api_save_campaign_settings(request: Request, campaign_id: str):
    _require_auth(request)
    body = await request.json()
    qt = int(body.get("qualified_threshold", 70))
    dt = int(body.get("disqualified_threshold", 60))
    if dt >= qt:
        return JSONResponse({"error": "Review threshold must be below qualified threshold"}, status_code=400)
    if _pool:
        await store.save_settings(_pool, {
            "qualified_threshold": str(qt),
            "disqualified_threshold": str(dt),
        }, campaign_id=campaign_id)
    # Update in-memory settings for Seedance (backward compat)
    if campaign_id == "seedance":
        _settings["qualified_threshold"]    = qt
        _settings["disqualified_threshold"] = dt
    return JSONResponse({"ok": True})


# ── API: campaign-scoped exclusions ──────────────────────────────────────────

@app.get("/api/campaigns/{campaign_id}/exclusions")
async def api_get_campaign_exclusions(request: Request, campaign_id: str):
    _require_auth(request)
    if _pool:
        return JSONResponse(await store.get_exclusions(_pool, campaign_id=campaign_id))
    if campaign_id == "seedance":
        return JSONResponse(_exclusions)
    return JSONResponse([])


@app.post("/api/campaigns/{campaign_id}/exclusions")
async def api_add_campaign_exclusion(request: Request, campaign_id: str):
    _require_auth(request)
    body = await request.json()
    value = (body.get("value") or "").strip()
    if not value:
        return JSONResponse({"error": "Value required"}, status_code=400)
    if _pool:
        await store.add_exclusion(_pool, value, campaign_id=campaign_id)
        if campaign_id == "seedance":
            _exclusions[:] = await store.get_exclusions(_pool)
    elif campaign_id == "seedance":
        _exclusions.insert(0, {"id": len(_exclusions) + 1, "value": value})
    return JSONResponse({"ok": True})


@app.delete("/api/campaigns/{campaign_id}/exclusions/{exclusion_id}")
async def api_remove_campaign_exclusion(request: Request, campaign_id: str, exclusion_id: int):
    _require_auth(request)
    if _pool:
        await store.remove_exclusion(_pool, exclusion_id)
        if campaign_id == "seedance":
            _exclusions[:] = await store.get_exclusions(_pool)
    elif campaign_id == "seedance":
        for i, e in enumerate(_exclusions):
            if e["id"] == exclusion_id:
                _exclusions.pop(i)
                break
    return JSONResponse({"ok": True})


@app.post("/api/campaigns/{campaign_id}/exclusions/import")
async def api_import_campaign_exclusions(request: Request, campaign_id: str, file: UploadFile = File(...)):
    _require_auth(request)
    import io as _io
    content = await file.read()
    text = content.decode("utf-8-sig").strip()
    urls = []

    _URL_COLS = {"COMPANY URL", "company_url", "company_website", "Website", "website", "URL", "url"}
    first_line = text.split("\n")[0].strip().strip('"') if text else ""

    if first_line in _URL_COLS or (first_line and not first_line.startswith("http")):
        reader = csv.DictReader(_io.StringIO(text))
        for row in reader:
            url = (
                row.get("COMPANY URL") or row.get("company_url") or
                row.get("company_website") or row.get("Website") or
                row.get("website") or row.get("URL") or row.get("url") or ""
            ).strip()
            if url:
                urls.append(url)
    else:
        for line in text.splitlines():
            url = line.strip().strip('"')
            if url:
                urls.append(url)

    if _pool and urls:
        for url in urls:
            await store.add_exclusion(_pool, url, campaign_id=campaign_id)
        if campaign_id == "seedance":
            _exclusions[:] = await store.get_exclusions(_pool)
    elif campaign_id == "seedance":
        for url in urls:
            if url not in [e.get("value") for e in _exclusions]:
                _exclusions.insert(0, {"id": len(_exclusions) + 1, "value": url})
    else:
        return JSONResponse({"error": "Campaign-scoped exclusion import requires a database"}, status_code=400)
    return JSONResponse({"ok": True, "imported": len(urls)})


# ── API: campaign-scoped email templates ──────────────────────────────────────

@app.get("/api/campaigns/{campaign_id}/email-templates")
async def api_get_campaign_templates(request: Request, campaign_id: str):
    _require_auth(request)
    if _pool:
        return JSONResponse(await store.get_email_templates(_pool, campaign_id=campaign_id))
    if campaign_id == "seedance":
        return JSONResponse(_templates)
    return JSONResponse([])


@app.post("/api/campaigns/{campaign_id}/email-templates")
async def api_add_campaign_template(request: Request, campaign_id: str):
    _require_auth(request)
    data = await request.json()
    vertical = (data.get("vertical") or "").strip()
    name     = (data.get("name") or "").strip()
    subject  = (data.get("subject") or "").strip()
    body     = (data.get("body") or "").strip()
    if not vertical or not name or not subject or not body:
        return JSONResponse({"error": "vertical, name, subject, and body are required"}, status_code=400)
    if _pool:
        t = await store.add_email_template(_pool, vertical, name, subject, body, campaign_id=campaign_id)
        if campaign_id == "seedance":
            _templates.append(t)
        return JSONResponse(t)
    if campaign_id != "seedance":
        return JSONResponse({"error": "Campaign-scoped template creation requires a database"}, status_code=400)
    t = {"id": len(_templates) + 1, "vertical": vertical, "name": name,
         "subject": subject, "body": body, "created_at": None}
    _templates.append(t)
    return JSONResponse(t)


# ── API: runs (flat, backward-compat — defaults to Seedance) ─────────────────

@app.get("/api/runs")
async def list_runs(request: Request):
    _require_auth(request)
    if _pool:
        return JSONResponse(await store.list_runs(_pool))
    # Fallback: list directories
    if not RUNS_DIR.exists():
        return JSONResponse([])
    dirs = sorted(RUNS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    return JSONResponse([_summarize_run_dir(d) for d in dirs])


@app.delete("/api/runs/{run_id}")
async def delete_run(request: Request, run_id: str):
    _require_auth(request)
    if _pool:
        await store.delete_run(_pool, run_id)
    run_dir = RUNS_DIR / run_id
    if run_dir.exists():
        shutil.rmtree(run_dir, ignore_errors=True)
    _jobs.pop(run_id, None)
    return JSONResponse({"ok": True})


@app.get("/api/runs/{run_id}/email-preview")
async def email_preview(request: Request, run_id: str):
    _require_auth(request)
    if _pool:
        leads = await store.get_leads(_pool, run_id)
    else:
        leads = _read_run_files(RUNS_DIR / run_id)
    previews = [
        {
            **_build_email(l),
            "lead_id":    l["lead_id"],
            "name":       l["name"],
            "company":    l["company"],
            "score":      l["score"],
            "decision":   l["decision"],
            "is_manual":  l["score"] < INSTANTLY_SCORE_THRESHOLD,
            "email_sent": l.get("email_sent", False),
        }
        for l in leads
    ]
    return JSONResponse(previews)


async def _fetch_instantly_by_email(campaign_id: str = "") -> dict:
    """Fetch all leads in one Instantly campaign, keyed by lowercased email.
    Returns {email: {"sent_at": str|None, "instantly_status": int}}. Empty if not configured."""
    campaign_id = (campaign_id or INSTANTLY_CAMPAIGN_ID or "").strip()
    if not INSTANTLY_API_KEY or not campaign_id:
        return {}
    ssl_verify = os.getenv("LLM_SSL_VERIFY", "true").lower() not in ("false", "0", "no")
    by_email: dict = {}
    async with httpx.AsyncClient(
        timeout=30, verify=ssl_verify,
        headers={"Authorization": f"Bearer {INSTANTLY_API_KEY}"},
    ) as client:
        starting_after = None
        for _ in range(40):  # paginate, cap at 40 pages (2000 leads)
            body = {"campaign_id": campaign_id, "limit": 50}
            if starting_after:
                body["starting_after"] = starting_after
            resp = await client.post(
                "https://api.instantly.ai/api/v2/leads/list", json=body)
            if not resp.is_success:
                break
            data = resp.json()
            items = data.get("items", [])
            for il in items:
                email = (il.get("email") or "").strip().lower()
                if not email:
                    continue
                last_step = (il.get("status_summary") or {}).get("lastStep") or {}
                sent_at = last_step.get("timestamp_executed")
                # Keep the record showing a send if any duplicate has one
                existing = by_email.get(email)
                if existing is None or (sent_at and not existing.get("sent_at")):
                    by_email[email] = {
                        "sent_at": sent_at,
                        "instantly_status": il.get("status"),
                    }
            starting_after = data.get("next_starting_after")
            if not starting_after or not items:
                break
    return by_email


def _lead_delivery_status(lead: dict, by_email: dict) -> dict:
    """Resolve a lead's Instantly status: pushed?, delivery state, sent_at timestamp.

    Live Instantly data (`info`, matched by email) is authoritative for delivery.
    The DB `email_sent` flag only tells us we attempted a push.
    """
    email = (lead.get("email") or "").strip().lower()
    info = by_email.get(email) if email else None
    db_pushed = bool(lead.get("email_sent"))

    if info and info.get("sent_at"):
        # Instantly confirms an actual send — most authoritative
        return {"pushed": True, "delivery": "sent", "sent_at": info["sent_at"]}
    if info:
        # In the campaign but no send recorded yet
        return {"pushed": True, "delivery": "queued", "sent_at": None}
    if db_pushed:
        # We pushed it but it's not currently in the campaign (removed/deduped/esp999).
        # Delivery cannot be confirmed from Instantly — report pushed, delivery unknown.
        return {"pushed": True, "delivery": "unknown", "sent_at": None}
    return {"pushed": False, "delivery": "not_pushed", "sent_at": None}


async def _resolve_lead_statuses(leads: list) -> dict:
    """Resolve Instantly delivery status for a set of leads that may span multiple
    campaigns. Each lead is checked against ITS OWN campaign's live data.
    Returns {lead_id: {pushed, delivery, sent_at, campaign_id}}."""
    routing_map = await store.get_routing_map(_pool) if _pool else {}
    lead_campaign = {l["lead_id"]: _resolve_instantly_campaign(l, routing_map) for l in leads}

    # Fetch each distinct campaign's email map exactly once
    maps: dict = {}
    for cid in {c for c in lead_campaign.values() if c}:
        try:
            maps[cid] = await _fetch_instantly_by_email(cid)
        except Exception as exc:
            logger.warning("Instantly status fetch failed for campaign %s: %s", cid, exc)
            maps[cid] = {}

    statuses: dict = {}
    for l in leads:
        cid = lead_campaign[l["lead_id"]]
        st = _lead_delivery_status(l, maps.get(cid, {}))
        st["campaign_id"] = cid
        statuses[l["lead_id"]] = st
    return statuses


@app.get("/api/runs/{run_id}/instantly-status")
async def instantly_status(request: Request, run_id: str):
    """Return actual Instantly delivery status per lead (sent / queued / not_pushed)."""
    _require_auth(request)
    if _pool:
        leads = await store.get_leads(_pool, run_id)
    else:
        leads = _read_run_files(RUNS_DIR / run_id)

    if not INSTANTLY_API_KEY:
        return JSONResponse({"configured": False, "statuses": {}})

    try:
        statuses = await _resolve_lead_statuses(leads)
    except Exception as exc:
        logger.warning("Instantly status fetch failed: %s", exc)
        return JSONResponse({"configured": True, "error": str(exc), "statuses": {}})

    return JSONResponse({"configured": True, "statuses": statuses})


@app.get("/api/runs/{run_id}/export")
async def export_run(request: Request, run_id: str):
    _require_auth(request)
    if _pool:
        leads = await store.get_leads(_pool, run_id)
    elif (RUNS_DIR / run_id).exists():
        leads = _read_run_files(RUNS_DIR / run_id)
    else:
        return JSONResponse({"error": "Run not found"}, status_code=404)
    leads = [l for l in _apply_thresholds(leads) if not _is_excluded(l)]

    # Pull live per-campaign Instantly status so the export reflects current state
    statuses: dict = {}
    try:
        statuses = await _resolve_lead_statuses(leads)
    except Exception as exc:
        logger.warning("Export: Instantly status fetch failed: %s", exc)

    import io
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "name", "company", "company_url", "title", "linkedin_url",
        "decision", "score", "email", "email_subject", "email_body",
        "instantly_campaign", "pushed_to_instantly", "email_sent", "email_sent_at",
    ])
    for l in leads:
        email = _build_email(l)
        status = statuses.get(l["lead_id"], {"pushed": False, "delivery": "not_pushed",
                                             "sent_at": None, "campaign_id": ""})
        writer.writerow([
            l["name"], l["company"], l.get("company_website", ""),
            l.get("role") or l.get("title", ""), l.get("linkedin_url", ""),
            l["decision"], l["score"], l.get("email", ""),
            email["subject"], email["body"],
            status.get("campaign_id", ""),
            "yes" if status["pushed"] else "no",
            "yes" if status["delivery"] == "sent" else "no",
            status["sent_at"] or "",
        ])

    filename = f"leads-{run_id[:8]}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/runs/{run_id}/send-emails")
async def send_emails(request: Request, run_id: str):
    _require_auth(request)
    if not INSTANTLY_API_KEY:
        return JSONResponse({"error": "Instantly not configured"}, status_code=400)
    if not _pool:
        return JSONResponse({"error": "Database required for email sending"}, status_code=400)

    eligible = await store.get_leads_for_email(_pool, run_id, INSTANTLY_SCORE_THRESHOLD)
    eligible = [l for l in eligible if not _is_excluded(l)]
    # Only send leads that have a real email address
    eligible = [l for l in eligible if (l.get("email") or "").strip()]
    if not eligible:
        return JSONResponse({"sent": 0, "message": "No eligible leads with email addresses to send"})

    # Group eligible leads by their resolved target Instantly campaign
    routing_map = await store.get_routing_map(_pool)
    groups: dict[str, list] = {}
    no_campaign = 0
    for l in eligible:
        cid = _resolve_instantly_campaign(l, routing_map)
        if not cid:
            no_campaign += 1
            continue
        groups.setdefault(cid, []).append(l)

    if not groups:
        return JSONResponse({"error": "No Instantly campaign configured for these leads "
                                      "(set a default INSTANTLY_CAMPAIGN_ID or a routing map)"},
                            status_code=400)

    all_sent_ids: list = []
    by_campaign: dict = {}
    try:
        for cid, group in groups.items():
            sent_ids = await _send_to_instantly(group, cid)
            all_sent_ids.extend(sent_ids)
            by_campaign[cid] = len(sent_ids)
    except Exception as exc:
        return JSONResponse({"error": f"Instantly API error: {exc}"}, status_code=502)

    if all_sent_ids:
        await store.mark_leads_email_sent(_pool, run_id, all_sent_ids)

    all_leads = await store.get_leads(_pool, run_id)
    unsent = [l for l in all_leads
              if not l.get("email_sent")
              and l["decision"] in ("qualified", "review")
              and l["score"] >= INSTANTLY_SCORE_THRESHOLD]
    status = "partial" if unsent else "sent"
    await store.update_run_email_status(_pool, run_id, status)

    return JSONResponse({"sent": len(all_sent_ids), "email_status": status,
                         "by_campaign": by_campaign})


# ── API: Instantly campaigns & routing ───────────────────────────────────────

# Canonical verticals leads classify into (matches the email-template selector)
_ROUTING_VERTICALS = [
    {"key": "ai_tools",        "label": "AI Tools"},
    {"key": "film_production", "label": "Film / Production"},
    {"key": "vfx_production",  "label": "VFX Production"},
    {"key": "animation",       "label": "Animation"},
    {"key": "short_drama",     "label": "Short Drama"},
    {"key": "ecommerce",       "label": "E-commerce / Ads"},
    {"key": "ai_generic",      "label": "Everything else (default)"},
]


async def _validate_instantly_campaign_status(campaign_id: str) -> dict:
    """Check a campaign ID against Instantly. Returns {exists, status, name}."""
    if not INSTANTLY_API_KEY or not campaign_id:
        return {"exists": False, "status": None, "name": ""}
    ssl_verify = os.getenv("LLM_SSL_VERIFY", "true").lower() not in ("false", "0", "no")
    try:
        async with httpx.AsyncClient(
            timeout=15, verify=ssl_verify,
            headers={"Authorization": f"Bearer {INSTANTLY_API_KEY}"},
        ) as client:
            r = await client.get(f"https://api.instantly.ai/api/v2/campaigns/{campaign_id}")
            if r.status_code == 200:
                d = r.json()
                return {"exists": True, "status": d.get("status"), "name": d.get("name", "")}
            return {"exists": False, "status": None, "name": ""}
    except Exception as exc:
        logger.warning("Campaign validation failed for %s: %s", campaign_id, exc)
        return {"exists": False, "status": None, "name": "", "error": str(exc)}


@app.get("/api/instantly/campaigns")
async def api_list_instantly_campaigns(request: Request):
    _require_auth(request)
    if not _pool:
        return JSONResponse([])
    rows = await store.list_instantly_campaigns(_pool)
    # Annotate each with live Instantly status (active/paused/etc.)
    for row in rows:
        info = await _validate_instantly_campaign_status(row["campaign_id"])
        row["live_status"] = info.get("status")
        row["exists"] = info.get("exists", False)
    return JSONResponse(rows)


@app.post("/api/instantly/campaigns")
async def api_add_instantly_campaign(request: Request):
    _require_auth(request)
    if not _pool:
        return JSONResponse({"error": "Database required"}, status_code=400)
    body = await request.json()
    label = (body.get("label") or "").strip()
    campaign_id = (body.get("campaign_id") or "").strip()
    if not label or not campaign_id:
        return JSONResponse({"error": "label and campaign_id are required"}, status_code=400)
    info = await _validate_instantly_campaign_status(campaign_id)
    if not info.get("exists"):
        return JSONResponse({"error": "Campaign ID not found in Instantly — check the ID"},
                            status_code=400)
    row = await store.add_instantly_campaign(_pool, label, campaign_id)
    row["live_status"] = info.get("status")
    row["exists"] = True
    return JSONResponse(row)


@app.delete("/api/instantly/campaigns/{row_id}")
async def api_remove_instantly_campaign(request: Request, row_id: int):
    _require_auth(request)
    if _pool:
        await store.remove_instantly_campaign(_pool, row_id)
    return JSONResponse({"ok": True})


@app.get("/api/instantly/routing")
async def api_get_routing(request: Request):
    _require_auth(request)
    rules = await store.get_routing_map(_pool) if _pool else {}
    return JSONResponse({
        "verticals": _ROUTING_VERTICALS,
        "rules": rules,
        "default_campaign_id": INSTANTLY_CAMPAIGN_ID,
    })


@app.post("/api/instantly/routing")
async def api_set_routing(request: Request):
    _require_auth(request)
    if not _pool:
        return JSONResponse({"error": "Database required"}, status_code=400)
    body = await request.json()
    vertical = (body.get("vertical") or "").strip()
    campaign_id = (body.get("campaign_id") or "").strip()
    valid_keys = {v["key"] for v in _ROUTING_VERTICALS}
    if vertical not in valid_keys:
        return JSONResponse({"error": f"Unknown vertical '{vertical}'"}, status_code=400)
    await store.set_routing(_pool, vertical, campaign_id)
    return JSONResponse({"ok": True, "vertical": vertical, "campaign_id": campaign_id})


# ── API: settings ─────────────────────────────────────────────────────────────

@app.get("/api/settings")
async def api_get_settings(request: Request):
    _require_auth(request)
    return JSONResponse(_settings)


@app.post("/api/settings")
async def api_save_settings(request: Request):
    _require_auth(request)
    body = await request.json()
    qt = int(body.get("qualified_threshold", 70))
    dt = int(body.get("disqualified_threshold", 60))
    if dt >= qt:
        return JSONResponse({"error": "Review threshold must be below qualified threshold"}, status_code=400)
    apollo = bool(body.get("apollo_enabled", _settings.get("apollo_enabled", False)))
    if _pool:
        await store.save_settings(_pool, {
            "qualified_threshold": str(qt),
            "disqualified_threshold": str(dt),
            "apollo_enabled": "true" if apollo else "false",
        })
    _settings["qualified_threshold"]    = qt
    _settings["disqualified_threshold"] = dt
    _settings["apollo_enabled"]         = apollo
    os.environ["APOLLO_ENABLED"] = "true" if apollo else "false"
    return JSONResponse({"ok": True})


# ── API: exclusions ───────────────────────────────────────────────────────────

@app.get("/api/exclusions")
async def api_get_exclusions(request: Request):
    _require_auth(request)
    return JSONResponse(_exclusions)


@app.post("/api/exclusions")
async def api_add_exclusion(request: Request):
    _require_auth(request)
    body = await request.json()
    value = (body.get("value") or "").strip()
    if not value:
        return JSONResponse({"error": "Value required"}, status_code=400)
    if _pool:
        await store.add_exclusion(_pool, value)
        _exclusions[:] = await store.get_exclusions(_pool)
    else:
        _exclusions.insert(0, {"id": len(_exclusions) + 1, "value": value})
    return JSONResponse({"ok": True})


@app.delete("/api/exclusions/{exclusion_id}")
async def api_remove_exclusion(request: Request, exclusion_id: int):
    _require_auth(request)
    if _pool:
        await store.remove_exclusion(_pool, exclusion_id)
    for i, e in enumerate(_exclusions):
        if e["id"] == exclusion_id:
            _exclusions.pop(i)
            break
    return JSONResponse({"ok": True})


@app.post("/api/exclusions/import")
async def api_import_exclusions(request: Request, file: UploadFile = File(...)):
    _require_auth(request)
    import io as _io
    content = await file.read()
    text = content.decode("utf-8-sig").strip()
    urls = []

    _URL_COLS = {"COMPANY URL", "company_url", "company_website", "Website", "website", "URL", "url"}
    first_line = text.split("\n")[0].strip().strip('"')

    if first_line in _URL_COLS or not first_line.startswith("http"):
        # Trigify-style CSV with a header row
        reader = csv.DictReader(_io.StringIO(text))
        for row in reader:
            url = (
                row.get("COMPANY URL") or row.get("company_url") or
                row.get("company_website") or row.get("Website") or
                row.get("website") or row.get("URL") or row.get("url") or ""
            ).strip()
            if url:
                urls.append(url)
    else:
        # Plain list of URLs, one per line (no header)
        for line in text.splitlines():
            url = line.strip().strip('"')
            if url:
                urls.append(url)

    if _pool and urls:
        for url in urls:
            await store.add_exclusion(_pool, url)
        _exclusions[:] = await store.get_exclusions(_pool)
    return JSONResponse({"ok": True, "imported": len(urls)})


# ── API: per-lead manual email address ────────────────────────────────────────

@app.patch("/api/runs/{run_id}/leads/{lead_id}/email")
async def api_set_lead_email(request: Request, run_id: str, lead_id: str):
    _require_auth(request)
    data = await request.json()
    email = (data.get("email") or "").strip()
    if not email or "@" not in email or "." not in email.split("@")[-1]:
        return JSONResponse({"error": "Valid email required"}, status_code=400)
    if _pool:
        await _pool.execute(
            "UPDATE leads SET email=$1 WHERE run_id=$2 AND lead_id=$3",
            email, run_id, lead_id,
        )
        # Persist to person_profiles so the email survives future re-uploads
        lead_row = await _pool.fetchrow(
            "SELECT person_name, linkedin_url FROM leads WHERE run_id=$1 AND lead_id=$2",
            run_id, lead_id,
        )
        if lead_row and (lead_row["person_name"] or lead_row["linkedin_url"]):
            try:
                await store.upsert_person_profile(
                    _pool,
                    lead_row["person_name"] or lead_row["linkedin_url"] or "",
                    linkedin_url=lead_row["linkedin_url"] or "",
                    known_email=email,
                )
            except Exception as exc:
                logger.warning("Could not persist known_email to person profile: %s", exc)
    return JSONResponse({"ok": True, "email": email})


@app.patch("/api/runs/{run_id}/leads/{lead_id}/campaign")
async def api_set_lead_campaign(request: Request, run_id: str, lead_id: str):
    """Override which Instantly campaign a single lead is routed to."""
    _require_auth(request)
    data = await request.json()
    campaign_id = (data.get("campaign_id") or "").strip()
    if _pool:
        await store.set_lead_instantly_campaign(_pool, run_id, lead_id, campaign_id)
    return JSONResponse({"ok": True, "campaign_id": campaign_id})


# ── API: per-lead manual email status ─────────────────────────────────────────

@app.post("/api/runs/{run_id}/leads/{lead_id}/email-status")
async def api_set_lead_email_status(request: Request, run_id: str, lead_id: str):
    _require_auth(request)
    data   = await request.json()
    status = data.get("status", "")
    if status not in ("sent", "not_sent"):
        return JSONResponse({"error": "status must be 'sent' or 'not_sent'"}, status_code=400)
    if _pool:
        await store.set_lead_manual_email_status(_pool, run_id, lead_id, status)
    return JSONResponse({"ok": True, "lead_id": lead_id, "status": status})


# ── API: email templates ───────────────────────────────────────────────────────

@app.get("/api/email-templates")
async def api_get_templates(request: Request):
    _require_auth(request)
    return JSONResponse(_templates)


@app.post("/api/email-templates")
async def api_add_template(request: Request):
    _require_auth(request)
    data = await request.json()
    vertical = (data.get("vertical") or "").strip()
    name     = (data.get("name") or "").strip()
    subject  = (data.get("subject") or "").strip()
    body     = (data.get("body") or "").strip()
    if not vertical or not name or not subject or not body:
        return JSONResponse({"error": "vertical, name, subject, and body are required"}, status_code=400)
    if _pool:
        t = await store.add_email_template(_pool, vertical, name, subject, body)
        _templates.append(t)
        return JSONResponse(t)
    # No-DB fallback: add to in-memory cache only
    t = {"id": len(_templates) + 1, "vertical": vertical, "name": name,
         "subject": subject, "body": body, "created_at": None}
    _templates.append(t)
    return JSONResponse(t)


@app.put("/api/email-templates/{template_id}")
async def api_update_template(request: Request, template_id: int):
    _require_auth(request)
    data = await request.json()
    vertical = (data.get("vertical") or "").strip()
    name     = (data.get("name") or "").strip()
    subject  = (data.get("subject") or "").strip()
    body     = (data.get("body") or "").strip()
    if not vertical or not name or not subject or not body:
        return JSONResponse({"error": "vertical, name, subject, and body are required"}, status_code=400)
    if _pool:
        updated = await store.update_email_template(_pool, template_id, vertical, name, subject, body)
        if not updated:
            return JSONResponse({"error": "not found"}, status_code=404)
        for i, t in enumerate(_templates):
            if t["id"] == template_id:
                _templates[i] = updated
                break
        return JSONResponse(updated)
    # No-DB fallback
    for i, t in enumerate(_templates):
        if t["id"] == template_id:
            _templates[i] = {**t, "vertical": vertical, "name": name, "subject": subject, "body": body}
            return JSONResponse(_templates[i])
    return JSONResponse({"error": "not found"}, status_code=404)


@app.delete("/api/email-templates/{template_id}")
async def api_delete_template(request: Request, template_id: int):
    _require_auth(request)
    if _pool:
        await store.delete_email_template(_pool, template_id)
    for i, t in enumerate(_templates):
        if t["id"] == template_id:
            _templates.pop(i)
            break
    return JSONResponse({"ok": True})


# ── API: skills management ────────────────────────────────────────────────────

# Files that make up a campaign (order matters for the UI file list)
_CAMPAIGN_FILES = ["config.json", "product.md", "icp.md", "signals.md", "disqualification.md", "qualification.md"]
_COMMON_FILES   = ["signal-taxonomy.md", "scoring-framework.md", "evidence-extraction.md"]
_EDITABLE_EXTS  = {".md", ".json"}


def _skill_campaigns() -> list[dict]:
    """Return all editable skill groups: one entry per campaign + common."""
    groups = []
    # common/ is always listed first
    common_dir = SKILLS_ROOT / "common"
    if common_dir.exists():
        groups.append({"id": "common", "name": "Common (shared)", "is_common": True})
    for cid in sorted(list_campaign_ids()):
        groups.append({"id": cid, "name": cid.capitalize(), "is_common": False})
    return groups


@app.get("/api/skills/campaigns")
async def api_skills_campaigns(request: Request):
    _require_auth(request)
    if _pool:
        # Use skill_files for common (always disk-seeded) and campaigns table for
        # campaign tabs — so UI-created campaigns (no disk dir) always appear.
        skill_ids = set(await store.list_skill_campaign_ids(_pool))
        db_campaigns = await store.list_campaigns(_pool)
        groups = []
        if "common" in skill_ids:
            groups.append({"id": "common", "name": "Common (shared)", "is_common": True})
        for c in sorted(db_campaigns, key=lambda x: x["id"]):
            groups.append({"id": c["id"], "name": c["name"], "is_common": False})
        return JSONResponse(groups)
    return JSONResponse(_skill_campaigns())


@app.get("/api/skills/{campaign_id}")
async def api_skills_list(request: Request, campaign_id: str):
    """List editable files for a campaign or 'common'."""
    _require_auth(request)
    if _pool:
        files = await store.list_skill_files(_pool, campaign_id)
        if not files:
            return JSONResponse({"error": "Campaign not found or has no files"}, status_code=404)
        return JSONResponse(files)
    # Disk fallback
    target_dir = SKILLS_ROOT / campaign_id
    if not target_dir.exists():
        return JSONResponse({"error": "Campaign not found"}, status_code=404)
    files = [
        {"name": f.name, "size": f.stat().st_size}
        for f in sorted(target_dir.iterdir())
        if f.is_file() and f.suffix in _EDITABLE_EXTS and not f.name.startswith(".")
    ]
    return JSONResponse(files)


@app.get("/api/skills/{campaign_id}/export")
async def api_skills_export(request: Request, campaign_id: str):
    """Download all skill files for a campaign as a zip archive."""
    _require_auth(request)

    if _pool:
        raw = await store.list_skill_files(_pool, campaign_id)
        if not raw:
            return JSONResponse({"error": "Campaign not found or has no files"}, status_code=404)
        file_contents = {}
        for row in raw:
            content = await store.get_skill_file(_pool, campaign_id, row["name"])
            if content is not None:
                file_contents[row["name"]] = content
    else:
        target_dir = SKILLS_ROOT / campaign_id
        if not target_dir.exists():
            return JSONResponse({"error": "Campaign not found"}, status_code=404)
        file_contents = {
            f.name: f.read_text(encoding="utf-8")
            for f in sorted(target_dir.iterdir())
            if f.is_file() and f.suffix in _EDITABLE_EXTS and not f.name.startswith(".")
        }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in file_contents.items():
            zf.writestr(name, content)
    buf.seek(0)

    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{campaign_id}-skills.zip"'},
    )


@app.get("/api/skills/{campaign_id}/{filename}")
async def api_skills_read(request: Request, campaign_id: str, filename: str):
    """Read the content of a single skill file."""
    _require_auth(request)
    if ".." in filename or "/" in filename:
        return JSONResponse({"error": "Invalid filename"}, status_code=400)
    if Path(filename).suffix not in _EDITABLE_EXTS:
        return JSONResponse({"error": "File type not editable"}, status_code=400)

    if _pool:
        content = await store.get_skill_file(_pool, campaign_id, filename)
        if content is None:
            return JSONResponse({"error": "File not found"}, status_code=404)
        return JSONResponse({"content": content})
    # Disk fallback
    file_path = SKILLS_ROOT / campaign_id / filename
    if not file_path.exists():
        return JSONResponse({"error": "File not found"}, status_code=404)
    return JSONResponse({"content": file_path.read_text(encoding="utf-8")})


@app.put("/api/skills/{campaign_id}/{filename}")
async def api_skills_write(request: Request, campaign_id: str, filename: str):
    """Save edited content to a skill file and hot-reload the affected campaign(s)."""
    # #region debug-point A:skills-write-entry
    try:
        import json, urllib.request; urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:7777/event", data=json.dumps({"sessionId":"skill-upload-403","runId":"pre-fix","hypothesisId":"A","location":"ui/server.py:api_skills_write","msg":"[DEBUG] skills write request received","data":{"campaign_id":campaign_id,"filename":filename,"method":request.method,"has_cookie":bool(request.headers.get("cookie")),"origin":request.headers.get("origin"),"referer":request.headers.get("referer"),"content_type":request.headers.get("content-type")}}).encode(), headers={"Content-Type":"application/json"}), timeout=0.4).read()
    except Exception:
        pass
    # #endregion
    _require_auth(request)
    if ".." in filename or "/" in filename:
        return JSONResponse({"error": "Invalid filename"}, status_code=400)
    if Path(filename).suffix not in _EDITABLE_EXTS:
        return JSONResponse({"error": "File type not editable"}, status_code=400)

    body = await request.json()
    content = body.get("content", "")

    if _pool:
        # Verify the row exists before saving (prevent creating arbitrary new files)
        existing = await store.get_skill_file(_pool, campaign_id, filename)
        if existing is None:
            return JSONResponse({"error": "File not found"}, status_code=404)
        await store.upsert_skill_file(_pool, campaign_id, filename, content)
        logger.info("Skill file saved to DB: %s/%s (%d bytes)", campaign_id, filename, len(content))
        reloaded = await _reload_campaign_from_db(campaign_id)
        return JSONResponse({"ok": True, "reloaded": reloaded})

    # Disk fallback
    from campaigns.registry import reload_campaign
    file_path = SKILLS_ROOT / campaign_id / filename
    if not file_path.exists():
        return JSONResponse({"error": "File not found"}, status_code=404)
    file_path.write_text(content, encoding="utf-8")
    logger.info("Skill file saved to disk: %s/%s (%d bytes)", campaign_id, filename, len(content))
    reloaded = []
    targets = list_campaign_ids() if campaign_id == "common" else [campaign_id]
    for cid in targets:
        try:
            reload_campaign(cid, SKILLS_ROOT)
            reloaded.append(cid)
        except Exception as exc:
            logger.warning("Hot-reload failed for '%s': %s", cid, exc)
    return JSONResponse({"ok": True, "reloaded": reloaded})


@app.post("/api/skills")
async def api_skills_create(
    request: Request,
    file: UploadFile | None = File(None),
    files: list[UploadFile] | None = File(None),
    name: str = Form(...),
):
    """
    Create a new campaign from a zip upload.
    All 6 files are required: config.json, product.md, icp.md,
    signals.md, disqualification.md, qualification.md.
    campaign_id is derived from name: lowercased, spaces → hyphens.
    """
    # #region debug-point D:skills-create-entry
    try:
        import json, urllib.request; urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:7777/event", data=json.dumps({"sessionId":"skill-upload-403","runId":"pre-fix","hypothesisId":"D","location":"ui/server.py:api_skills_create","msg":"[DEBUG] skills create request received","data":{"name":name,"filename":file.filename if file else None,"filenames":[f.filename for f in (files or [])],"method":request.method,"has_cookie":bool(request.headers.get("cookie")),"origin":request.headers.get("origin"),"referer":request.headers.get("referer"),"content_type":request.headers.get("content-type")}}).encode(), headers={"Content-Type":"application/json"}), timeout=0.4).read()
    except Exception:
        pass
    # #endregion
    _require_auth(request)
    import zipfile
    import io as _io

    campaign_id = name.strip().lower().replace(" ", "-")
    if not campaign_id or ".." in campaign_id or "/" in campaign_id:
        return JSONResponse({"error": "Invalid campaign name"}, status_code=400)

    # Conflict check: DB or disk
    if _pool:
        existing = await store.list_skill_files(_pool, campaign_id)
        if existing:
            return JSONResponse({"error": f"Campaign '{campaign_id}' already exists"}, status_code=409)
    elif (SKILLS_ROOT / campaign_id).exists():
        return JSONResponse({"error": f"Campaign '{campaign_id}' already exists"}, status_code=409)

    required = {"config.json", "product.md", "icp.md", "signals.md", "disqualification.md", "qualification.md"}
    files_content: dict[str, str] = {}

    if file is not None:
        zip_bytes = await file.read()
        try:
            zf = zipfile.ZipFile(_io.BytesIO(zip_bytes))
        except zipfile.BadZipFile:
            return JSONResponse({"error": "Uploaded file is not a valid zip"}, status_code=400)

        entries = [n for n in zf.namelist() if not n.endswith("/") and not n.startswith("__MACOSX")]
        if not entries:
            return JSONResponse({"error": "Zip is empty"}, status_code=400)

        parts = [e.split("/") for e in entries]
        prefix = parts[0][0] if all(p[0] == parts[0][0] for p in parts) and len(parts[0]) > 1 else ""

        def _strip(n: str) -> str:
            return n[len(prefix) + 1:] if prefix and n.startswith(prefix + "/") else n

        files_in_zip = {_strip(e): e for e in entries}
        missing = required - files_in_zip.keys()
        if missing:
            return JSONResponse({"error": f"Zip missing required files: {', '.join(sorted(missing))}"}, status_code=400)

        for local_name, zip_entry in files_in_zip.items():
            if Path(local_name).suffix in _EDITABLE_EXTS:
                try:
                    files_content[local_name] = zf.read(zip_entry).decode("utf-8")
                except UnicodeDecodeError:
                    return JSONResponse({"error": f"File '{local_name}' must be UTF-8 text"}, status_code=400)
    else:
        upload_map = {
            Path(f.filename or "").name: f
            for f in (files or [])
            if (f.filename or "").strip()
        }
        if not upload_map:
            return JSONResponse({"error": "Upload either a zip or the 6 required skill files"}, status_code=400)
        missing = required - upload_map.keys()
        if missing:
            return JSONResponse({"error": f"Missing required files: {', '.join(sorted(missing))}"}, status_code=400)
        for local_name in required:
            try:
                files_content[local_name] = (await upload_map[local_name].read()).decode("utf-8")
            except UnicodeDecodeError:
                return JSONResponse({"error": f"File '{local_name}' must be UTF-8 text"}, status_code=400)

    written = list(files_content.keys())

    if _pool:
        # DB path: persist campaign row + all skill files, reload from DB
        await store.ensure_campaign(_pool, campaign_id, name.strip())
        for fname, fcontent in files_content.items():
            await store.upsert_skill_file(_pool, campaign_id, fname, fcontent)
        files_for_loader = await store.get_skill_files_for_loader(_pool, campaign_id)
        try:
            reload_campaign_from_strings(campaign_id, files_for_loader, SKILLS_ROOT)
        except Exception as exc:
            logger.warning("Campaign '%s' saved to DB but failed to load: %s", campaign_id, exc)
            return JSONResponse({"ok": True, "campaign_id": campaign_id, "files": written, "load_warning": str(exc)})
    else:
        # Disk fallback
        from campaigns.registry import reload_campaign
        campaign_dir = SKILLS_ROOT / campaign_id
        campaign_dir.mkdir(parents=True)
        for fname, fcontent in files_content.items():
            (campaign_dir / fname).write_text(fcontent, encoding="utf-8")
        try:
            reload_campaign(campaign_id, SKILLS_ROOT)
        except Exception as exc:
            logger.warning("Campaign '%s' created on disk but failed to load: %s", campaign_id, exc)
            return JSONResponse({"ok": True, "campaign_id": campaign_id, "files": written, "load_warning": str(exc)})

    logger.info("New campaign created: %s (files: %s)", campaign_id, ", ".join(written))
    return JSONResponse({"ok": True, "campaign_id": campaign_id, "files": written})


@app.delete("/api/skills/{campaign_id}")
async def api_skills_delete(request: Request, campaign_id: str):
    """Delete a campaign and remove it from the registry. seedance and common are protected."""
    _require_auth(request)
    if campaign_id in ("common", "seedance", "kling"):
        return JSONResponse({"error": f"Campaign '{campaign_id}' cannot be deleted"}, status_code=400)

    from campaigns.registry import remove_campaign

    if _pool:
        existing = await store.list_skill_files(_pool, campaign_id)
        if not existing:
            return JSONResponse({"error": "Campaign not found"}, status_code=404)
        await store.delete_campaign(_pool, campaign_id)
    else:
        campaign_dir = SKILLS_ROOT / campaign_id
        if not campaign_dir.exists():
            return JSONResponse({"error": "Campaign not found"}, status_code=404)
        shutil.rmtree(campaign_dir)

    remove_campaign(campaign_id)
    logger.info("Campaign deleted: %s", campaign_id)
    return JSONResponse({"ok": True})


# ── HTML ──────────────────────────────────────────────────────────────────────

# ── HTML templates ────────────────────────────────────────────────────────────

TEMPLATES_DIR = Path(__file__).parent / "templates"
_LOGIN_HTML = (TEMPLATES_DIR / "login.html").read_text(encoding="utf-8")
HTML = (TEMPLATES_DIR / "app.html").read_text(encoding="utf-8")






@app.get("/")
async def index(request: Request):
    if not _is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse(HTML)


@app.get("/{full_path:path}")
async def spa_fallback(request: Request, full_path: str):
    """Serve the app shell for all client-side routes so refresh works."""
    if not _is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse(HTML)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    uvicorn.run(app, host="0.0.0.0", port=8080, reload=False)
