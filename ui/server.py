"""
Seedance Lead Qualification Engine — Web UI
Run: python ui/server.py
Open: http://localhost:8080
"""

import asyncio
import csv
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

ROOT = Path(__file__).parent.parent
OUTPUT_DIR = ROOT / "output"
ARTIFACTS_DIR = OUTPUT_DIR / "artifacts"

app = FastAPI()
jobs: dict = {}


def build_results() -> list:
    leads = []
    for fname, decision in [
        ("qualified_leads.csv", "qualified"),
        ("review_leads.csv", "review"),
        ("disqualified_leads.csv", "disqualified"),
    ]:
        path = OUTPUT_DIR / fname
        if not path.exists():
            continue
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                lid = row.get("lead_id", "")
                art, ev = {}, {}
                art_p = ARTIFACTS_DIR / f"{lid}_decision.json"
                ev_p = ARTIFACTS_DIR / f"{lid}_evidence.json"
                if art_p.exists():
                    art = json.loads(art_p.read_text())
                if ev_p.exists():
                    ev = json.loads(ev_p.read_text())
                leads.append({
                    "name": row.get("person_name", ""),
                    "company": row.get("company_name", ""),
                    "role": row.get("title", ""),
                    "decision": decision,
                    "score": int(float(row.get("score", 0))),
                    "conf": row.get("confidence", ""),
                    "segment": art.get("segment_fit", 0),
                    "workflow": art.get("workflow_fit", 0),
                    "pain": art.get("pain_fit", 0),
                    "buyer": art.get("buyer_fit", 0),
                    "commercial": art.get("commercial_fit", 0),
                    "reasons": art.get("qualification_reasons", []),
                    "disreasons": art.get("disqualification_reasons", []),
                    "usecase": art.get("suggested_use_case") or row.get("use_case", ""),
                    "signal_strength": ev.get("signal_strength", ""),
                    "observed": ev.get("observed_evidence", []),
                    "inferred": ev.get("inferred_evidence", []),
                    "unknowns": ev.get("unknowns", []),
                    "hard_disqualifiers": ev.get("hard_disqualifiers", []),
                    "reasoning": art.get("reasoning_summary") or row.get("reasoning_summary", ""),
                })
    leads.sort(key=lambda x: x["score"], reverse=True)
    return leads


@app.get("/api/results")
async def get_results():
    return JSONResponse(build_results())


@app.post("/api/run")
async def run_pipeline(file: UploadFile = File(...), concurrency: int = Form(10)):
    job_id = str(uuid.uuid4())
    content = await file.read()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".csv")
    tmp.write(content)
    tmp.close()
    jobs[job_id] = {"lines": [], "status": "running", "returncode": None}
    asyncio.create_task(run_job(job_id, tmp.name, concurrency))
    return JSONResponse({"job_id": job_id})


async def run_job(job_id: str, csv_path: str, concurrency: int):
    cmd = [
        sys.executable, str(ROOT / "main.py"),
        "--input", csv_path,
        "--output-dir", str(OUTPUT_DIR),
        "--concurrency", str(max(5, min(20, concurrency))),
        "--verbose",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(ROOT),
        )
        async for line in proc.stdout:
            jobs[job_id]["lines"].append(line.decode().rstrip())
        await proc.wait()
        jobs[job_id]["returncode"] = proc.returncode
        jobs[job_id]["status"] = "done" if proc.returncode == 0 else "error"
    except Exception as exc:
        jobs[job_id]["lines"].append(f"ERROR: {exc}")
        jobs[job_id]["status"] = "error"
        jobs[job_id]["returncode"] = 1
    finally:
        try:
            os.unlink(csv_path)
        except Exception:
            pass


@app.get("/api/progress/{job_id}")
async def progress(job_id: str):
    if job_id not in jobs:
        return JSONResponse({"error": "not found"}, status_code=404)

    async def stream():
        sent = 0
        while True:
            job = jobs[job_id]
            while sent < len(job["lines"]):
                yield f"data: {json.dumps({'line': job['lines'][sent]})}\n\n"
                sent += 1
            if job["status"] != "running":
                yield f"data: {json.dumps({'done': True, 'returncode': job['returncode']})}\n\n"
                break
            await asyncio.sleep(0.15)

    return StreamingResponse(stream(), media_type="text/event-stream")


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
body { background: var(--bg); color: var(--text); font-family: 'Syne', sans-serif; min-height: 100vh; line-height: 1.5; }
.screen { display: none; }
.screen.active { display: block; }

