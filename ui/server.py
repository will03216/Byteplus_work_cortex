"""
Seedance Lead Qualification Engine — Web UI
Run locally:  python ui/server.py
In Docker:    set DATABASE_URL env var, then docker-compose up
"""

import asyncio
import csv
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

import secrets

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

logger = logging.getLogger("seedance.ui")

# ── In-memory job state (SSE streaming only) ──────────────────────────────────
# Keyed by run_id. Cleared after results are imported to DB.
_jobs: dict = {}

# ── DB pool (set in lifespan) ─────────────────────────────────────────────────
_pool = None

# ── Settings, exclusions, and template cache (loaded from DB at startup) ───────
_settings: dict = {"qualified_threshold": 70, "disqualified_threshold": 60}
_exclusions: list = []
_templates: list = []  # all email_templates rows, refreshed on add/delete

DATABASE_URL              = os.getenv("DATABASE_URL", "")
APP_USERNAME              = os.getenv("APP_USERNAME", "admin")
APP_PASSWORD              = os.getenv("APP_PASSWORD", "changeme")
SESSION_SECRET            = os.getenv("SESSION_SECRET", secrets.token_hex(32))
INSTANTLY_API_KEY         = os.getenv("INSTANTLY_API_KEY", "")
INSTANTLY_CAMPAIGN_ID     = os.getenv("INSTANTLY_CAMPAIGN_ID", "")
INSTANTLY_SCORE_THRESHOLD = int(os.getenv("INSTANTLY_SCORE_THRESHOLD", "75"))
PLACEHOLDER_EMAIL         = os.getenv("PLACEHOLDER_EMAIL", "mittulmadaan91@gmail.com")


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pool
    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    if DATABASE_URL:
        try:
            _pool = await store.create_pool(DATABASE_URL)
            await store.create_tables(_pool)
            await store.mark_stale_runs(_pool)
            s = await store.get_settings(_pool)
            _settings["qualified_threshold"]    = int(s.get("qualified_threshold", 70))
            _settings["disqualified_threshold"] = int(s.get("disqualified_threshold", 60))
            _exclusions[:] = await store.get_exclusions(_pool)
            await store.seed_email_templates(_pool)
            _templates[:] = await store.get_email_templates(_pool)
            logger.info("PostgreSQL connected: %s", DATABASE_URL.split("@")[-1])
        except Exception as exc:
            logger.error("DB connection failed: %s — running without persistence", exc)
            _pool = None
    else:
        logger.warning("DATABASE_URL not set — results will not persist across restarts")

    yield

    if _pool:
        await _pool.close()


app = FastAPI(lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, max_age=60 * 60 * 8)


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _is_authenticated(request: Request) -> bool:
    return request.session.get("authenticated") is True


def _require_auth(request: Request):
    if not _is_authenticated(request):
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
                art, ev = {}, {}
                input_payload = {}
                art_p = run_dir / "artifacts" / f"{lid}_decision.json"
                ev_p  = run_dir / "artifacts" / f"{lid}_evidence.json"
                input_p = run_dir / "artifacts" / f"{lid}_input.json"
                if art_p.exists():
                    art = json.loads(art_p.read_text())
                if ev_p.exists():
                    ev = json.loads(ev_p.read_text())
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
                })
    leads.sort(key=lambda x: x["score"], reverse=True)
    return leads


def _summarize_run_dir(run_dir: Path) -> dict:
    leads = _read_run_files(run_dir)
    counts = {"qualified": 0, "review": 0, "disqualified": 0}
    for lead in leads:
        if lead["decision"] in counts:
            counts[lead["decision"]] += 1
    return {
        "run_id": run_dir.name,
        "filename": run_dir.name,
        "status": "done",
        "lead_count": len(leads),
        "n_qualified": counts["qualified"],
        "n_review": counts["review"],
        "n_disqualified": counts["disqualified"],
        "created_at": datetime.fromtimestamp(run_dir.stat().st_mtime).isoformat(),
    }


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


def _build_email(lead: dict) -> dict:
    first_name = (lead.get("name") or "there").split()[0]
    company    = lead.get("company") or "your company"
    industry   = (lead.get("industry") or "").strip().lower()
    vertical   = _SEGMENT_TO_VERTICAL.get(industry, "ai_generic")

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
        subject = f"Seedance 2.0 + {company}?"
        body    = f"Hi {first_name},\n\nWould this be a good time to explore ByteDance models and potential partnerships?\n\n/Antti\nBook 30min: https://bytedance.sg.larkoffice.com/scheduler/d18e3ecd70eae8c6"

    return {"subject": subject, "body": body, "vertical": vertical}


async def _send_to_instantly(leads: list) -> dict:
    ssl_verify = os.getenv("LLM_SSL_VERIFY", "true").lower() not in ("false", "0", "no")
    results = []
    async with httpx.AsyncClient(
        timeout=30,
        verify=ssl_verify,
        headers={"Authorization": f"Bearer {INSTANTLY_API_KEY}"},
    ) as client:
        for lead in leads:
            name = (lead.get("name") or "").strip()
            parts = name.split(None, 1)
            payload = {
                "email":           lead.get("email") or PLACEHOLDER_EMAIL,
                "first_name":      parts[0] if parts else "",
                "last_name":       parts[1] if len(parts) > 1 else "",
                "company_name":    lead.get("company") or "",
                "website":         lead.get("company_website") or "",
                "personalization": _build_email(lead)["body"],
                "campaign_id":     INSTANTLY_CAMPAIGN_ID,
            }
            resp = await client.post("https://api.instantly.ai/api/v2/leads", json=payload)
            if not resp.is_success:
                logger.error(
                    "Instantly API error %d for %s: %s",
                    resp.status_code, payload["email"], resp.text,
                )
                resp.raise_for_status()
            results.append(resp.json())
    return {"leads_sent": len(results)}


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
async def run_pipeline(request: Request, file: UploadFile = File(...), concurrency: int = Form(10)):
    _require_auth(request)
    run_id  = str(uuid.uuid4())
    content = await file.read()

    # Save upload to a temp file for main.py to read
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".csv")
    tmp.write(content)
    tmp.close()

    # Per-run isolated output directory
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # Persist run record
    if _pool:
        await store.insert_run(_pool, run_id, file.filename or "upload.csv", concurrency)

    _jobs[run_id] = {"lines": [], "status": "running", "returncode": None}
    asyncio.create_task(_run_job(run_id, tmp.name, run_dir, concurrency))

    return JSONResponse({"run_id": run_id})


async def _run_job(run_id: str, csv_path: str, run_dir: Path, concurrency: int):
    cmd = [
        sys.executable, str(ROOT / "main.py"),
        "--input",       csv_path,
        "--output-dir",  str(run_dir),
        "--concurrency", str(max(5, min(20, concurrency))),
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
            await _import_to_db(run_id, run_dir, rc)
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


async def _import_to_db(run_id: str, run_dir: Path, returncode: int):
    leads = _read_run_files(run_dir)
    for lead in leads:
        await store.upsert_lead(_pool, run_id, lead)
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
            "is_manual":  l["score"] > INSTANTLY_SCORE_THRESHOLD,
            "email_sent": l.get("email_sent", False),
        }
        for l in leads
    ]
    return JSONResponse(previews)


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

    import io
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["name", "company", "decision", "score", "email_subject", "email_body"])
    for l in leads:
        email = _build_email(l)
        writer.writerow([l["name"], l["company"], l["decision"], l["score"],
                         email["subject"], email["body"]])

    filename = f"leads-{run_id[:8]}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/runs/{run_id}/send-emails")
async def send_emails(request: Request, run_id: str):
    _require_auth(request)
    if not INSTANTLY_API_KEY or not INSTANTLY_CAMPAIGN_ID:
        return JSONResponse({"error": "Instantly not configured"}, status_code=400)
    if not _pool:
        return JSONResponse({"error": "Database required for email sending"}, status_code=400)

    eligible = await store.get_leads_for_email(_pool, run_id, INSTANTLY_SCORE_THRESHOLD)
    eligible = [l for l in eligible if not _is_excluded(l)]
    if not eligible:
        return JSONResponse({"sent": 0, "message": "No eligible leads to send"})

    try:
        await _send_to_instantly(eligible)
    except Exception as exc:
        return JSONResponse({"error": f"Instantly API error: {exc}"}, status_code=502)

    lead_ids = [l["lead_id"] for l in eligible]
    await store.mark_leads_email_sent(_pool, run_id, lead_ids)

    all_leads = await store.get_leads(_pool, run_id)
    unsent = [l for l in all_leads
              if not l.get("email_sent")
              and l["decision"] in ("qualified", "review")
              and l["score"] <= INSTANTLY_SCORE_THRESHOLD]
    status = "partial" if unsent else "sent"
    await store.update_run_email_status(_pool, run_id, status)

    return JSONResponse({"sent": len(eligible), "email_status": status})


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
    if _pool:
        await store.save_settings(_pool, {"qualified_threshold": str(qt), "disqualified_threshold": str(dt)})
    _settings["qualified_threshold"]    = qt
    _settings["disqualified_threshold"] = dt
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


# ── HTML ──────────────────────────────────────────────────────────────────────

_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Seedance · Sign in</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0d0e0f; color: #f0ede8; font-family: system-ui, -apple-system, sans-serif;
       min-height: 100vh; display: flex; align-items: center; justify-content: center; }
.card { width: 100%; max-width: 360px; background: #141516; border: 1px solid rgba(255,255,255,0.07);
        border-radius: 16px; padding: 32px; display: flex; flex-direction: column; gap: 20px; }
.logo { display: flex; align-items: center; gap: 10px; margin-bottom: 4px; }
.logo-mark { width: 32px; height: 32px; background: #c8ff57; border-radius: 8px;
             display: flex; align-items: center; justify-content: center; }