/* ── Upload screen ── */
.upload-shell {
  min-height: 100vh; display: flex; flex-direction: column;
  align-items: center; justify-content: center; padding: 40px 20px; gap: 32px;
}
.upload-logo { display: flex; align-items: center; gap: 10px; }
.logo-mark {
  width: 32px; height: 32px; background: var(--accent); border-radius: 8px;
  display: flex; align-items: center; justify-content: center;
}
.logo-mark svg { width: 16px; height: 16px; }
.logo-text { font-size: 15px; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase; }
.logo-sub { font-size: 11px; color: var(--text3); letter-spacing: 0.04em; }
.upload-card {
  width: 100%; max-width: 480px;
  background: var(--bg2); border: 1px solid var(--border);
  border-radius: 16px; padding: 32px;
  display: flex; flex-direction: column; gap: 24px;
}
.upload-card-header h2 { font-size: 20px; font-weight: 700; margin-bottom: 4px; }
.upload-card-header p { font-size: 13px; color: var(--text3); font-family: 'DM Mono', monospace; }
.dropzone {
  border: 1px dashed var(--border2); border-radius: 10px;
  padding: 32px 20px; cursor: pointer; transition: all 0.15s;
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 10px; min-height: 120px; text-align: center;
  background: var(--bg3);
}
.dropzone:hover, .dropzone.drag-over {
  border-color: var(--accent); background: rgba(200,255,87,0.04);
}
.dropzone svg { color: var(--text3); }
.dropzone.drag-over svg { color: var(--accent); }
.drop-hint-text { font-size: 14px; color: var(--text2); }
.drop-hint-sub { font-size: 11px; color: var(--text3); font-family: 'DM Mono', monospace; }
.file-info { display: none; flex-direction: column; align-items: center; gap: 6px; }
.file-info.visible { display: flex; }
.file-icon { color: var(--accent); }
.file-name-text { font-size: 13px; font-weight: 600; color: var(--text); }
.file-size-text { font-size: 11px; color: var(--text3); font-family: 'DM Mono', monospace; }
.upload-options {
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 14px; background: var(--bg3); border-radius: 8px;
  border: 1px solid var(--border);
}
.option-label { font-size: 12px; color: var(--text2); display: flex; align-items: center; gap: 8px; }
.option-label span { font-family: 'DM Mono', monospace; font-size: 11px; color: var(--text3); }
.conc-controls { display: flex; align-items: center; gap: 10px; }
.conc-val { font-family: 'DM Mono', monospace; font-size: 14px; font-weight: 600; color: var(--accent); min-width: 24px; text-align: center; }
.conc-btn {
  width: 24px; height: 24px; border-radius: 6px; border: 1px solid var(--border2);
  background: var(--bg2); color: var(--text2); cursor: pointer; font-size: 14px;
  display: flex; align-items: center; justify-content: center; transition: all 0.1s; line-height: 1;
}
.conc-btn:hover { background: var(--bg3); color: var(--text); border-color: var(--border2); }
.submit-btn {
  width: 100%; padding: 14px; border-radius: 10px; border: none;
  background: var(--accent); color: #0d0e0f; font-family: 'Syne', sans-serif;
  font-size: 14px; font-weight: 700; cursor: pointer; transition: all 0.15s;
  letter-spacing: 0.02em;
}
.submit-btn:hover:not(:disabled) { background: #d4ff3a; transform: translateY(-1px); }
.submit-btn:disabled { opacity: 0.35; cursor: not-allowed; transform: none; }
.view-existing-btn {
  background: none; border: none; color: var(--text3); font-family: 'Syne', sans-serif;
  font-size: 12px; cursor: pointer; padding: 8px; transition: color 0.15s;
  font-family: 'DM Mono', monospace; letter-spacing: 0.03em;
}
.view-existing-btn:hover { color: var(--text2); }

/* ── Progress screen ── */
.progress-shell {
  min-height: 100vh; max-width: 800px; margin: 0 auto;
  padding: 48px 32px; display: flex; flex-direction: column; gap: 32px;
}
.progress-logo { display: flex; align-items: center; gap: 10px; }
.progress-header { display: flex; flex-direction: column; gap: 20px; }
.progress-title-row { display: flex; align-items: center; gap: 14px; }
.progress-title { font-size: 22px; font-weight: 700; }
.progress-spinner {
  width: 20px; height: 20px; border: 2px solid var(--border2);
  border-top-color: var(--accent); border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
.progress-title-row.done .progress-spinner { display: none; }
.progress-counters {
  display: flex; gap: 20px; flex-wrap: wrap;
}
.pcounter {
  display: flex; flex-direction: column; gap: 2px;
  padding: 12px 18px; border-radius: 8px; background: var(--bg2);
  border: 1px solid var(--border); min-width: 90px;
}
.pcount { font-size: 28px; font-weight: 700; font-family: 'DM Mono', monospace; line-height: 1; }
.pcounter.green .pcount { color: var(--green); }
.pcounter.amber .pcount { color: var(--amber); }
.pcounter.red .pcount { color: var(--red); }
.pcounter.grey .pcount { color: var(--text2); }
.plabel { font-size: 11px; color: var(--text3); font-family: 'DM Mono', monospace; letter-spacing: 0.05em; text-transform: uppercase; }
.log-terminal {
  flex: 1; background: var(--bg2); border: 1px solid var(--border);
  border-radius: 10px; padding: 16px; overflow-y: auto;
  font-family: 'DM Mono', monospace; font-size: 11.5px; line-height: 1.7;
  max-height: 500px; min-height: 300px;
}
.log-line { color: var(--text3); }
.log-line.log-lead-qual { color: var(--green); }
.log-line.log-lead-rev { color: var(--amber); }
.log-line.log-lead-dis { color: var(--text3); }
.log-line.log-error { color: var(--red); }
.log-line.log-info { color: var(--text2); }
.progress-error { color: var(--red); font-family: 'DM Mono', monospace; font-size: 13px; margin-top: 8px; display: none; }

/* ── Results shell ── */
.results-shell { display: grid; grid-template-columns: 220px 1fr; min-height: 100vh; }
.sidebar {
  background: var(--bg2); border-right: 1px solid var(--border);
  padding: 28px 20px; position: sticky; top: 0; height: 100vh;
  overflow-y: auto; display: flex; flex-direction: column; gap: 32px;
}
.sidebar-logo { display: flex; align-items: center; gap: 10px; }
.sidebar-logo .logo-text { font-size: 13px; }
.sidebar-logo .logo-sub { font-size: 10px; }
.sidebar-label {
  font-family: 'DM Mono', monospace; font-size: 10px; letter-spacing: 0.1em;
  text-transform: uppercase; color: var(--text3); margin-bottom: 10px;
}
.filter-group { display: flex; flex-direction: column; gap: 4px; }
.filter-btn {
  display: flex; align-items: center; justify-content: space-between;
  padding: 7px 10px; border-radius: 6px; border: none;
  background: transparent; color: var(--text2); font-family: 'Syne', sans-serif;
  font-size: 13px; cursor: pointer; transition: all 0.15s; text-align: left; width: 100%;
}
.filter-btn:hover { background: var(--bg3); color: var(--text); }
.filter-btn.active { background: var(--bg3); color: var(--text); }
.filter-btn .count {
  font-family: 'DM Mono', monospace; font-size: 11px;
  padding: 1px 6px; border-radius: 4px; background: var(--bg); color: var(--text3);
}
.filter-btn.active .count { color: var(--text2); }
.sidebar-dims { display: flex; flex-direction: column; gap: 8px; }
.dim-row { display: flex; justify-content: space-between; align-items: center; }
.dim-dot-label { display: flex; align-items: center; gap: 6px; font-size: 11px; color: var(--text3); font-family: 'DM Mono', monospace; }
.dim-dot { width: 8px; height: 8px; border-radius: 2px; display: inline-block; }
.dim-max { font-size: 11px; color: var(--text3); font-family: 'DM Mono', monospace; }
.stats-block { display: flex; flex-direction: column; gap: 10px; margin-top: auto; }
.stat-row { display: flex; justify-content: space-between; align-items: center; }
.stat-label { font-size: 11px; color: var(--text3); font-family: 'DM Mono', monospace; }
.stat-val { font-size: 13px; font-weight: 600; }
.run-new-btn {
  display: block; width: 100%; padding: 8px 12px; border-radius: 6px; border: 1px solid var(--border2);
  background: transparent; color: var(--text3); font-family: 'DM Mono', monospace; font-size: 11px;
  cursor: pointer; transition: all 0.15s; text-align: center; letter-spacing: 0.04em;
}
.run-new-btn:hover { background: var(--bg3); color: var(--text2); }

/* ── Results main ── */
.results-main { padding: 32px 36px; overflow-y: auto; }
.page-header { margin-bottom: 28px; }
.page-title { font-size: 24px; font-weight: 700; letter-spacing: -0.02em; margin-bottom: 4px; }
.page-meta { font-size: 13px; color: var(--text3); font-family: 'DM Mono', monospace; }
.summary-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 28px; }
.scard { background: var(--bg2); border: 1px solid var(--border); border-radius: 10px; padding: 16px 18px; transition: border-color 0.15s; }
.scard:hover { border-color: var(--border2); }
.scard-label { font-size: 11px; color: var(--text3); font-family: 'DM Mono', monospace; letter-spacing: 0.05em; margin-bottom: 8px; }
.scard-val { font-size: 28px; font-weight: 700; line-height: 1; }
.scard-val.green { color: var(--green); }
.scard-val.amber { color: var(--amber); }
.scard-val.red { color: var(--red); }

/* ── Lead rows ── */
.leads-grid { display: flex; flex-direction: column; gap: 8px; }
.lead-row {
  background: var(--bg2); border: 1px solid var(--border); border-radius: 10px;
  overflow: hidden; transition: border-color 0.15s;
  animation: fadeIn 0.2s ease both;
}
@keyframes fadeIn { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; } }
.lead-row:hover { border-color: var(--border2); }
.lead-summary {
  display: grid; grid-template-columns: 36px 1fr 100px 80px 90px 30px;
  align-items: center; gap: 16px; padding: 14px 18px; cursor: pointer; user-select: none;
}
.avatar { width: 36px; height: 36px; border-radius: 8px; display: flex; align-items: center; justify-content: center; font-size: 12px; font-weight: 600; flex-shrink: 0; }
.lead-name { font-size: 14px; font-weight: 600; color: var(--text); }
.lead-company { font-size: 12px; color: var(--text3); margin-top: 1px; font-family: 'DM Mono', monospace; }
.mini-bars { display: flex; align-items: flex-end; gap: 3px; height: 20px; }
.mini-bar { width: 10px; border-radius: 2px; }
.decision-badge { font-size: 10px; font-weight: 600; letter-spacing: 0.07em; text-transform: uppercase; padding: 3px 8px; border-radius: 4px; font-family: 'DM Mono', monospace; }
.decision-badge.qualified { background: var(--green-bg); color: var(--green); }
.decision-badge.review { background: var(--amber-bg); color: var(--amber); }
.decision-badge.disqualified { background: var(--red-bg); color: var(--red); }
.score-num { font-size: 22px; font-weight: 700; font-family: 'DM Mono', monospace; line-height: 1; }
.score-num.green { color: var(--green); }
.score-num.amber { color: var(--amber); }
.score-num.red { color: var(--red); }
.chevron { color: var(--text3); font-size: 12px; transition: transform 0.2s; display: flex; align-items: center; justify-content: center; }
.lead-row.open .chevron { transform: rotate(90deg); }
.lead-detail { display: none; padding: 0 18px 18px; border-top: 1px solid var(--border); }
.lead-row.open .lead-detail { display: block; }
.detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; padding-top: 18px; }
.detail-section-title { font-size: 10px; font-weight: 600; letter-spacing: 0.1em; text-transform: uppercase; color: var(--text3); font-family: 'DM Mono', monospace; margin-bottom: 12px; }
.score-bars { display: flex; flex-direction: column; gap: 10px; }
.bar-row { display: flex; align-items: center; gap: 10px; }
.bar-label { font-size: 12px; color: var(--text2); width: 108px; flex-shrink: 0; }
.bar-track { flex: 1; height: 4px; background: var(--bg3); border-radius: 2px; overflow: hidden; }
.bar-fill { height: 100%; border-radius: 2px; }
.bar-score { font-size: 11px; font-family: 'DM Mono', monospace; color: var(--text3); min-width: 36px; text-align: right; }
.reason-list { display: flex; flex-direction: column; gap: 8px; }
.reason-item { font-size: 12px; color: var(--text2); line-height: 1.55; padding-left: 14px; position: relative; }
.reason-item::before { content: ''; position: absolute; left: 0; top: 7px; width: 5px; height: 5px; border-radius: 50%; }
.reason-item.pos::before { background: var(--green); }
.reason-item.neg::before { background: var(--red); }
.use-case-box { margin-top: 14px; padding: 10px 12px; background: var(--bg3); border-radius: 6px; border-left: 2px solid var(--border2); font-size: 12px; color: var(--text2); line-height: 1.55; }
.use-case-label { font-size: 10px; font-family: 'DM Mono', monospace; color: var(--text3); margin-bottom: 4px; letter-spacing: 0.05em; text-transform: uppercase; }
.conf-badge { font-size: 10px; font-family: 'DM Mono', monospace; padding: 2px 7px; border-radius: 3px; background: var(--bg3); color: var(--text3); }
.evidence-section { margin-top: 16px; }
.evidence-section .detail-section-title { margin-bottom: 8px; }
.ev-item { font-size: 12px; color: var(--text2); line-height: 1.55; padding: 3px 0 3px 14px; position: relative; }
.ev-item::before { content: ''; position: absolute; left: 0; top: 9px; width: 4px; height: 4px; border-radius: 50%; background: var(--text3); }
.ev-item.inferred { color: var(--text3); }
.ev-item.inferred::before { background: var(--blue); opacity: 0.5; }
.ev-item.unknown::before { background: var(--amber); opacity: 0.4; }
.ev-item.hard-dis::before { background: var(--red); }
.signal-badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 10px; font-family: 'DM Mono', monospace; background: var(--bg3); color: var(--text3); margin-bottom: 10px; }
</style>
</head>
<body>

<!-- ── Screen 1: Upload ── -->
<section id="s-upload" class="screen active">
  <div class="upload-shell">
    <div class="upload-logo">
      <div class="logo-mark">
        <svg viewBox="0 0 14 14" fill="none"><rect x="1" y="1" width="5" height="5" rx="1" fill="#0d0e0f"/><rect x="8" y="1" width="5" height="5" rx="1" fill="#0d0e0f"/><rect x="1" y="8" width="5" height="5" rx="1" fill="#0d0e0f"/><rect x="8" y="8" width="2" height="5" rx="1" fill="#0d0e0f"/></svg>
      </div>
      <div>
        <div class="logo-text">Seedance</div>
        <div class="logo-sub">Lead Pipeline</div>
      </div>
    </div>

    <div class="upload-card">
      <div class="upload-card-header">
        <h2>Upload leads</h2>
        <p>Trigify CSV export · stages 1 &amp; 2 will run automatically</p>
      </div>

      <div id="dropzone" class="dropzone">
        <div id="drop-hint" style="display:flex;flex-direction:column;align-items:center;gap:10px;">
          <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
          <div class="drop-hint-text">Drop CSV here or click to browse</div>
          <div class="drop-hint-sub">.csv · Trigify format</div>
        </div>
        <div id="file-info" class="file-info">
          <svg class="file-icon" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
          <div id="file-name-text" class="file-name-text">—</div>
          <div id="file-size-text" class="file-size-text">—</div>
        </div>
      </div>
      <input type="file" id="file-input" accept=".csv" style="display:none">

      <div class="upload-options">
        <div class="option-label">Concurrency <span>parallel leads</span></div>
        <div class="conc-controls">
          <button class="conc-btn" id="conc-dec">−</button>
          <div class="conc-val" id="conc-display">10</div>
          <button class="conc-btn" id="conc-inc">+</button>
        </div>
      </div>

      <button id="submit-btn" class="submit-btn" disabled onclick="startRun()">
        Run Pipeline →
      </button>
    </div>

    <button class="view-existing-btn" onclick="viewExisting()">
      View existing results →
    </button>
  </div>