.logo-mark svg { width: 16px; height: 16px; }
.logo-text { font-size: 15px; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase; }
.logo-sub  { font-size: 11px; color: #5c5a57; letter-spacing: 0.04em; }
h2 { font-size: 19px; font-weight: 700; }
label { font-size: 12px; color: #9b9790; display: block; margin-bottom: 6px; }
input { width: 100%; padding: 10px 12px; border-radius: 8px; border: 1px solid rgba(255,255,255,0.1);
        background: #1c1e20; color: #f0ede8; font-size: 14px; font-family: inherit; outline: none; }
input:focus { border-color: #c8ff57; }
.field { display: flex; flex-direction: column; }
button { width: 100%; padding: 13px; border-radius: 10px; border: none; background: #c8ff57;
         color: #0d0e0f; font-size: 14px; font-weight: 700; cursor: pointer; font-family: inherit; }
button:hover { background: #d4ff3a; }
.login-error { font-size: 12px; color: #f87171; text-align: center; }
</style>
</head>
<body>
<div class="card">
  <div class="logo">
    <div class="logo-mark"><svg viewBox="0 0 14 14" fill="none"><rect x="1" y="1" width="5" height="5" rx="1" fill="#0d0e0f"/><rect x="8" y="1" width="5" height="5" rx="1" fill="#0d0e0f"/><rect x="1" y="8" width="5" height="5" rx="1" fill="#0d0e0f"/><rect x="8" y="8" width="2" height="5" rx="1" fill="#0d0e0f"/></svg></div>
    <div><div class="logo-text">Seedance</div><div class="logo-sub">Lead Pipeline</div></div>
  </div>
  <h2>Sign in</h2>
  <!--ERROR-->
  <form method="post" action="/login" style="display:flex;flex-direction:column;gap:16px;">
    <div class="field"><label>Username</label><input name="username" type="text" autocomplete="username" required autofocus></div>
    <div class="field"><label>Password</label><input name="password" type="password" autocomplete="current-password" required></div>
    <button type="submit">Sign in →</button>
  </form>
</div>
</body>
</html>"""

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Seedance · Lead Pipeline</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Syne:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg: #0d0e0f; --bg2: #141516; --bg3: #1c1e20;
  --border: rgba(255,255,255,0.07); --border2: rgba(255,255,255,0.12);
  --text: #f0ede8; --text2: #9b9790; --text3: #5c5a57;
  --green: #4ade80; --green-bg: rgba(74,222,128,0.08);
  --amber: #fbbf24; --amber-bg: rgba(251,191,36,0.08);
  --red: #f87171; --red-bg: rgba(248,113,113,0.08);
  --blue: #60a5fa; --accent: #c8ff57;
}
body { background: var(--bg); color: var(--text); font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; min-height: 100vh; line-height: 1.55; -webkit-font-smoothing: antialiased; text-rendering: optimizeLegibility; }
.screen { display: none; }
.screen.active { display: block; }

/* ── Upload ── */
.upload-shell { min-height:100vh; display:flex; flex-direction:column; align-items:center; justify-content:center; padding:40px 20px; gap:28px; }
.logo-wrap { display:flex; align-items:center; gap:10px; }
.logo-mark { width:32px; height:32px; background:var(--accent); border-radius:8px; display:flex; align-items:center; justify-content:center; }
.logo-mark svg { width:16px; height:16px; }
.logo-text { font-size:15px; font-weight:700; letter-spacing:0.06em; text-transform:uppercase; font-family:'Syne',sans-serif; }
.logo-sub { font-size:11px; color:var(--text3); letter-spacing:0.04em; }
.upload-card { width:100%; max-width:460px; background:var(--bg2); border:1px solid var(--border); border-radius:16px; padding:28px; display:flex; flex-direction:column; gap:20px; }
.upload-history-card { width:100%; max-width:460px; background:var(--bg2); border:1px solid var(--border); border-radius:16px; padding:18px; display:flex; flex-direction:column; gap:12px; }
.upload-history-head { display:flex; align-items:center; justify-content:space-between; gap:12px; }
.upload-history-title { font-size:12px; font-family:'DM Mono',monospace; color:var(--text3); letter-spacing:0.08em; text-transform:uppercase; }
.upload-history-sub { font-size:11px; color:var(--text3); font-family:'DM Mono',monospace; }
.upload-runs { display:flex; flex-direction:column; gap:6px; max-height:260px; overflow-y:auto; }
.upload-card h2 { font-size:19px; font-weight:700; font-family:'Syne',sans-serif; }
.upload-card p { font-size:12px; color:var(--text3); font-family:'DM Mono',monospace; margin-top:3px; }
.dropzone { border:1px dashed var(--border2); border-radius:10px; padding:28px 20px; cursor:pointer; transition:all 0.15s; display:flex; flex-direction:column; align-items:center; gap:10px; min-height:110px; text-align:center; background:var(--bg3); }
.dropzone:hover, .dropzone.drag-over { border-color:var(--accent); background:rgba(200,255,87,0.04); }
.dropzone svg { color:var(--text3); transition:color 0.15s; }
.dropzone.drag-over svg { color:var(--accent); }
.drop-hint-text { font-size:14px; color:var(--text2); }
.drop-hint-sub { font-size:11px; color:var(--text3); font-family:'DM Mono',monospace; }
.file-info { display:none; flex-direction:column; align-items:center; gap:5px; }
.file-info.show { display:flex; }
.file-icon { color:var(--accent); }
.file-name-text { font-size:13px; font-weight:600; }
.file-size-text { font-size:11px; color:var(--text3); font-family:'DM Mono',monospace; }
.option-row { display:flex; align-items:center; justify-content:space-between; padding:10px 12px; background:var(--bg3); border-radius:8px; border:1px solid var(--border); }
.option-label { font-size:12px; color:var(--text2); }
.option-label span { color:var(--text3); font-family:'DM Mono',monospace; font-size:11px; margin-left:5px; }
.conc-wrap { display:flex; align-items:center; gap:8px; }
.conc-val { font-family:'DM Mono',monospace; font-size:14px; font-weight:600; color:var(--accent); min-width:22px; text-align:center; }
.conc-btn { width:22px; height:22px; border-radius:5px; border:1px solid var(--border2); background:var(--bg2); color:var(--text2); cursor:pointer; font-size:14px; display:flex; align-items:center; justify-content:center; transition:all 0.1s; line-height:1; }
.conc-btn:hover { background:var(--bg3); color:var(--text); }
.submit-btn { width:100%; padding:13px; border-radius:10px; border:none; background:var(--accent); color:#0d0e0f; font-family:inherit; font-size:14px; font-weight:700; cursor:pointer; transition:all 0.15s; letter-spacing:0.02em; }
.submit-btn:hover:not(:disabled) { background:#d4ff3a; transform:translateY(-1px); }
.submit-btn:disabled { opacity:0.3; cursor:not-allowed; }
.link-btn { background:none; border:none; color:var(--text3); font-family:'DM Mono',monospace; font-size:12px; cursor:pointer; padding:6px; transition:color 0.15s; letter-spacing:0.03em; }
.link-btn:hover { color:var(--text2); }

/* ── Progress ── */
.progress-shell { min-height:100vh; max-width:780px; margin:0 auto; padding:48px 32px; display:flex; flex-direction:column; gap:28px; }
.progress-title-row { display:flex; align-items:center; gap:12px; }
.progress-title { font-size:21px; font-weight:700; font-family:'Syne',sans-serif; }
.progress-spinner { width:18px; height:18px; border:2px solid var(--border2); border-top-color:var(--accent); border-radius:50%; animation:spin 0.8s linear infinite; flex-shrink:0; }
@keyframes spin { to { transform:rotate(360deg); } }
.done-check { width:18px; height:18px; color:var(--green); flex-shrink:0; }
.prog-counters { display:flex; gap:14px; flex-wrap:wrap; }
.pcounter { display:flex; flex-direction:column; gap:2px; padding:10px 16px; border-radius:8px; background:var(--bg2); border:1px solid var(--border); min-width:80px; }
.pcount { font-size:26px; font-weight:700; font-family:'DM Mono',monospace; line-height:1; }
.pcounter.grey .pcount { color:var(--text2); }
.pcounter.green .pcount { color:var(--green); }
.pcounter.amber .pcount { color:var(--amber); }
.pcounter.red .pcount { color:var(--red); }
.plabel { font-size:10px; color:var(--text3); font-family:'DM Mono',monospace; text-transform:uppercase; letter-spacing:0.05em; }
.log-terminal { flex:1; background:var(--bg2); border:1px solid var(--border); border-radius:10px; padding:14px 16px; overflow-y:auto; font-family:'DM Mono',monospace; font-size:11.5px; line-height:1.75; max-height:480px; min-height:260px; }
.log-line { color:var(--text3); }
.log-qual { color:var(--green); }
.log-rev  { color:var(--amber); }
.log-err  { color:var(--red); }
.log-info { color:var(--text2); }

/* ── Results shell ── */
.results-shell { display:grid; grid-template-columns:230px 1fr; min-height:100vh; }
.sidebar { background:var(--bg2); border-right:1px solid var(--border); padding:24px 18px; position:sticky; top:0; height:100vh; overflow-y:auto; display:flex; flex-direction:column; gap:26px; }
.sidebar-label { font-family:'DM Mono',monospace; font-size:10px; letter-spacing:0.1em; text-transform:uppercase; color:var(--text3); margin-bottom:8px; }
.sidebar-head { display:flex; align-items:center; justify-content:space-between; gap:10px; margin-bottom:8px; }
.sidebar-head .sidebar-label { margin-bottom:0; }
.see-all-btn { background:none; border:none; color:var(--text3); font-family:'DM Mono',monospace; font-size:10px; letter-spacing:0.06em; text-transform:uppercase; cursor:pointer; padding:0; transition:color 0.15s; }
.see-all-btn:hover { color:var(--text2); }
.see-all-btn.hidden { display:none; }
.filter-group { display:flex; flex-direction:column; gap:3px; }
.filter-btn { display:flex; align-items:center; justify-content:space-between; padding:7px 10px; border-radius:6px; border:none; background:transparent; color:var(--text2); font-family:inherit; font-size:13px; cursor:pointer; transition:all 0.15s; text-align:left; width:100%; }
.filter-btn:hover { background:var(--bg3); color:var(--text); }
.filter-btn.active { background:var(--bg3); color:var(--text); }
.filter-btn .count { font-family:'DM Mono',monospace; font-size:11px; padding:1px 6px; border-radius:4px; background:var(--bg); color:var(--text3); }
.filter-btn.active .count { color:var(--text2); }

/* Past runs list */
.past-runs { display:flex; flex-direction:column; gap:3px; }
.run-item { padding:8px 10px; border-radius:6px; cursor:pointer; transition:background 0.12s; border:1px solid transparent; }
.run-item:hover { background:var(--bg3); }
.run-item.current { background:var(--bg3); border-color:var(--border2); }
.run-item-name { font-size:12px; color:var(--text); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; font-weight:500; }
.run-item-meta { font-size:10px; color:var(--text3); font-family:'DM Mono',monospace; margin-top:2px; }
.run-item-badges { display:flex; gap:4px; margin-top:5px; flex-wrap:wrap; }
.run-micro-badge { font-size:9px; font-family:'DM Mono',monospace; padding:1px 5px; border-radius:3px; font-weight:600; }
.run-micro-badge.qualified    { background:var(--green-bg); color:var(--green); }
.run-micro-badge.review       { background:var(--amber-bg); color:var(--amber); }
.run-micro-badge.disqualified { background:var(--red-bg);   color:var(--red); }
.run-status-dot { display:inline-block; width:6px; height:6px; border-radius:50%; margin-right:4px; }
.run-status-dot.done  { background:var(--green); }
.run-status-dot.error { background:var(--red); }
.run-status-dot.running { background:var(--amber); animation:pulse 1s infinite; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }

.dim-list { display:flex; flex-direction:column; gap:7px; }
.dim-row { display:flex; justify-content:space-between; align-items:center; }
.dim-dot-label { display:flex; align-items:center; gap:6px; font-size:11px; color:var(--text3); font-family:'DM Mono',monospace; }
.dim-dot { width:7px; height:7px; border-radius:2px; }
.dim-max { font-size:11px; color:var(--text3); font-family:'DM Mono',monospace; }
.stats-block { display:flex; flex-direction:column; gap:8px; }
.stat-row { display:flex; justify-content:space-between; align-items:center; }
.stat-label { font-size:11px; color:var(--text3); font-family:'DM Mono',monospace; }
.stat-val { font-size:13px; font-weight:600; }
.sidebar-actions { display:flex; flex-direction:column; gap:6px; margin-top:auto; }
.sidebar-btn { display:block; width:100%; padding:7px 10px; border-radius:6px; border:1px solid var(--border); background:transparent; color:var(--text3); font-family:'DM Mono',monospace; font-size:11px; cursor:pointer; transition:all 0.15s; text-align:center; letter-spacing:0.04em; }
.sidebar-btn:hover { background:var(--bg3); color:var(--text2); border-color:var(--border2); }
.sidebar-btn.danger:hover { background:var(--red-bg); color:var(--red); border-color:var(--red); }

/* ── Results main ── */
.results-main { padding:30px 34px; overflow-y:auto; }
.page-header { margin-bottom:26px; }
.page-title { font-size:22px; font-weight:700; letter-spacing:-0.02em; margin-bottom:3px; font-family:'Syne',sans-serif; }
.page-meta { font-size:12px; color:var(--text3); font-family:'DM Mono',monospace; }
.summary-row { display:grid; grid-template-columns:repeat(4,1fr); gap:10px; margin-bottom:24px; }
.scard { background:var(--bg2); border:1px solid var(--border); border-radius:10px; padding:14px 16px; transition:border-color 0.15s; }
.scard:hover { border-color:var(--border2); }
.scard-label { font-size:10px; color:var(--text3); font-family:'DM Mono',monospace; letter-spacing:0.05em; margin-bottom:6px; }
.scard-val { font-size:26px; font-weight:700; line-height:1; }
.scard-val.green { color:var(--green); }
.scard-val.amber { color:var(--amber); }
.scard-val.red   { color:var(--red); }
.results-load-state { padding:14px 4px 22px; font-size:11px; color:var(--text3); font-family:'DM Mono',monospace; text-align:center; }

/* ── Lead rows ── */
.leads-grid { display:flex; flex-direction:column; gap:7px; }
.lead-row { background:var(--bg2); border:1px solid var(--border); border-radius:10px; overflow:hidden; transition:border-color 0.15s; animation:fadeIn 0.2s ease both; }
@keyframes fadeIn { from{opacity:0;transform:translateY(4px)} to{opacity:1} }
.lead-row:hover { border-color:var(--border2); }
.lead-summary { display:grid; grid-template-columns:36px 1fr 100px 80px auto 28px; align-items:center; gap:14px; padding:13px 16px; cursor:pointer; user-select:none; }
.avatar { width:36px; height:36px; border-radius:8px; display:flex; align-items:center; justify-content:center; font-size:12px; font-weight:600; flex-shrink:0; }
.lead-name { font-size:14px; font-weight:600; }
.lead-company { font-size:11px; color:var(--text3); margin-top:1px; font-family:'DM Mono',monospace; }
.lead-company-extra { font-size:11px; color:var(--text3); margin-top:2px; font-family:'DM Mono',monospace; }
.mini-bars { display:flex; align-items:flex-end; gap:3px; height:20px; }
.mini-bar { width:10px; border-radius:2px; }
.decision-badge { font-size:10px; font-weight:600; letter-spacing:0.07em; text-transform:uppercase; padding:3px 8px; border-radius:4px; font-family:'DM Mono',monospace; }
.decision-badge.qualified    { background:var(--green-bg); color:var(--green); }
.decision-badge.review       { background:var(--amber-bg); color:var(--amber); }
.decision-badge.disqualified { background:var(--red-bg);   color:var(--red); }
.score-num { font-size:21px; font-weight:700; font-family:'DM Mono',monospace; line-height:1; }
.score-num.green { color:var(--green); }
.score-num.amber { color:var(--amber); }
.score-num.red   { color:var(--red); }
.chevron { color:var(--text3); font-size:11px; transition:transform 0.2s; display:flex; align-items:center; justify-content:center; }
.lead-row.open .chevron { transform:rotate(90deg); }
.lead-detail { display:none; padding:0 16px 16px; border-top:1px solid var(--border); }
.lead-row.open .lead-detail { display:block; }
.detail-grid { display:grid; grid-template-columns:1fr 1fr; gap:22px; padding-top:16px; }
.detail-section-title { font-size:10px; font-weight:600; letter-spacing:0.1em; text-transform:uppercase; color:var(--text3); font-family:'DM Mono',monospace; margin-bottom:10px; }
.score-bars { display:flex; flex-direction:column; gap:9px; }
.bar-row { display:flex; align-items:center; gap:10px; }
.bar-label { font-size:12px; color:var(--text2); width:106px; flex-shrink:0; }
.bar-track { flex:1; height:4px; background:var(--bg3); border-radius:2px; overflow:hidden; }
.bar-fill { height:100%; border-radius:2px; }
.bar-score { font-size:11px; font-family:'DM Mono',monospace; color:var(--text3); min-width:36px; text-align:right; }
.reason-list { display:flex; flex-direction:column; gap:7px; }
.reason-item { font-size:12px; color:var(--text2); line-height:1.5; padding-left:13px; position:relative; }
.reason-item::before { content:''; position:absolute; left:0; top:7px; width:5px; height:5px; border-radius:50%; }
.reason-item.pos::before { background:var(--green); }
.reason-item.neg::before { background:var(--red); }
.use-case-box { margin-top:12px; padding:9px 11px; background:var(--bg3); border-radius:6px; border-left:2px solid var(--border2); font-size:12px; color:var(--text2); line-height:1.55; }
.use-case-label { font-size:10px; font-family:'DM Mono',monospace; color:var(--text3); margin-bottom:3px; letter-spacing:0.05em; text-transform:uppercase; }
.detail-links { display:flex; flex-direction:column; gap:8px; margin-top:12px; }
.detail-link { display:inline-flex; align-items:center; font-size:12px; color:var(--accent); text-decoration:none; font-family:'DM Mono',monospace; letter-spacing:0.02em; }
.detail-link:hover { text-decoration:underline; }
.detail-source { font-size:11px; color:var(--text3); font-family:'DM Mono',monospace; line-height:1.5; }
.conf-badge { font-size:10px; font-family:'DM Mono',monospace; padding:2px 6px; border-radius:3px; background:var(--bg3); color:var(--text3); }
.detail-meta { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:12px; }
.meta-pill { display:inline-flex; align-items:center; padding:3px 8px; border-radius:999px; background:var(--bg3); color:var(--text2); font-size:10px; font-family:'DM Mono',monospace; letter-spacing:0.03em; }
.ev-section { margin-top:14px; }
.ev-item { font-size:12px; color:var(--text2); line-height:1.55; padding:2px 0 2px 12px; position:relative; }
.ev-item::before { content:''; position:absolute; left:0; top:8px; width:4px; height:4px; border-radius:50%; background:var(--text3); }
.ev-item.inferred::before { background:var(--blue); opacity:0.5; }
.ev-item.unknown::before  { background:var(--amber); opacity:0.4; }
.ev-item.hard-dis::before { background:var(--red); }
.signal-badge { display:inline-block; padding:2px 7px; border-radius:4px; font-size:10px; font-family:'DM Mono',monospace; background:var(--bg3); color:var(--text3); margin-bottom:8px; }

/* ── Email preview ── */
.email-preview { margin-top:14px; border-top:1px solid var(--border); padding-top:14px; }
.email-preview-title { font-size:10px; font-weight:600; letter-spacing:0.1em; text-transform:uppercase; color:var(--text3); font-family:'DM Mono',monospace; margin-bottom:10px; }
.email-subject { display:flex; align-items:center; gap:8px; font-size:12px; font-family:'DM Mono',monospace; color:var(--text3); margin-bottom:8px; }
.email-subject span { color:var(--text2); flex:1; }
.email-body-wrap { position:relative; }
.email-body { background:var(--bg3); border-radius:6px; padding:12px; font-size:12px; color:var(--text2); line-height:1.7; white-space:pre-wrap; font-family:'DM Mono',monospace; }
.copy-btn { cursor:pointer; padding:3px 10px; border-radius:5px; font-size:11px; font-family:'DM Mono',monospace; background:var(--bg2); color:var(--text2); border:1px solid var(--border); white-space:nowrap; transition:all 0.15s; letter-spacing:0.03em; }
.copy-btn:hover { background:var(--bg3); color:var(--text1); border-color:var(--text3); }
.copy-btn.copied { color:var(--green); border-color:var(--green); background:rgba(74,222,128,0.08); }
.email-body-header { display:flex; align-items:center; justify-content:space-between; margin-bottom:6px; }
.email-body-label { font-size:10px; font-weight:600; letter-spacing:0.1em; text-transform:uppercase; color:var(--text3); font-family:'DM Mono',monospace; }
.manual-badge { display:inline-block; padding:2px 8px; border-radius:4px; font-size:10px; font-family:'DM Mono',monospace; background:rgba(96,165,250,0.1); color:#60a5fa; margin-bottom:8px; }
.email-sent-badge { display:inline-block; padding:2px 8px; border-radius:4px; font-size:10px; font-family:'DM Mono',monospace; background:rgba(74,222,128,0.1); color:var(--green); margin-bottom:8px; }
/* ── Email status on run items ── */
.email-status-badge { font-size:9px; font-family:'DM Mono',monospace; padding:1px 5px; border-radius:3px; font-weight:600; }
.email-status-badge.not_sent { background:rgba(255,255,255,0.05); color:var(--text3); }
.email-status-badge.sent     { background:rgba(74,222,128,0.1); color:var(--green); }
.email-status-badge.partial  { background:rgba(251,191,36,0.1); color:var(--amber); }

/* ── Confirm modal ── */
.modal-overlay { position:fixed; inset:0; background:rgba(0,0,0,0.6); display:flex; align-items:center; justify-content:center; z-index:100; }
.modal-overlay.hidden { display:none; }
.modal { background:var(--bg2); border:1px solid var(--border2); border-radius:12px; padding:24px; max-width:360px; width:90%; }
.runs-modal { background:var(--bg2); border:1px solid var(--border2); border-radius:14px; width:min(560px, 92vw); max-height:min(72vh, 760px); display:flex; flex-direction:column; overflow:hidden; }
.runs-modal-head { display:flex; align-items:center; justify-content:space-between; gap:12px; padding:18px 18px 14px; border-bottom:1px solid var(--border); }
.runs-modal-title { font-size:15px; font-weight:700; font-family:'Syne',sans-serif; }
.runs-modal-sub { font-size:11px; color:var(--text3); font-family:'DM Mono',monospace; margin-top:3px; }
.runs-modal-close { width:30px; height:30px; border-radius:8px; border:1px solid var(--border2); background:transparent; color:var(--text3); cursor:pointer; font-size:16px; line-height:1; transition:all 0.15s; }
.runs-modal-close:hover { background:var(--bg3); color:var(--text); }
.runs-modal-list { padding:12px; overflow-y:auto; display:flex; flex-direction:column; gap:6px; }
.runs-modal-empty { font-size:11px; color:var(--text3); font-family:'DM Mono',monospace; padding:8px 10px; }
.runs-modal .run-item { background:var(--bg); }
.runs-modal .run-item:hover { background:var(--bg3); }
.modal h3 { font-size:16px; font-weight:700; margin-bottom:8px; }
.modal p { font-size:13px; color:var(--text2); margin-bottom:20px; line-height:1.5; }
.modal-actions { display:flex; gap:10px; justify-content:flex-end; }
.modal-btn { padding:8px 16px; border-radius:7px; border:1px solid var(--border2); font-family:inherit; font-size:13px; font-weight:600; cursor:pointer; }
.modal-btn.cancel { background:transparent; color:var(--text2); }
.modal-btn.cancel:hover { background:var(--bg3); }
.modal-btn.confirm { background:var(--red-bg); color:var(--red); border-color:var(--red); }
.modal-btn.confirm:hover { background:var(--red); color:#fff; }

/* ── Settings screen ── */
.settings-shell { display:grid; grid-template-columns:220px 1fr; height:100vh; overflow:hidden; }
.settings-main { overflow-y:auto; padding:32px 36px; }
.settings-section { background:var(--bg2); border:1px solid var(--border); border-radius:12px; padding:24px; margin-bottom:18px; max-width:540px; }
.settings-section-title { font-size:14px; font-weight:700; font-family:'Syne',sans-serif; margin-bottom:4px; }
.settings-section-sub { font-size:11px; color:var(--text3); font-family:'DM Mono',monospace; margin-bottom:20px; line-height:1.6; }
.threshold-row { display:flex; align-items:center; gap:12px; margin-bottom:14px; }
.threshold-label { font-size:12px; color:var(--text2); font-family:'DM Mono',monospace; flex:1; }
.threshold-input { width:68px; background:var(--bg3); border:1px solid var(--border2); border-radius:6px; padding:6px 10px; color:var(--text1); font-family:'DM Mono',monospace; font-size:14px; text-align:center; }
.threshold-input:focus { outline:none; border-color:var(--text3); }
.threshold-hint { font-size:10px; color:var(--text3); font-family:'DM Mono',monospace; white-space:nowrap; }
.settings-save-btn { padding:8px 18px; border-radius:7px; background:var(--bg3); border:1px solid var(--border2); color:var(--text1); font-family:'DM Mono',monospace; font-size:12px; cursor:pointer; transition:all 0.15s; margin-top:4px; }
.settings-save-btn:hover { border-color:var(--text3); }
.settings-save-btn.saved { color:var(--green); border-color:var(--green); background:rgba(74,222,128,0.08); }
.exclusion-add-row { display:flex; gap:8px; margin-bottom:14px; }
.exclusion-input { flex:1; background:var(--bg3); border:1px solid var(--border2); border-radius:6px; padding:8px 12px; color:var(--text1); font-family:'DM Mono',monospace; font-size:12px; }
.exclusion-input::placeholder { color:var(--text3); }
.exclusion-input:focus { outline:none; border-color:var(--text3); }
.exclusion-list { display:flex; flex-direction:column; gap:6px; max-height:280px; overflow-y:auto; }
.exclusion-item { display:flex; align-items:center; justify-content:space-between; background:var(--bg3); border-radius:6px; padding:8px 12px; font-size:12px; font-family:'DM Mono',monospace; color:var(--text2); }
.exclusion-del { background:none; border:none; color:var(--text3); cursor:pointer; font-size:16px; padding:0 2px; line-height:1; transition:color 0.15s; }
.exclusion-del:hover { color:var(--red); }
.exclusion-empty { font-size:11px; color:var(--text3); font-family:'DM Mono',monospace; padding:6px 0; }
.sidebar-btn.active-nav { border-color:var(--text3); color:var(--text1); }
.leads-grid { transition: opacity 0.15s ease; }
.lead-row.excluded { opacity:0.38; }
.lead-row.excluded:hover { opacity:0.52; }
.customer-badge { display:inline-block; padding:1px 7px; border-radius:4px; font-size:10px; font-family:'DM Mono',monospace; background:rgba(255,255,255,0.05); color:var(--text3); border:1px solid var(--border); margin-left:7px; vertical-align:middle; letter-spacing:0.03em; }
.import-csv-row { display:flex; align-items:center; gap:10px; margin-top:12px; padding-top:12px; border-top:1px solid var(--border); }
.import-csv-label { font-size:11px; color:var(--text3); font-family:'DM Mono',monospace; flex:1; line-height:1.5; }
.leads-grid.rerendering { opacity: 0; }
.score-cell { display:flex; flex-direction:column; align-items:flex-end; gap:3px; min-width:52px; }
.manual-email-badge { font-size:9px; font-family:'DM Mono',monospace; padding:1px 5px; border-radius:3px; font-weight:600; white-space:nowrap; line-height:1.4; }
.manual-email-badge.sent     { background:rgba(74,222,128,0.12); color:var(--green); border:1px solid rgba(74,222,128,0.25); }
.manual-email-badge.not_sent { background:rgba(248,113,113,0.10); color:var(--red);   border:1px solid rgba(248,113,113,0.25); }

/* ── Email Templates ── */
.tmpl-list { display:flex; flex-direction:column; gap:8px; margin-bottom:14px; max-height:360px; overflow-y:auto; }
.tmpl-card { background:var(--bg3); border:1px solid var(--border); border-radius:8px; padding:12px 14px; }
.tmpl-card-top { display:flex; align-items:flex-start; gap:10px; }
.tmpl-vertical-badge { font-size:9px; font-family:'DM Mono',monospace; padding:2px 7px; border-radius:3px; font-weight:600; background:rgba(96,165,250,0.12); color:#60a5fa; border:1px solid rgba(96,165,250,0.2); white-space:nowrap; flex-shrink:0; margin-top:1px; }
.tmpl-info { flex:1; min-width:0; }
.tmpl-name { font-size:12px; font-weight:600; color:var(--text1); margin-bottom:2px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.tmpl-subject { font-size:11px; color:var(--text3); font-family:'DM Mono',monospace; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.tmpl-actions { display:flex; gap:4px; flex-shrink:0; }
.tmpl-btn { background:none; border:none; color:var(--text3); cursor:pointer; font-size:12px; padding:2px 5px; line-height:1; border-radius:4px; transition:all 0.15s; font-family:'DM Mono',monospace; }
.tmpl-btn:hover { color:var(--text1); background:var(--bg2); }
.tmpl-btn.del:hover { color:var(--red); background:var(--red-bg); }
.tmpl-btn.copied { color:var(--green); }
.tmpl-edit-form { display:none; flex-direction:column; gap:10px; margin-top:12px; padding-top:12px; border-top:1px solid var(--border); }
.tmpl-edit-form.open { display:flex; }
.tmpl-empty { font-size:11px; color:var(--text3); font-family:'DM Mono',monospace; padding:6px 0; }
.tmpl-add-form { border-top:1px solid var(--border); margin-top:6px; padding-top:16px; display:none; flex-direction:column; gap:10px; }
.tmpl-add-form.open { display:flex; }
.tmpl-form-row { display:flex; flex-direction:column; gap:4px; }
.tmpl-form-label { font-size:10px; font-family:'DM Mono',monospace; color:var(--text3); letter-spacing:0.06em; text-transform:uppercase; }
.tmpl-form-input { background:var(--bg); border:1px solid var(--border2); border-radius:6px; padding:7px 10px; color:var(--text1); font-family:'DM Mono',monospace; font-size:12px; }
.tmpl-form-input:focus { outline:none; border-color:var(--text3); }
select.tmpl-form-input { appearance:none; cursor:pointer; }
.tmpl-form-textarea { background:var(--bg); border:1px solid var(--border2); border-radius:6px; padding:8px 10px; color:var(--text1); font-family:'DM Mono',monospace; font-size:11px; resize:vertical; min-height:140px; line-height:1.55; }
.tmpl-form-textarea:focus { outline:none; border-color:var(--text3); }
.tmpl-form-hint { font-size:10px; color:var(--text3); font-family:'DM Mono',monospace; }
.tmpl-form-actions { display:flex; gap:8px; }
</style>
</head>
<body>

<!-- ── Screen 1: Upload ── -->
<section id="s-upload" class="screen active">
  <div class="upload-shell">
    <div class="logo-wrap">
      <div class="logo-mark"><svg viewBox="0 0 14 14" fill="none"><rect x="1" y="1" width="5" height="5" rx="1" fill="#0d0e0f"/><rect x="8" y="1" width="5" height="5" rx="1" fill="#0d0e0f"/><rect x="1" y="8" width="5" height="5" rx="1" fill="#0d0e0f"/><rect x="8" y="8" width="2" height="5" rx="1" fill="#0d0e0f"/></svg></div>
      <div><div class="logo-text">Seedance</div><div class="logo-sub">Lead Pipeline</div></div>
    </div>

    <div class="upload-card">
      <div>
        <h2>Upload leads</h2>
        <p>Trigify CSV export · each run is fully isolated</p>
      </div>

      <div id="dropzone" class="dropzone">
        <div id="drop-hint" style="display:flex;flex-direction:column;align-items:center;gap:10px;">
          <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
          <div class="drop-hint-text">Drop CSV here or click to browse</div>
          <div class="drop-hint-sub">.csv · Trigify format</div>
        </div>
        <div id="file-info" class="file-info">
          <svg class="file-icon" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
          <div id="file-name-text" class="file-name-text">—</div>
          <div id="file-size-text" class="file-size-text">—</div>
        </div>
      </div>
      <input type="file" id="file-input" accept=".csv" style="display:none">

      <div class="option-row">
        <div class="option-label">Concurrency <span>parallel leads</span></div>
        <div class="conc-wrap">
          <button class="conc-btn" id="conc-dec">−</button>
          <div class="conc-val" id="conc-display">10</div>
          <button class="conc-btn" id="conc-inc">+</button>
        </div>
      </div>

      <button id="submit-btn" class="submit-btn" disabled onclick="startRun()">Run Pipeline →</button>
    </div>

    <div class="upload-history-card">
      <div class="upload-history-head">
        <div class="upload-history-title">Historical runs</div>
        <div class="upload-history-sub" id="upload-runs-count">Loading…</div>
      </div>
      <div class="upload-runs" id="upload-runs-list">
        <div style="font-size:11px;color:var(--text3);font-family:'DM Mono',monospace;padding:4px 6px;">Loading…</div>
      </div>
    </div>

    <button class="link-btn" onclick="viewExisting()">Open latest run →</button>
  </div>
</section>

<!-- ── Screen 2: Progress ── -->
<section id="s-progress" class="screen">
  <div class="progress-shell">
    <div class="logo-wrap">
      <div class="logo-mark"><svg viewBox="0 0 14 14" fill="none"><rect x="1" y="1" width="5" height="5" rx="1" fill="#0d0e0f"/><rect x="8" y="1" width="5" height="5" rx="1" fill="#0d0e0f"/><rect x="1" y="8" width="5" height="5" rx="1" fill="#0d0e0f"/><rect x="8" y="8" width="2" height="5" rx="1" fill="#0d0e0f"/></svg></div>
      <div><div class="logo-text">Seedance</div><div class="logo-sub">Lead Pipeline</div></div>
    </div>
    <div>
      <div class="progress-title-row" id="prog-title-row">
        <div class="progress-spinner" id="prog-spinner"></div>
        <div class="progress-title" id="prog-title">Processing leads…</div>
      </div>
    </div>
    <div class="prog-counters">
      <div class="pcounter grey"><div class="pcount" id="prog-total">0</div><div class="plabel">Processed</div></div>
      <div class="pcounter green"><div class="pcount" id="prog-qual">0</div><div class="plabel">Qualified</div></div>
      <div class="pcounter amber"><div class="pcount" id="prog-rev">0</div><div class="plabel">Review</div></div>
      <div class="pcounter red"><div class="pcount" id="prog-dis">0</div><div class="plabel">Disqualified</div></div>
    </div>
    <div id="log-output" class="log-terminal"></div>
  </div>
</section>

<!-- ── Screen 3: Results ── -->
<section id="s-results" class="screen">
  <div class="results-shell">
    <aside class="sidebar">
      <div class="logo-wrap">
        <div class="logo-mark"><svg viewBox="0 0 14 14" fill="none"><rect x="1" y="1" width="5" height="5" rx="1" fill="#0d0e0f"/><rect x="8" y="1" width="5" height="5" rx="1" fill="#0d0e0f"/><rect x="1" y="8" width="5" height="5" rx="1" fill="#0d0e0f"/><rect x="8" y="8" width="2" height="5" rx="1" fill="#0d0e0f"/></svg></div>
        <div><div class="logo-text">Seedance</div><div class="logo-sub">Lead Pipeline</div></div>
      </div>

      <div>
        <div class="sidebar-label">Filter</div>
        <div class="filter-group">
          <button class="filter-btn active" data-filter="all"          onclick="filterLeads('all',this)">All leads   <span class="count" id="cnt-all">0</span></button>
          <button class="filter-btn"         data-filter="qualified"    onclick="filterLeads('qualified',this)">Qualified   <span class="count" id="cnt-qual">0</span></button>
          <button class="filter-btn"         data-filter="review"       onclick="filterLeads('review',this)">For review  <span class="count" id="cnt-rev">0</span></button>
          <button class="filter-btn"         data-filter="disqualified" onclick="filterLeads('disqualified',this)">Disqualified <span class="count" id="cnt-dis">0</span></button>
        </div>
      </div>

      <div>
        <div class="sidebar-head">
          <div class="sidebar-label">Past runs</div>
          <button class="see-all-btn hidden" id="see-all-runs-btn" onclick="openRunsModal()">See all</button>
        </div>
        <div class="past-runs" id="past-runs-list">
          <div style="font-size:11px;color:var(--text3);font-family:'DM Mono',monospace;padding:4px 6px;">Loading…</div>
        </div>
      </div>

      <div class="sidebar-actions">
        <button class="sidebar-btn" onclick="show('s-upload')">+ New run</button>
        <button class="sidebar-btn" id="send-instantly-btn" onclick="openSendModal()">Send to Instantly</button>
        <button class="sidebar-btn" onclick="exportRun()">↓ Export CSV</button>
        <button class="sidebar-btn" onclick="openSettings()">⚙ Settings</button>
        <button class="sidebar-btn danger" onclick="confirmDelete()">Delete this run</button>
      </div>
    </aside>

    <main class="results-main">
      <div class="page-header">
        <div class="page-title">Lead Qualification</div>
        <div class="page-meta" id="page-meta">—</div>
      </div>
      <div class="summary-row">
        <div class="scard"><div class="scard-label">Total leads</div><div class="scard-val" id="sum-total">—</div></div>
        <div class="scard"><div class="scard-label">Qualified</div><div class="scard-val green" id="sum-qual">—</div></div>
        <div class="scard"><div class="scard-label">For review</div><div class="scard-val amber" id="sum-rev">—</div></div>
        <div class="scard"><div class="scard-label">Disqualified</div><div class="scard-val red" id="sum-dis">—</div></div>
      </div>
      <div class="leads-grid" id="leads"></div>
      <div class="results-load-state" id="results-load-state"></div>
    </main>
  </div>
</section>

<!-- ── Delete confirm modal ── -->
<div class="modal-overlay hidden" id="delete-modal">
  <div class="modal">
    <h3>Delete this run?</h3>
    <p>All leads and scores for this run will be permanently removed from the database. This cannot be undone.</p>
    <div class="modal-actions">
      <button class="modal-btn cancel" onclick="closeModal()">Cancel</button>
      <button class="modal-btn confirm" onclick="deleteCurrentRun()">Delete</button>
    </div>
  </div>
</div>

<div class="modal-overlay hidden" id="send-modal">
  <div class="modal">
    <h3>Send to Instantly</h3>
    <p id="send-modal-body">Loading eligible leads...</p>
    <div class="modal-actions">
      <button class="modal-btn cancel" onclick="closeSendModal()">Cancel</button>
      <button class="modal-btn" id="send-modal-confirm" style="background:var(--green-bg);color:var(--green);border-color:var(--green);" onclick="confirmSendEmails()">Send</button>
    </div>
  </div>
</div>

<div class="modal-overlay hidden" id="runs-modal" onclick="if(event.target===this) closeRunsModal()">
  <div class="runs-modal">
    <div class="runs-modal-head">
      <div>
        <div class="runs-modal-title">All previous runs</div>
        <div class="runs-modal-sub" id="runs-modal-count">0 runs</div>
      </div>
      <button class="runs-modal-close" onclick="closeRunsModal()">×</button>
    </div>
    <div class="runs-modal-list" id="runs-modal-list">
      <div class="runs-modal-empty">Loading…</div>
    </div>
  </div>
</div>

<!-- ── Email sent confirmation modal (blocking — no dismiss on backdrop) ── -->
<div class="modal-overlay hidden" id="email-confirm-modal">
  <div class="modal" style="max-width:380px;">
    <h3 id="email-confirm-title" style="margin-bottom:10px;">Did you send this email?</h3>
    <p id="email-confirm-sub" style="margin-bottom:24px;">Mark whether you sent the email to <strong id="email-confirm-name"></strong> at <strong id="email-confirm-company"></strong>.</p>
    <div class="modal-actions" style="justify-content:stretch;gap:10px;">
      <button class="modal-btn" id="email-confirm-no" style="flex:1;background:var(--bg3);color:var(--text2);" onclick="confirmEmailStatus('not_sent')">No, didn't send</button>
      <button class="modal-btn confirm" id="email-confirm-yes" style="flex:1;background:var(--green-bg);color:var(--green);border-color:var(--green);" onclick="confirmEmailStatus('sent')">Yes, sent it ✓</button>
    </div>
  </div>
</div>

<!-- ── Screen 4: Settings ── -->
<section id="s-settings" class="screen">
  <div class="settings-shell">
    <aside class="sidebar">
      <div class="logo-wrap">
        <div class="logo-mark"><svg viewBox="0 0 14 14" fill="none"><rect x="1" y="1" width="5" height="5" rx="1" fill="#0d0e0f"/><rect x="8" y="1" width="5" height="5" rx="1" fill="#0d0e0f"/><rect x="1" y="8" width="5" height="5" rx="1" fill="#0d0e0f"/><rect x="8" y="8" width="2" height="5" rx="1" fill="#0d0e0f"/></svg></div>
        <div><div class="logo-text">Seedance</div><div class="logo-sub">Lead Pipeline</div></div>
      </div>
      <div class="sidebar-actions" style="margin-top:auto;">
        <button class="sidebar-btn" onclick="show('s-upload')">+ New run</button>
        <button class="sidebar-btn" onclick="backToResults()">← Back to results</button>
        <button class="sidebar-btn active-nav">⚙ Settings</button>
      </div>
    </aside>
    <main class="settings-main">
      <div class="page-header">
        <div class="page-title">Settings</div>
        <div class="page-meta">Pipeline configuration</div>
      </div>

      <!-- Score Thresholds -->
      <div class="settings-section">
        <div class="settings-section-title">Score Thresholds</div>
        <div class="settings-section-sub">Leads are reclassified instantly using your saved thresholds — no need to re-run the pipeline.</div>
        <div class="threshold-row">
          <span class="threshold-label">Qualified if score ≥</span>
          <input type="number" class="threshold-input" id="input-qual-threshold" min="1" max="100" value="70">
        </div>
        <div class="threshold-row">
          <span class="threshold-label">Review if score ≥</span>
          <input type="number" class="threshold-input" id="input-dis-threshold" min="1" max="100" value="60">
          <span class="threshold-hint">below this = disqualified</span>
        </div>
        <button class="settings-save-btn" id="save-thresholds-btn" onclick="saveThresholds()">Save thresholds</button>
      </div>

      <!-- Exclusion List -->
      <div class="settings-section">
        <div class="settings-section-title">Exclusion List</div>
        <div class="settings-section-sub">Add company website URLs of existing customers (e.g. <code style="font-size:10px;background:var(--bg3);padding:1px 4px;border-radius:3px;">imagineart.io</code>). Matched leads are greyed out in results and never sent to Instantly.</div>
        <div class="exclusion-add-row">
          <input type="text" class="exclusion-input" id="exclusion-input" placeholder="e.g. imagineart.io or https://veed.io" onkeydown="if(event.key==='Enter') addExclusion()">
          <button class="settings-save-btn" onclick="addExclusion()">Add</button>
        </div>
        <div class="exclusion-list" id="exclusion-list">
          <div class="exclusion-empty">No exclusions yet.</div>
        </div>
        <div class="import-csv-row">
          <span class="import-csv-label">Bulk import from Trigify CSV — reads the COMPANY URL column automatically</span>
          <input type="file" id="exclusion-csv-input" accept=".csv" style="display:none" onchange="importExclusionCsv(this)">
          <button class="settings-save-btn" id="import-csv-btn" onclick="document.getElementById('exclusion-csv-input').click()">Import CSV</button>
        </div>
      </div>

      <!-- Email Templates -->
      <div class="settings-section" style="max-width:600px;">
        <div class="settings-section-title">Email Templates</div>
        <div class="settings-section-sub">Templates are selected automatically based on the lead's industry vertical. Use <code style="font-size:10px;background:var(--bg3);padding:1px 4px;border-radius:3px;">{first_name}</code> and <code style="font-size:10px;background:var(--bg3);padding:1px 4px;border-radius:3px;">{company}</code> as placeholders.</div>
        <div class="tmpl-list" id="tmpl-list"><div class="tmpl-empty">Loading...</div></div>
        <button class="settings-save-btn" id="tmpl-add-btn" onclick="toggleTmplForm()">+ Add template</button>
        <div class="tmpl-add-form" id="tmpl-add-form">
          <div class="tmpl-form-row">
            <label class="tmpl-form-label">Vertical</label>
            <select class="tmpl-form-input" id="tmpl-vertical">
              <option value="ai_tools">AI Tools</option>
              <option value="film_production">Film Production</option>
              <option value="vfx_production">VFX Production</option>
              <option value="animation">Animation</option>
              <option value="short_drama">Short Drama</option>
              <option value="ecommerce">Ecommerce</option>
              <option value="ai_generic">AI Generic</option>
            </select>
          </div>
          <div class="tmpl-form-row">
            <label class="tmpl-form-label">Template name</label>
            <input type="text" class="tmpl-form-input" id="tmpl-name" placeholder="e.g. Film Production – Version B">
          </div>
          <div class="tmpl-form-row">
            <label class="tmpl-form-label">Subject</label>
            <input type="text" class="tmpl-form-input" id="tmpl-subject" placeholder="e.g. Seedance 2.0 + {company}?">
          </div>
          <div class="tmpl-form-row">
            <label class="tmpl-form-label">Body</label>
            <textarea class="tmpl-form-textarea" id="tmpl-body" placeholder="Hi {first_name},&#10;&#10;...&#10;&#10;-- Antti"></textarea>
            <span class="tmpl-form-hint">Use {first_name} and {company} for personalisation. For images, paste a public URL inline (e.g. https://example.com/image.png).</span>
          </div>
          <div class="tmpl-form-actions">
            <button class="settings-save-btn" onclick="saveTmpl()">Save template</button>
            <button class="settings-save-btn" onclick="toggleTmplForm()" style="color:var(--text3)">Cancel</button>
          </div>
        </div>
      </div>
    </main>
  </div>
</section>

<script>
// ── State ──────────────────────────────────────────────────────────────────────
let allLeads       = [];
let allRuns        = [];
let emailPreviews  = {};
let appSettings    = { qualified_threshold: 70, disqualified_threshold: 60 };
let exclusions     = [];
let emailTemplates = [];
let currentRunId   = null;
let selectedFile  = null;
let concurrency   = 10;
let currentFilter = 'all';
let visibleLeadCount = 0;

const INITIAL_LEADS_PAGE = 50;
const LEADS_PAGE_SIZE = 50;
const LEADS_PREFETCH_OFFSET = 5;

const MAX = { segment:30, workflow:15, pain:20, buyer:25, commercial:10 };
const CLR = { segment:'#60a5fa', workflow:'#4ade80', pain:'#fb923c', buyer:'#c084fc', commercial:'#fbbf24' };
const LBL = { segment:'Segment fit', workflow:'Workflow fit', pain:'Pain fit', buyer:'Buyer fit', commercial:'Commercial fit' };
const PALETTES = [
  ['#1a3a2a','#4ade80'],['#1a2a3a','#60a5fa'],['#2a1a3a','#c084fc'],
  ['#3a2a1a','#fbbf24'],['#3a1a1a','#f87171'],['#1a3a38','#2dd4bf'],
];

// ── Screens ────────────────────────────────────────────────────────────────────
function show(id) {
  document.querySelectorAll('.screen').forEach(s => s.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  if (id === 's-results' || id === 's-upload') loadPastRuns();
  if (id === 's-upload') clearRunIdFromUrl();
}

// ── Upload ─────────────────────────────────────────────────────────────────────
const dropzone  = document.getElementById('dropzone');
const fileInput = document.getElementById('file-input');

dropzone.addEventListener('click', () => fileInput.click());
dropzone.addEventListener('dragover', e => { e.preventDefault(); dropzone.classList.add('drag-over'); });
dropzone.addEventListener('dragleave', () => dropzone.classList.remove('drag-over'));
dropzone.addEventListener('drop', e => { e.preventDefault(); dropzone.classList.remove('drag-over'); if (e.dataTransfer.files[0]) setFile(e.dataTransfer.files[0]); });
fileInput.addEventListener('change', e => { if (e.target.files[0]) setFile(e.target.files[0]); });

function setFile(file) {
  selectedFile = file;
  document.getElementById('drop-hint').style.display = 'none';
  document.getElementById('file-info').classList.add('show');
  document.getElementById('file-name-text').textContent = file.name;
  document.getElementById('file-size-text').textContent = (file.size / 1024).toFixed(1) + ' KB';
  document.getElementById('submit-btn').disabled = false;
}

document.getElementById('conc-dec').addEventListener('click', () => {
  if (concurrency > 5) { concurrency--; document.getElementById('conc-display').textContent = concurrency; }
});
document.getElementById('conc-inc').addEventListener('click', () => {
  if (concurrency < 20) { concurrency++; document.getElementById('conc-display').textContent = concurrency; }
});

async function startRun() {
  if (!selectedFile) return;
  const fd = new FormData();
  fd.append('file', selectedFile);
  fd.append('concurrency', concurrency);
  resetProgress();
  show('s-progress');
  const { run_id } = await fetch('/api/run', { method:'POST', body:fd }).then(r => r.json());
  currentRunId = run_id;
  setRunIdInUrl(run_id);
  connectSSE(run_id);
}

async function viewExisting() {
  const runs = await fetch('/api/runs').then(r => r.json());
  if (!runs.length) { alert('No runs found. Upload a CSV to get started.'); return; }
  await loadRun(runs[0].run_id);
}

// ── Progress ───────────────────────────────────────────────────────────────────
const pStats = { qual:0, rev:0, dis:0 };

function resetProgress() {
  pStats.qual = pStats.rev = pStats.dis = 0;
  ['prog-total','prog-qual','prog-rev','prog-dis'].forEach(id => document.getElementById(id).textContent = '0');
  document.getElementById('log-output').innerHTML = '';
  document.getElementById('prog-title').textContent = 'Processing leads…';
  document.getElementById('prog-title').style.color = '';
  document.getElementById('prog-spinner').style.display = '';
}

function connectSSE(runId) {
  const es = new EventSource('/api/progress/' + runId);
  es.onmessage = e => {
    const d = JSON.parse(e.data);
    if (d.done) {
      es.close();
      document.getElementById('prog-spinner').style.display = 'none';
      if (d.returncode === 0) {
        document.getElementById('prog-title').textContent = 'Done — loading results…';
        loadRun(runId);
      } else {
        document.getElementById('prog-title').textContent = 'Pipeline failed';
        document.getElementById('prog-title').style.color = 'var(--red)';
      }
    } else {
      appendLog(d.line);
      parseLog(d.line);
    }
  };
  es.onerror = () => es.close();
}

function appendLog(line) {
  const isResult = line.includes('✓') || line.includes('✗');
  const isError  = line.includes('ERROR') || line.includes('FAILED') || line.includes('TIMED OUT');
  if (!isResult && !isError) return;  // suppress INFO/DEBUG noise

  const el = document.getElementById('log-output');
  const div = document.createElement('div');
  div.className = 'log-line';
  if      (line.includes('✓') && line.includes('| QUALIFIED'))    div.classList.add('log-qual');
  else if (line.includes('✓') && line.includes('| REVIEW'))       div.classList.add('log-rev');
  else if (isError)                                                div.classList.add('log-err');

  // Show a clean version: "✓ Company Name | QUALIFIED | score=85"
  const match = line.match(/[✓✗]\s+lead_id=(\S+)\s+\|\s+(\w+)\s+\|\s+score=\s*(\d+)/);
  div.textContent = match
    ? `${line.includes('✓') ? '✓' : '✗'}  ${match[1].padEnd(30)} ${match[2].padEnd(14)} score ${match[3]}`
    : line;

  el.appendChild(div);
  el.scrollTop = el.scrollHeight;
}

function parseLog(line) {
  if (!line.includes('✓')) return;
  if      (line.includes('| QUALIFIED'))    pStats.qual++;
  else if (line.includes('| REVIEW'))       pStats.rev++;
  else if (line.includes('| DISQUALIFIED')) pStats.dis++;
  else return;
  const total = pStats.qual + pStats.rev + pStats.dis;
  document.getElementById('prog-total').textContent = total;
  document.getElementById('prog-qual').textContent  = pStats.qual;
  document.getElementById('prog-rev').textContent   = pStats.rev;
  document.getElementById('prog-dis').textContent   = pStats.dis;
}

// ── Results ────────────────────────────────────────────────────────────────────
function setRunIdInUrl(runId) {
  const url = new URL(window.location.href);
  url.searchParams.set('run_id', runId);
  window.history.replaceState({}, '', url);
}

function clearRunIdFromUrl() {
  const url = new URL(window.location.href);
  if (!url.searchParams.has('run_id')) return;
  url.searchParams.delete('run_id');
  window.history.replaceState({}, '', url);
}

function getRunIdFromUrl() {
  return new URL(window.location.href).searchParams.get('run_id');
}

function getRunMeta(runId) {
  return allRuns.find(r => r.run_id === runId) || null;
}

function formatRunDateTime(value) {
  if (!value) return 'Unknown time';
  const dt = new Date(value);
  if (Number.isNaN(dt.getTime())) return 'Unknown time';
  return dt.toLocaleString('en-US', {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}

async function loadRun(runId) {
  currentRunId = runId;
  setRunIdInUrl(runId);
  const [leads, previews] = await Promise.all([
    fetch('/api/results/' + runId).then(r => r.json()),
    fetch('/api/runs/' + runId + '/email-preview')
      .then(r => r.ok ? r.json() : [])
      .catch(() => []),
  ]);
  emailPreviews = {};
  (Array.isArray(previews) ? previews : []).forEach(p => { emailPreviews[p.lead_id] = p; });
  updateSendButton();
  populateResults(leads, runId);
  show('s-results');
}

function updateSendButton() {
  const run = allRuns.find(r => r.run_id === currentRunId);
  const btn = document.getElementById('send-instantly-btn');
  if (!btn) return;
  if (run && run.email_status === 'sent') {
    btn.textContent = 'Emails sent ✓';
    btn.disabled = true;
  } else {
    btn.textContent = 'Send to Instantly';
    btn.disabled = false;
  }
}

function populateResults(leads, runId) {
  allLeads = leads;
  currentFilter = 'all';
  visibleLeadCount = INITIAL_LEADS_PAGE;
  const counts = { qualified:0, review:0, disqualified:0 };
  leads.forEach(l => counts[l.decision]++);
  const total = leads.length;

  document.getElementById('cnt-qual').textContent  = counts.qualified;
  document.getElementById('cnt-rev').textContent   = counts.review;
  document.getElementById('cnt-dis').textContent   = counts.disqualified;
  document.getElementById('sum-qual').textContent  = counts.qualified;
  document.getElementById('sum-rev').textContent   = counts.review;
  document.getElementById('sum-dis').textContent   = counts.disqualified;
  ['cnt-all','sum-total'].forEach(id => document.getElementById(id).textContent = total);
  const runMeta = getRunMeta(runId);
  const metaBits = [
    total + ' leads',
    runMeta && runMeta.filename ? runMeta.filename : runId.slice(0,8),
    runMeta ? formatRunDateTime(runMeta.created_at) : new Date().toLocaleDateString('en-US', { month:'long', year:'numeric' }),
  ];
  document.getElementById('page-meta').textContent = metaBits.join(' · ');

  document.querySelectorAll('.filter-btn').forEach(b => b.classList.toggle('active', b.dataset.filter === 'all'));
  document.querySelector('.results-main').scrollTop = 0;
  renderLeads('all');
}

async function loadPastRuns() {
  const runs = await fetch('/api/runs').then(r => r.json());
  allRuns = runs;
  renderUploadRuns(runs);
  renderPastRuns(runs);
  renderRunsModal(runs);
}

function renderUploadRuns(runs) {
  const el = document.getElementById('upload-runs-list');
  const countEl = document.getElementById('upload-runs-count');
  countEl.textContent = runs.length ? `${runs.length} total` : 'No saved runs';
  if (!runs.length) {
    el.innerHTML = `<div style="font-size:11px;color:var(--text3);font-family:'DM Mono',monospace;padding:4px 6px;">No historical runs yet</div>`;
    return;
  }
  el.innerHTML = runs.map(r => {
    const ts = formatRunDateTime(r.created_at);
    const fname = (r.filename || 'Unnamed run').length > 28 ? (r.filename || 'Unnamed run').slice(0,25) + '…' : (r.filename || 'Unnamed run');
    const badges = [
      r.n_qualified    ? `<span class="run-micro-badge qualified">${r.n_qualified}Q</span>` : '',
      r.n_review       ? `<span class="run-micro-badge review">${r.n_review}R</span>` : '',
      r.n_disqualified ? `<span class="run-micro-badge disqualified">${r.n_disqualified}D</span>` : '',
      r.email_status   ? `<span class="email-status-badge ${r.email_status}">${r.email_status === 'not_sent' ? 'email not sent' : r.email_status === 'sent' ? 'emails sent' : 'partial'}</span>` : '',
    ].join('');
    return `
      <div class="run-item" onclick="loadRun('${r.run_id}')">
        <div class="run-item-name">
          <span class="run-status-dot ${r.status}"></span>${fname}
        </div>
        <div class="run-item-meta">${ts} · ${r.lead_count} leads</div>
        ${badges ? `<div class="run-item-badges">${badges}</div>` : ''}
      </div>`;
  }).join('');
}

function renderPastRuns(runs) {
  const seeAllBtn = document.getElementById('see-all-runs-btn');
  seeAllBtn.classList.toggle('hidden', runs.length <= 3);
  const el = document.getElementById('past-runs-list');
  if (!runs.length) {
    el.innerHTML = `<div style="font-size:11px;color:var(--text3);font-family:'DM Mono',monospace;padding:4px 6px;">No runs yet</div>`;
    return;
  }
  el.innerHTML = runs.slice(0, 3).map(r => {
    const isCurrent = r.run_id === currentRunId;
    const date = formatRunDateTime(r.created_at);
    const baseName = r.filename || 'Unnamed run';
    const fname = baseName.length > 22 ? baseName.slice(0,19) + '…' : baseName;
    const badges = [
      r.n_qualified    ? `<span class="run-micro-badge qualified">${r.n_qualified}Q</span>` : '',
      r.n_review       ? `<span class="run-micro-badge review">${r.n_review}R</span>` : '',
      r.n_disqualified ? `<span class="run-micro-badge disqualified">${r.n_disqualified}D</span>` : '',
      r.email_status   ? `<span class="email-status-badge ${r.email_status}">${r.email_status === 'not_sent' ? 'email not sent' : r.email_status === 'sent' ? 'emails sent' : 'partial'}</span>` : '',
    ].join('');
    return `
      <div class="run-item${isCurrent ? ' current' : ''}" onclick="loadRun('${r.run_id}')">
        <div class="run-item-name">
          <span class="run-status-dot ${r.status}"></span>${fname}
        </div>
        <div class="run-item-meta">${date} · ${r.lead_count} leads</div>
        ${badges ? `<div class="run-item-badges">${badges}</div>` : ''}
      </div>`;
  }).join('');
}

function renderRunsModal(runs) {
  const countEl = document.getElementById('runs-modal-count');
  const el = document.getElementById('runs-modal-list');
  countEl.textContent = runs.length ? `${runs.length} runs` : 'No runs';
  if (!runs.length) {
    el.innerHTML = `<div class="runs-modal-empty">No runs yet</div>`;
    return;
  }
  el.innerHTML = runs.map(r => {
    const isCurrent = r.run_id === currentRunId;
    const date = formatRunDateTime(r.created_at);
    const baseName = r.filename || 'Unnamed run';
    const fname = baseName.length > 34 ? baseName.slice(0,31) + '…' : baseName;
    const badges = [
      r.n_qualified    ? `<span class="run-micro-badge qualified">${r.n_qualified}Q</span>` : '',
      r.n_review       ? `<span class="run-micro-badge review">${r.n_review}R</span>` : '',
      r.n_disqualified ? `<span class="run-micro-badge disqualified">${r.n_disqualified}D</span>` : '',
    ].join('');
    return `
      <div class="run-item${isCurrent ? ' current' : ''}" onclick="selectRunFromModal('${r.run_id}')">
        <div class="run-item-name">
          <span class="run-status-dot ${r.status}"></span>${fname}
        </div>
        <div class="run-item-meta">${date} · ${r.lead_count} leads</div>
        ${badges ? `<div class="run-item-badges">${badges}</div>` : ''}
      </div>`;
  }).join('');
}

function openRunsModal() {
  document.getElementById('runs-modal').classList.remove('hidden');
}

function closeRunsModal() {
  document.getElementById('runs-modal').classList.add('hidden');
}

function selectRunFromModal(runId) {
  closeRunsModal();
  window.setTimeout(() => { loadRun(runId); }, 0);
}

// ── Filter ─────────────────────────────────────────────────────────────────────
function filterLeads(filter, btn) {
  currentFilter = filter;
  visibleLeadCount = INITIAL_LEADS_PAGE;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.toggle('active', b.dataset.filter === filter));
  document.querySelector('.results-main').scrollTop = 0;
  renderLeads(filter);
}

// ── Render leads ───────────────────────────────────────────────────────────────
function initials(name) { return name.split(' ').filter(Boolean).slice(0,2).map(n=>n[0]).join('').toUpperCase(); }
function scoreClass(d) { return d==='qualified'?'green':d==='review'?'amber':'red'; }
function formatSignalType(value) { return (value || '').replaceAll('_', ' ').replace(/\b\w/g, c => c.toUpperCase()) || 'Unknown source'; }
function normalizeWebsiteUrl(value) {
  if (!value) return '';
  return /^https?:\/\//i.test(value) ? value : `https://${value}`;
}

function getFilteredLeads(filter = currentFilter) {
  return filter === 'all' ? allLeads : allLeads.filter(l => l.decision === filter);
}

function updateLoadState(total, shown) {
  const el = document.getElementById('results-load-state');
  if (!total) {
    el.textContent = 'No leads to display';
    return;
  }
  if (shown < total) {
    el.textContent = `Showing ${shown} of ${total} leads · scroll for more`;
    return;
  }
  el.textContent = `Showing all ${total} leads`;
}

function maybeLoadMoreLeads() {
  const filtered = getFilteredLeads();
  if (visibleLeadCount >= filtered.length) return;
  const triggerIndex = Math.max(0, Math.min(visibleLeadCount - LEADS_PREFETCH_OFFSET, filtered.length - 1));
  const triggerRow = document.querySelector(`#leads .lead-row[data-render-index="${triggerIndex}"]`);
  const container = document.querySelector('.results-main');
  if (!triggerRow || !container) return;
  const triggerRect = triggerRow.getBoundingClientRect();
  const containerRect = container.getBoundingClientRect();
  if (triggerRect.top <= containerRect.bottom) {
    visibleLeadCount = Math.min(visibleLeadCount + LEADS_PAGE_SIZE, filtered.length);
    renderLeads(currentFilter);
  }
}

function renderLeads(filter) {
  currentFilter = filter;
  const filtered = getFilteredLeads(filter);
  visibleLeadCount = Math.max(Math.min(visibleLeadCount || INITIAL_LEADS_PAGE, filtered.length), Math.min(INITIAL_LEADS_PAGE, filtered.length));
  const visible = filtered.slice(0, visibleLeadCount);
  const el = document.getElementById('leads');
  el.classList.add('rerendering');
  requestAnimationFrame(() => {
    el.innerHTML = visible.map((l,i) => renderRow(l,i)).join('');
    el.classList.remove('rerendering');
    updateLoadState(filtered.length, visible.length);
    maybeLoadMoreLeads();
  });
}

function renderRow(l, i) {
  const [abg, afg] = PALETTES[i % PALETTES.length];
  const sc = scoreClass(l.decision);
  const companyMeta = [l.company, l.role].filter(Boolean).join(' · ');
  const companySizeMeta = l.company_size ? `${l.company_size} employees` : '';
  const industryMeta    = l.industry ? l.industry.replace(/_/g,' ').replace(/\b\w/g,c=>c.toUpperCase()) : '';
  const miniBars = ['segment','workflow','pain','buyer','commercial'].map(k => {
    const h = Math.max(3, Math.round(l[k]/MAX[k]*20));
    return `<div class="mini-bar" style="height:${h}px;background:${CLR[k]};opacity:0.75;"></div>`;
  }).join('');
  const bars = ['segment','buyer','pain','workflow','commercial'].map(k =>
    `<div class="bar-row">
      <span class="bar-label">${LBL[k]}</span>
      <div class="bar-track"><div class="bar-fill" style="width:${Math.round(l[k]/MAX[k]*100)}%;background:${CLR[k]};"></div></div>
      <span class="bar-score">${l[k]}/${MAX[k]}</span>
    </div>`).join('');
  const qHtml = (l.reasons||[]).map(r=>`<div class="reason-item pos">${r}</div>`).join('');
  const dHtml = (l.disreasons||[]).map(r=>`<div class="reason-item neg">${r}</div>`).join('');
  const sigBadge = l.signal_strength ? `<span class="signal-badge">signal: ${l.signal_strength}</span>` : '';
  const linkedinLink = l.linkedin_url ? `<a class="detail-link" href="${l.linkedin_url}" target="_blank" rel="noopener noreferrer">Open LinkedIn profile ↗</a>` : '';
  const companyWebsiteLink = l.company_website ? `<a class="detail-link" href="${normalizeWebsiteUrl(l.company_website)}" target="_blank" rel="noopener noreferrer">Open company website ↗</a>` : '';
  const sourceText = `<div class="detail-source">Generated from: ${formatSignalType(l.signal_type)}</div>`;
  const sourceLink = l.source_url ? `<a class="detail-link" href="${l.source_url}" target="_blank" rel="noopener noreferrer">Open source reference ↗</a>` : '';
  const referenceLinks = [linkedinLink, companyWebsiteLink, sourceText, sourceLink].filter(Boolean).join('');
  const detailMeta = [industryMeta, companySizeMeta].filter(Boolean).map(v => `<span class="meta-pill">${v}</span>`).join('');
  const evHtml = [
    ...(l.observed||[]).map(e=>`<div class="ev-item">${e}</div>`),
    ...(l.inferred||[]).map(e=>`<div class="ev-item inferred">${e}</div>`),
    ...(l.unknowns||[]).map(e=>`<div class="ev-item unknown">${e}</div>`),
    ...(l.hard_disqualifiers||[]).map(e=>`<div class="ev-item hard-dis">${e}</div>`),
  ].join('');
  const preview = emailPreviews[l.lead_id];
  const emailHtml = preview ? (() => {
    const manualTag = preview.is_manual
      ? `<span class="manual-badge">★ High value — send manually</span>`
      : preview.email_sent
        ? `<span class="email-sent-badge">✓ Sent to Instantly</span>`
        : '';
    const lid = JSON.stringify(l.lead_id).replace(/"/g,'&quot;');
    return `<div class="email-preview">
      <div class="email-preview-title">Email Preview</div>
      ${manualTag}
      <div class="email-subject">
        <span>Subject: <span>${preview.subject}</span></span>
        <button class="copy-btn" onclick="copyEmailField(${lid},'subject',this)">⧉ Copy subject</button>
      </div>
      <div class="email-body-wrap">
        <div class="email-body-header">
          <span class="email-body-label">Body</span>
          <button class="copy-btn" onclick="copyEmailField(${lid},'body',this)">⧉ Copy body</button>
        </div>
        <div class="email-body">${preview.body}</div>
      </div>
    </div>`;
  })() : '';

  const customerBadge = l.excluded ? '<span class="customer-badge">existing customer</span>' : '';
  return `
    <div class="lead-row${l.excluded ? ' excluded' : ''}" id="lr-${i}" data-render-index="${i}">
      <div class="lead-summary" onclick="toggle(${i})">
        <div class="avatar" style="background:${abg};color:${afg}">${initials(l.name)}</div>
        <div>
          <div class="lead-name">${l.name}${customerBadge}</div>
          <div class="lead-company">${companyMeta}</div>
          ${(industryMeta || companySizeMeta) ? `<div class="lead-company-extra">${[industryMeta, companySizeMeta].filter(Boolean).join(' · ')}</div>` : ''}
        </div>
        <div class="mini-bars">${miniBars}</div>
        <span class="decision-badge ${l.decision}">${l.decision}</span>
        <div class="score-cell">
          <span class="score-num ${sc}">${l.score}</span>
          ${l.manual_email_status === 'sent'     ? '<span class="manual-email-badge sent">✉ sent</span>'     : ''}
          ${l.manual_email_status === 'not_sent' ? '<span class="manual-email-badge not_sent">✉ not sent</span>' : ''}
        </div>
        <span class="chevron">▶</span>
      </div>
      <div class="lead-detail">
        <div class="detail-grid">
          <div>
            <div class="detail-section-title">Score breakdown <span class="conf-badge">${l.conf} confidence</span></div>
            ${detailMeta ? `<div class="detail-meta">${detailMeta}</div>` : ''}
            <div class="score-bars">${bars}</div>
            ${l.reasoning ? `<div class="use-case-box" style="margin-top:12px;"><div class="use-case-label">Reasoning</div>${l.reasoning}</div>` : ''}
            ${referenceLinks ? `<div class="detail-links">${referenceLinks}</div>` : ''}
            ${l.usecase ? `<div class="use-case-box"><div class="use-case-label">Suggested use case</div>${l.usecase}</div>` : ''}
          </div>
          <div>
            <div class="detail-section-title">${l.decision==='disqualified'?'Disqualification reasons':'Qualification signals'}</div>
            <div class="reason-list">${qHtml}${dHtml}</div>
            <div class="ev-section">${sigBadge}${evHtml}</div>
          </div>
        </div>
        ${emailHtml}
      </div>
    </div>`;
}

function toggle(i) { document.getElementById('lr-' + i).classList.toggle('open'); }

let _emailConfirmLeadId = null;

function copyEmailField(leadId, field, btn) {
  const preview = emailPreviews[leadId];
  const text = (preview && preview[field]) || '';
  const label = field === 'subject' ? '⧉ Copy subject' : '⧉ Copy body';
  const doFlash = () => {
    btn.textContent = '✓ Copied!';
    btn.classList.add('copied');
    setTimeout(() => { btn.textContent = label; btn.classList.remove('copied'); }, 1800);
    if (field === 'body') openEmailConfirmModal(leadId);
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(doFlash).catch(() => fallbackCopy(text, doFlash));
  } else {
    fallbackCopy(text, doFlash);
  }
}

function openEmailConfirmModal(leadId) {
  _emailConfirmLeadId = leadId;
  const lead = allLeads.find(l => l.lead_id === leadId);
  document.getElementById('email-confirm-name').textContent    = lead ? lead.name    : leadId;
  document.getElementById('email-confirm-company').textContent = lead ? lead.company : '';
  document.getElementById('email-confirm-modal').classList.remove('hidden');
}

async function confirmEmailStatus(status) {
  const leadId = _emailConfirmLeadId;
  document.getElementById('email-confirm-modal').classList.add('hidden');
  _emailConfirmLeadId = null;
  if (!leadId || !currentRunId) return;
  await fetch(`/api/runs/${currentRunId}/leads/${encodeURIComponent(leadId)}/email-status`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ status }),
  });
  const lead = allLeads.find(l => l.lead_id === leadId);
  if (lead) { lead.manual_email_status = status; renderLeads(currentFilter); }
}

function fallbackCopy(text, cb) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.cssText = 'position:fixed;top:0;left:0;width:1px;height:1px;opacity:0;';
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  try { document.execCommand('copy'); } catch(e) {}
  document.body.removeChild(ta);
  cb();
}

document.querySelector('.results-main').addEventListener('scroll', () => {
  if (!document.getElementById('s-results').classList.contains('active')) return;
  maybeLoadMoreLeads();
});

// ── Delete run ─────────────────────────────────────────────────────────────────
function confirmDelete() { document.getElementById('delete-modal').classList.remove('hidden'); }
function closeModal()    { document.getElementById('delete-modal').classList.add('hidden'); }

async function deleteCurrentRun() {
  closeModal();
  if (!currentRunId) return;
  await fetch('/api/runs/' + currentRunId, { method:'DELETE' });
  currentRunId = null;
  allLeads = [];
  show('s-upload');
  // Reset file input so user can re-upload same file
  document.getElementById('file-input').value = '';
  selectedFile = null;
  document.getElementById('submit-btn').disabled = true;
  document.getElementById('drop-hint').style.display = '';
  document.getElementById('file-info').classList.remove('show');
  clearRunIdFromUrl();
}

// ── Send to Instantly ──────────────────────────────────────────────────────────
async function openSendModal() {
  if (!currentRunId) return;
  document.getElementById('send-modal-body').textContent = 'Loading eligible leads...';
  document.getElementById('send-modal-confirm').disabled = true;
  document.getElementById('send-modal').classList.remove('hidden');

  const previews = Object.values(emailPreviews);
  const eligible  = previews.filter(p => !p.is_manual && !p.email_sent);
  const manual    = previews.filter(p => p.is_manual);

  if (!eligible.length) {
    document.getElementById('send-modal-body').textContent =
      manual.length
        ? `All ${manual.length} eligible leads are high-value (score > threshold) — send these manually.`
        : 'No eligible leads to send.';
    return;
  }

  document.getElementById('send-modal-body').innerHTML =
    `<strong>${eligible.length} lead${eligible.length > 1 ? 's' : ''}</strong> will be added to your Instantly campaign.` +
    (manual.length ? `<br><br><span style="color:var(--blue)">★ ${manual.length} high-value lead${manual.length > 1 ? 's' : ''} (score &gt; threshold) excluded — send these manually.</span>` : '');
  document.getElementById('send-modal-confirm').disabled = false;
}

function closeSendModal() {
  document.getElementById('send-modal').classList.add('hidden');
}

async function confirmSendEmails() {
  document.getElementById('send-modal-confirm').disabled = true;
  document.getElementById('send-modal-body').textContent = 'Sending...';
  try {
    const res = await fetch('/api/runs/' + currentRunId + '/send-emails', { method: 'POST' }).then(r => r.json());
    if (res.error) {
      document.getElementById('send-modal-body').textContent = 'Error: ' + res.error;
      return;
    }
    document.getElementById('send-modal-body').textContent = `✓ ${res.sent} lead${res.sent > 1 ? 's' : ''} sent to Instantly.`;
    await loadPastRuns();
    await loadRun(currentRunId);
  } catch(e) {
    document.getElementById('send-modal-body').textContent = 'Request failed. Check your Instantly config.';
  }
  setTimeout(closeSendModal, 2000);
}

// ── Settings ───────────────────────────────────────────────────────────────────

async function openSettings() {
  const [s, excl, tmpls] = await Promise.all([
    fetch('/api/settings').then(r => r.json()).catch(() => appSettings),
    fetch('/api/exclusions').then(r => r.json()).catch(() => []),
    fetch('/api/email-templates').then(r => r.json()).catch(() => []),
  ]);
  appSettings = s;
  exclusions  = excl;
  emailTemplates = tmpls;
  document.getElementById('input-qual-threshold').value = s.qualified_threshold;
  document.getElementById('input-dis-threshold').value  = s.disqualified_threshold;
  renderExclusions();
  renderTemplates();
  show('s-settings');
}

function exportRun() {
  if (!currentRunId) return;
  const a = document.createElement('a');
  a.href = '/api/runs/' + currentRunId + '/export';
  a.download = 'leads-' + currentRunId.slice(0, 8) + '.csv';
  a.click();
}

function backToResults() {
  if (currentRunId) { show('s-results'); } else { show('s-upload'); }
}

async function saveThresholds() {
  const qt  = parseInt(document.getElementById('input-qual-threshold').value, 10);
  const dt  = parseInt(document.getElementById('input-dis-threshold').value, 10);
  if (isNaN(qt) || isNaN(dt) || dt >= qt) {
    alert('Review threshold must be a number less than the qualified threshold.');
    return;
  }
  const resp = await fetch('/api/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ qualified_threshold: qt, disqualified_threshold: dt }),
  });
  if (!resp.ok) { alert((await resp.json()).error); return; }
  appSettings = { qualified_threshold: qt, disqualified_threshold: dt };
  const btn = document.getElementById('save-thresholds-btn');
  btn.textContent = 'Saved ✓';
  btn.classList.add('saved');
  setTimeout(() => { btn.textContent = 'Save thresholds'; btn.classList.remove('saved'); }, 2000);
  if (currentRunId) reclassifyAndRender();
}

function reclassifyAndRender() {
  const qt = appSettings.qualified_threshold;
  const dt = appSettings.disqualified_threshold;
  allLeads = allLeads.map(l => {
    const d = { ...l };
    if (d.hard_disqualifiers && d.hard_disqualifiers.length) {
      d.decision = 'disqualified';
    } else if (d.score >= qt) {
      d.decision = 'qualified';
    } else if (d.score < dt) {
      d.decision = 'disqualified';
    } else {
      d.decision = 'review';
    }
    return d;
  });
  populateResults(allLeads, currentRunId);
}

function renderExclusions() {
  const el = document.getElementById('exclusion-list');
  if (!exclusions.length) {
    el.innerHTML = '<div class="exclusion-empty">No exclusions yet.</div>';
    return;
  }
  el.innerHTML = exclusions.map(e =>
    `<div class="exclusion-item">
       <span>${e.value}</span>
       <button class="exclusion-del" onclick="removeExclusion(${e.id})" title="Remove">×</button>
     </div>`
  ).join('');
}

async function importExclusionCsv(input) {
  const file = input.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append('file', file);
  const btn = document.getElementById('import-csv-btn');
  btn.textContent = 'Importing…';
  btn.disabled = true;
  try {
    const resp = await fetch('/api/exclusions/import', { method: 'POST', body: fd });
    const data = await resp.json();
    if (resp.ok) {
      exclusions = await fetch('/api/exclusions').then(r => r.json());
      renderExclusions();
      btn.textContent = `✓ Imported ${data.imported}`;
      btn.classList.add('saved');
      setTimeout(() => { btn.textContent = 'Import CSV'; btn.classList.remove('saved'); }, 3000);
    }
  } finally {
    btn.disabled = false;
    input.value = '';
  }
}

async function addExclusion() {
  const input = document.getElementById('exclusion-input');
  const value = input.value.trim();
  if (!value) return;
  await fetch('/api/exclusions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ value }),
  });
  input.value = '';
  exclusions = await fetch('/api/exclusions').then(r => r.json());
  renderExclusions();
}

async function removeExclusion(id) {
  await fetch('/api/exclusions/' + id, { method: 'DELETE' });
  exclusions = exclusions.filter(e => e.id !== id);
  renderExclusions();
}

// ── Email Templates ────────────────────────────────────────────────────────────

const VERTICAL_LABELS = {
  ai_tools: 'AI Tools', film_production: 'Film Production',
  vfx_production: 'VFX Production', animation: 'Animation',
  short_drama: 'Short Drama', ecommerce: 'Ecommerce', ai_generic: 'AI Generic',
};

const VERTICAL_OPTIONS = ['ai_tools','film_production','vfx_production','animation','short_drama','ecommerce','ai_generic'];

function renderTemplates() {
  const list = document.getElementById('tmpl-list');
  if (!list) return;
  if (!emailTemplates.length) {
    list.innerHTML = '<div class="tmpl-empty">No templates yet.</div>';
    return;
  }
  list.innerHTML = emailTemplates.map(t => {
    const vOpts = VERTICAL_OPTIONS.map(v =>
      `<option value="${v}"${v===t.vertical?' selected':''}>${VERTICAL_LABELS[v]||v}</option>`
    ).join('');
    return `
    <div class="tmpl-card" id="tmpl-card-${t.id}">
      <div class="tmpl-card-top">
        <span class="tmpl-vertical-badge">${VERTICAL_LABELS[t.vertical] || t.vertical}</span>
        <div class="tmpl-info">
          <div class="tmpl-name">${t.name}</div>
          <div class="tmpl-subject">${t.subject}</div>
        </div>
        <div class="tmpl-actions">
          <button class="tmpl-btn" onclick="copyTmpl(${t.id}, this)" title="Copy subject + body">Copy</button>
          <button class="tmpl-btn" onclick="toggleEditTmpl(${t.id})" title="Edit template">Edit</button>
          <button class="tmpl-btn del" onclick="deleteTmpl(${t.id})" title="Delete template">Del</button>
        </div>
      </div>
      <div class="tmpl-edit-form" id="tmpl-edit-${t.id}">
        <div class="tmpl-form-row">
          <label class="tmpl-form-label">Vertical</label>
          <select class="tmpl-form-input" id="tmpl-edit-vertical-${t.id}">${vOpts}</select>
        </div>
        <div class="tmpl-form-row">
          <label class="tmpl-form-label">Template name</label>
          <input type="text" class="tmpl-form-input" id="tmpl-edit-name-${t.id}" value="${t.name.replace(/"/g,'&quot;')}">
        </div>
        <div class="tmpl-form-row">
          <label class="tmpl-form-label">Subject</label>
          <input type="text" class="tmpl-form-input" id="tmpl-edit-subject-${t.id}" value="${t.subject.replace(/"/g,'&quot;')}">
        </div>
        <div class="tmpl-form-row">
          <label class="tmpl-form-label">Body</label>
          <textarea class="tmpl-form-textarea" id="tmpl-edit-body-${t.id}">${t.body.replace(/</g,'&lt;')}</textarea>
        </div>
        <div class="tmpl-form-actions">
          <button class="settings-save-btn" onclick="updateTmpl(${t.id})">Save changes</button>
          <button class="settings-save-btn" onclick="toggleEditTmpl(${t.id})" style="color:var(--text3)">Cancel</button>
        </div>
      </div>
    </div>`;
  }).join('');
}

function toggleEditTmpl(id) {
  const form = document.getElementById('tmpl-edit-' + id);
  form.classList.toggle('open');
}

async function updateTmpl(id) {
  const vertical = document.getElementById('tmpl-edit-vertical-' + id).value;
  const name     = document.getElementById('tmpl-edit-name-' + id).value.trim();
  const subject  = document.getElementById('tmpl-edit-subject-' + id).value.trim();
  const body     = document.getElementById('tmpl-edit-body-' + id).value.trim();
  if (!name || !subject || !body) { alert('Name, subject, and body are required.'); return; }
  const resp = await fetch('/api/email-templates/' + id, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ vertical, name, subject, body }),
  });
  if (!resp.ok) { alert('Failed to update template.'); return; }
  const updated = await resp.json();
  const idx = emailTemplates.findIndex(t => t.id === id);
  if (idx !== -1) emailTemplates[idx] = updated;
  renderTemplates();
}

function copyTmpl(id, btn) {
  const t = emailTemplates.find(t => t.id === id);
  if (!t) return;
  const text = 'Subject: ' + t.subject + '\\n\\n' + t.body;
  const prev = btn.textContent;
  if (navigator.clipboard) {
    navigator.clipboard.writeText(text).then(() => { btn.textContent = '✓'; btn.classList.add('copied'); setTimeout(() => { btn.textContent = prev; btn.classList.remove('copied'); }, 1500); });
  } else {
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select(); document.execCommand('copy');
    document.body.removeChild(ta);
    btn.textContent = '✓'; btn.classList.add('copied'); setTimeout(() => { btn.textContent = prev; btn.classList.remove('copied'); }, 1500);
  }
}

function toggleTmplForm() {
  const form = document.getElementById('tmpl-add-form');
  const btn  = document.getElementById('tmpl-add-btn');
  const open = form.classList.toggle('open');
  btn.textContent = open ? '− Cancel' : '+ Add template';
  if (open) {
    document.getElementById('tmpl-name').value    = '';
    document.getElementById('tmpl-subject').value = '';
    document.getElementById('tmpl-body').value    = '';
  }
}

async function saveTmpl() {
  const vertical = document.getElementById('tmpl-vertical').value;
  const name     = document.getElementById('tmpl-name').value.trim();
  const subject  = document.getElementById('tmpl-subject').value.trim();
  const body     = document.getElementById('tmpl-body').value.trim();
  if (!name || !subject || !body) { alert('Name, subject, and body are required.'); return; }
  const resp = await fetch('/api/email-templates', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ vertical, name, subject, body }),
  });
  if (!resp.ok) { alert('Failed to save template.'); return; }
  const t = await resp.json();
  emailTemplates.push(t);
  renderTemplates();
  toggleTmplForm();
}

async function deleteTmpl(id) {
  if (!confirm('Delete this template?')) return;
  await fetch('/api/email-templates/' + id, { method: 'DELETE' });
  emailTemplates = emailTemplates.filter(t => t.id !== id);
  renderTemplates();
}

async function boot() {
  await loadPastRuns();
  const requestedRunId = getRunIdFromUrl();
  if (!requestedRunId) return;
  const requestedRun = allRuns.find(r => r.run_id === requestedRunId);
  if (requestedRun) {
    await loadRun(requestedRunId);
  }
}

boot();
</script>
</body>
</html>"""


@app.get("/")
async def index(request: Request):
    if not _is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse(HTML)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    uvicorn.run(app, host="0.0.0.0", port=8080, reload=False)