</section>

<!-- ── Screen 2: Progress ── -->
<section id="s-progress" class="screen">
  <div class="progress-shell">
    <div class="progress-logo">
      <div class="logo-mark">
        <svg viewBox="0 0 14 14" fill="none"><rect x="1" y="1" width="5" height="5" rx="1" fill="#0d0e0f"/><rect x="8" y="1" width="5" height="5" rx="1" fill="#0d0e0f"/><rect x="1" y="8" width="5" height="5" rx="1" fill="#0d0e0f"/><rect x="8" y="8" width="2" height="5" rx="1" fill="#0d0e0f"/></svg>
      </div>
      <div>
        <div class="logo-text">Seedance</div>
        <div class="logo-sub">Lead Pipeline</div>
      </div>
    </div>

    <div class="progress-header">
      <div class="progress-title-row" id="prog-title-row">
        <div class="progress-spinner"></div>
        <div class="progress-title" id="prog-title">Processing leads…</div>
      </div>
      <div class="progress-counters">
        <div class="pcounter grey">
          <div class="pcount" id="prog-total">0</div>
          <div class="plabel">Processed</div>
        </div>
        <div class="pcounter green">
          <div class="pcount" id="prog-qual">0</div>
          <div class="plabel">Qualified</div>
        </div>
        <div class="pcounter amber">
          <div class="pcount" id="prog-rev">0</div>
          <div class="plabel">Review</div>
        </div>
        <div class="pcounter red">
          <div class="pcount" id="prog-dis">0</div>
          <div class="plabel">Disqualified</div>
        </div>
      </div>
    </div>

    <div id="log-output" class="log-terminal"></div>
    <div id="progress-error" class="progress-error"></div>
  </div>
</section>

<!-- ── Screen 3: Results ── -->
<section id="s-results" class="screen">
  <div class="results-shell">
    <aside class="sidebar">
      <div class="sidebar-logo">
        <div class="logo-mark">
          <svg viewBox="0 0 14 14" fill="none"><rect x="1" y="1" width="5" height="5" rx="1" fill="#0d0e0f"/><rect x="8" y="1" width="5" height="5" rx="1" fill="#0d0e0f"/><rect x="1" y="8" width="5" height="5" rx="1" fill="#0d0e0f"/><rect x="8" y="8" width="2" height="5" rx="1" fill="#0d0e0f"/></svg>
        </div>
        <div>
          <div class="logo-text">Seedance</div>
          <div class="logo-sub">Lead Pipeline</div>
        </div>
      </div>

      <div>
        <div class="sidebar-label">Filter</div>
        <div class="filter-group">
          <button class="filter-btn active" data-filter="all" onclick="filterLeads('all',this)">All leads <span class="count" id="cnt-all">0</span></button>
          <button class="filter-btn" data-filter="qualified" onclick="filterLeads('qualified',this)">Qualified <span class="count" id="cnt-qual">0</span></button>
          <button class="filter-btn" data-filter="review" onclick="filterLeads('review',this)">For review <span class="count" id="cnt-rev">0</span></button>
          <button class="filter-btn" data-filter="disqualified" onclick="filterLeads('disqualified',this)">Disqualified <span class="count" id="cnt-dis">0</span></button>
        </div>
      </div>

      <div>
        <div class="sidebar-label">Score dims</div>
        <div class="sidebar-dims">
          <div class="dim-row"><span class="dim-dot-label"><span class="dim-dot" style="background:#60a5fa"></span>Segment</span><span class="dim-max">/30</span></div>
          <div class="dim-row"><span class="dim-dot-label"><span class="dim-dot" style="background:#c084fc"></span>Buyer</span><span class="dim-max">/25</span></div>
          <div class="dim-row"><span class="dim-dot-label"><span class="dim-dot" style="background:#fb923c"></span>Pain</span><span class="dim-max">/20</span></div>
          <div class="dim-row"><span class="dim-dot-label"><span class="dim-dot" style="background:#4ade80"></span>Workflow</span><span class="dim-max">/15</span></div>
          <div class="dim-row"><span class="dim-dot-label"><span class="dim-dot" style="background:#fbbf24"></span>Commercial</span><span class="dim-max">/10</span></div>
        </div>
      </div>

      <div class="stats-block">
        <div class="sidebar-label">This run</div>
        <div class="stat-row"><span class="stat-label">Total</span><span class="stat-val" id="stat-total">—</span></div>
        <div class="stat-row"><span class="stat-label">Qualified</span><span class="stat-val" style="color:var(--green)" id="stat-qual">—</span></div>
        <div class="stat-row"><span class="stat-label">Review</span><span class="stat-val" style="color:var(--amber)" id="stat-rev">—</span></div>
        <div class="stat-row"><span class="stat-label">Disqualified</span><span class="stat-val" style="color:var(--red)" id="stat-dis">—</span></div>
      </div>

      <button class="run-new-btn" onclick="show('s-upload')">+ New run</button>
    </aside>

    <main class="results-main">
      <div class="page-header">
        <div class="page-title">Lead Qualification</div>
        <div class="page-meta" id="page-meta">Loading…</div>
      </div>
      <div class="summary-row">
        <div class="scard"><div class="scard-label">Total leads</div><div class="scard-val" id="sum-total">—</div></div>
        <div class="scard"><div class="scard-label">Qualified</div><div class="scard-val green" id="sum-qual">—</div></div>
        <div class="scard"><div class="scard-label">For review</div><div class="scard-val amber" id="sum-rev">—</div></div>
        <div class="scard"><div class="scard-label">Disqualified</div><div class="scard-val red" id="sum-dis">—</div></div>
      </div>
      <div class="leads-grid" id="leads"></div>
    </main>
  </div>
</section>

<script>
// ── State ──
let allLeads = [];
let currentFilter = 'all';
let selectedFile = null;
let concurrency = 10;

const MAX_SCORES = { segment: 30, workflow: 15, pain: 20, buyer: 25, commercial: 10 };
const BAR_COLORS = { segment: '#60a5fa', workflow: '#4ade80', pain: '#fb923c', buyer: '#c084fc', commercial: '#fbbf24' };
const DIM_LABELS = { segment: 'Segment fit', workflow: 'Workflow fit', pain: 'Pain fit', buyer: 'Buyer fit', commercial: 'Commercial fit' };
const AVATAR_PALETTES = [
  ['#1a3a2a','#4ade80'],['#1a2a3a','#60a5fa'],['#2a1a3a','#c084fc'],
  ['#3a2a1a','#fbbf24'],['#3a1a1a','#f87171'],['#1a3a38','#2dd4bf'],
];

// ── Screens ──
function show(id) {
  document.querySelectorAll('.screen').forEach(s => s.classList.remove('active'));
  document.getElementById(id).classList.add('active');
}

// ── Upload ──
const dropzone = document.getElementById('dropzone');
const fileInput = document.getElementById('file-input');

dropzone.addEventListener('click', () => fileInput.click());
dropzone.addEventListener('dragover', e => { e.preventDefault(); dropzone.classList.add('drag-over'); });
dropzone.addEventListener('dragleave', () => dropzone.classList.remove('drag-over'));
dropzone.addEventListener('drop', e => {
  e.preventDefault(); dropzone.classList.remove('drag-over');
  if (e.dataTransfer.files[0]) setFile(e.dataTransfer.files[0]);
});
fileInput.addEventListener('change', e => { if (e.target.files[0]) setFile(e.target.files[0]); });

function setFile(file) {
  selectedFile = file;
  document.getElementById('drop-hint').style.display = 'none';
  const fi = document.getElementById('file-info');
  fi.classList.add('visible');
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
  show('s-progress');
  resetProgress();
  const res = await fetch('/api/run', { method: 'POST', body: fd });
  const { job_id } = await res.json();
  connectSSE(job_id);
}

async function viewExisting() {
  const data = await fetch('/api/results').then(r => r.json());
  if (!data.length) { alert('No results found. Run the pipeline first.'); return; }
  loadResults(data);
}

// ── Progress ──
const progStats = { qualified: 0, review: 0, disqualified: 0, total: 0 };

function resetProgress() {
  Object.assign(progStats, { qualified: 0, review: 0, disqualified: 0, total: 0 });
  ['prog-total','prog-qual','prog-rev','prog-dis'].forEach(id => document.getElementById(id).textContent = '0');
  document.getElementById('log-output').innerHTML = '';
  document.getElementById('progress-error').style.display = 'none';
  const tr = document.getElementById('prog-title-row');
  tr.classList.remove('done');
  document.getElementById('prog-title').textContent = 'Processing leads…';
  const sp = tr.querySelector('.progress-spinner');
  if (sp) sp.style.display = '';
}

function connectSSE(jobId) {
  const es = new EventSource('/api/progress/' + jobId);
  es.onmessage = e => {
    const data = JSON.parse(e.data);
    if (data.done) {
      es.close();
      const tr = document.getElementById('prog-title-row');
      tr.classList.add('done');
      const sp = tr.querySelector('.progress-spinner');
      if (sp) sp.style.display = 'none';
      if (data.returncode === 0) {
        document.getElementById('prog-title').textContent = 'Done — loading results…';
        fetch('/api/results').then(r => r.json()).then(loadResults);
      } else {
        document.getElementById('prog-title').textContent = 'Pipeline failed';
        document.getElementById('prog-title').style.color = 'var(--red)';
        const err = document.getElementById('progress-error');
        err.textContent = 'Pipeline exited with error. Check logs above.';
        err.style.display = 'block';
      }
    } else {
      appendLog(data.line);
      parseLogLine(data.line);
    }
  };
  es.onerror = () => es.close();
}

function appendLog(line) {
  const el = document.getElementById('log-output');
  const div = document.createElement('div');
  div.className = 'log-line';
  if (line.includes('✓') && line.includes('| QUALIFIED')) div.classList.add('log-lead-qual');
  else if (line.includes('✓') && line.includes('| REVIEW')) div.classList.add('log-lead-rev');
  else if (line.includes('✓') && line.includes('| DISQUALIFIED')) div.classList.add('log-lead-dis');
  else if (line.includes('ERROR') || line.includes('✗')) div.classList.add('log-error');
  else if (line.includes('| INFO')) div.classList.add('log-info');
  div.textContent = line;
  el.appendChild(div);
  el.scrollTop = el.scrollHeight;
}

function parseLogLine(line) {
  if (!line.includes('✓')) return;
  if (line.includes('| QUALIFIED')) progStats.qualified++;
  else if (line.includes('| REVIEW')) progStats.review++;
  else if (line.includes('| DISQUALIFIED')) progStats.disqualified++;
  else return;
  progStats.total = progStats.qualified + progStats.review + progStats.disqualified;
  document.getElementById('prog-total').textContent = progStats.total;
  document.getElementById('prog-qual').textContent = progStats.qualified;
  document.getElementById('prog-rev').textContent = progStats.review;
  document.getElementById('prog-dis').textContent = progStats.disqualified;
}

// ── Results ──
function loadResults(data) {
  allLeads = data;
  const counts = { qualified: 0, review: 0, disqualified: 0 };
  data.forEach(l => counts[l.decision]++);
  const total = data.length;

  document.getElementById('cnt-all').textContent = total;
  document.getElementById('cnt-qual').textContent = counts.qualified;
  document.getElementById('cnt-rev').textContent = counts.review;
  document.getElementById('cnt-dis').textContent = counts.disqualified;
  document.getElementById('stat-total').textContent = total;
  document.getElementById('stat-qual').textContent = counts.qualified;
  document.getElementById('stat-rev').textContent = counts.review;
  document.getElementById('stat-dis').textContent = counts.disqualified;
  document.getElementById('sum-total').textContent = total;
  document.getElementById('sum-qual').textContent = counts.qualified;
  document.getElementById('sum-rev').textContent = counts.review;
  document.getElementById('sum-dis').textContent = counts.disqualified;
  document.getElementById('page-meta').textContent = total + ' leads · Seedance video AI · ' + new Date().toLocaleDateString('en-US', { month: 'long', year: 'numeric' });

  show('s-results');
  renderLeads('all');
}

function filterLeads(filter, btn) {
  currentFilter = filter;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.toggle('active', b.dataset.filter === filter));
  renderLeads(filter);
}

function initials(name) {
  return name.split(' ').filter(Boolean).slice(0,2).map(n => n[0]).join('').toUpperCase();
}

function scoreClass(d) { return d === 'qualified' ? 'green' : d === 'review' ? 'amber' : 'red'; }

function miniBarHtml(l) {
  return ['segment','workflow','pain','buyer','commercial'].map(k => {
    const pct = Math.round(l[k] / MAX_SCORES[k] * 100);
    const h = Math.max(3, Math.round(pct * 20 / 100));
    return `<div class="mini-bar" style="height:${h}px;background:${BAR_COLORS[k]};opacity:0.75;"></div>`;
  }).join('');
}

function renderLeads(filter) {
  const filtered = filter === 'all' ? allLeads : allLeads.filter(l => l.decision === filter);
  const el = document.getElementById('leads');
  el.innerHTML = filtered.map((l, i) => renderLeadRow(l, i)).join('');
}

function renderLeadRow(l, i) {
  const [abg, afg] = AVATAR_PALETTES[i % AVATAR_PALETTES.length];
  const sc = scoreClass(l.decision);

  const bars = ['segment','buyer','pain','workflow','commercial'].map(k => `
    <div class="bar-row">
      <span class="bar-label">${DIM_LABELS[k]}</span>
      <div class="bar-track"><div class="bar-fill" style="width:${Math.round(l[k]/MAX_SCORES[k]*100)}%;background:${BAR_COLORS[k]};"></div></div>
      <span class="bar-score">${l[k]}/${MAX_SCORES[k]}</span>
    </div>`).join('');

  const confBadge = `<span class="conf-badge">${l.conf} confidence</span>`;

  // Reasons column
  const qHtml = (l.reasons||[]).map(r => `<div class="reason-item pos">${r}</div>`).join('');
  const dHtml = (l.disreasons||[]).map(r => `<div class="reason-item neg">${r}</div>`).join('');
  const reasonsTitle = l.decision === 'disqualified' ? 'Disqualification reasons' : 'Qualification signals';
  const usecaseHtml = l.usecase ? `<div class="use-case-box"><div class="use-case-label">Suggested use case</div>${l.usecase}</div>` : '';

  // Evidence tab
  const sigBadge = l.signal_strength ? `<span class="signal-badge">signal: ${l.signal_strength}</span>` : '';
  const obsHtml = (l.observed||[]).map(e => `<div class="ev-item">${e}</div>`).join('');
  const infHtml = (l.inferred||[]).map(e => `<div class="ev-item inferred">${e}</div>`).join('');
  const unkHtml = (l.unknowns||[]).map(e => `<div class="ev-item unknown">${e}</div>`).join('');
  const hdHtml  = (l.hard_disqualifiers||[]).map(e => `<div class="ev-item hard-dis">${e}</div>`).join('');

  const evidenceHtml = `
    ${sigBadge}
    ${obsHtml ? '<div class="detail-section-title" style="margin-top:10px;">Observed</div>' + obsHtml : ''}
    ${infHtml ? '<div class="detail-section-title" style="margin-top:10px;">Inferred</div>' + infHtml : ''}
    ${unkHtml ? '<div class="detail-section-title" style="margin-top:10px;">Unknowns</div>' + unkHtml : ''}
    ${hdHtml  ? '<div class="detail-section-title" style="margin-top:10px;color:var(--red);">Hard disqualifiers</div>' + hdHtml : ''}
  `;

  return `<div class="lead-row" id="lr-${i}">
    <div class="lead-summary" onclick="toggle(${i})">
      <div class="avatar" style="background:${abg};color:${afg}">${initials(l.name)}</div>
      <div>
        <div class="lead-name">${l.name}</div>
        <div class="lead-company">${l.company} · ${l.role}</div>
      </div>
      <div class="mini-bars">${miniBarHtml(l)}</div>
      <span class="decision-badge ${l.decision}">${l.decision}</span>
      <span class="score-num ${sc}">${l.score}</span>
      <span class="chevron">▶</span>
    </div>
    <div class="lead-detail">
      <div class="detail-grid">
        <div>
          <div class="detail-section-title">Score breakdown ${confBadge}</div>
          <div class="score-bars">${bars}</div>
          ${l.reasoning ? `<div class="use-case-box" style="margin-top:14px;"><div class="use-case-label">Reasoning</div>${l.reasoning}</div>` : ''}
        </div>
        <div>
          <div class="detail-section-title">${reasonsTitle}</div>
          <div class="reason-list">${qHtml}${dHtml}</div>
          ${usecaseHtml}
          <div class="evidence-section">${evidenceHtml}</div>
        </div>
      </div>
    </div>
  </div>`;
}

function toggle(i) {
  document.getElementById('lr-' + i).classList.toggle('open');
}
</script>
</body>
</html>"""


@app.get("/")
async def index():
    return HTMLResponse(HTML)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080, reload=False)
