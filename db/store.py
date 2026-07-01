"""
PostgreSQL persistence layer.
Uses asyncpg with a connection pool. JSONB columns are auto-serialised via
a per-connection codec registered in create_pool().
"""

import json
import re
from typing import Optional

import asyncpg


# ── Schema ────────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS campaigns (
    id          TEXT        PRIMARY KEY,
    name        TEXT        NOT NULL,
    description TEXT,
    is_active   BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT         PRIMARY KEY,
    campaign_id  TEXT         REFERENCES campaigns(id),
    filename     TEXT         NOT NULL,
    concurrency  INT          NOT NULL,
    status       TEXT         NOT NULL DEFAULT 'running',
    returncode   INT,
    lead_count   INT          NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS leads (
    id              SERIAL  PRIMARY KEY,
    run_id          TEXT    NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    lead_id         TEXT    NOT NULL,
    person_name     TEXT,
    company         TEXT,
    company_size    TEXT,
    linkedin_url    TEXT,
    company_website TEXT,
    title           TEXT,
    signal_type     TEXT,
    source_url      TEXT,
    decision        TEXT    NOT NULL,
    score           INT     NOT NULL DEFAULT 0,
    confidence      TEXT,
    segment_fit     INT     NOT NULL DEFAULT 0,
    buyer_fit       INT     NOT NULL DEFAULT 0,
    pain_fit        INT     NOT NULL DEFAULT 0,
    workflow_fit    INT     NOT NULL DEFAULT 0,
    commercial_fit  INT     NOT NULL DEFAULT 0,
    reasoning       TEXT,
    use_case        TEXT,
    signal_strength TEXT,
    evidence        JSONB   NOT NULL DEFAULT '{}',
    qual_reasons    JSONB   NOT NULL DEFAULT '[]',
    disqual_reasons JSONB   NOT NULL DEFAULT '[]',
    UNIQUE (run_id, lead_id)
);

CREATE INDEX IF NOT EXISTS idx_leads_run     ON leads(run_id);
CREATE INDEX IF NOT EXISTS idx_leads_run_dec ON leads(run_id, decision);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT        PRIMARY KEY,
    value      TEXT        NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Campaign-scoped settings (used for multi-campaign threshold management)
CREATE TABLE IF NOT EXISTS campaign_settings (
    campaign_id TEXT        NOT NULL REFERENCES campaigns(id),
    key         TEXT        NOT NULL,
    value       TEXT        NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (campaign_id, key)
);

CREATE TABLE IF NOT EXISTS exclusions (
    id          SERIAL      PRIMARY KEY,
    campaign_id TEXT        REFERENCES campaigns(id),
    value       TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (campaign_id, value)
);

CREATE TABLE IF NOT EXISTS email_templates (
    id          SERIAL      PRIMARY KEY,
    campaign_id TEXT        REFERENCES campaigns(id),
    vertical    TEXT        NOT NULL,
    name        TEXT        NOT NULL,
    subject     TEXT        NOT NULL,
    body        TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Saved Instantly outreach campaigns (label + external campaign id)
CREATE TABLE IF NOT EXISTS instantly_campaigns (
    id            SERIAL      PRIMARY KEY,
    label         TEXT        NOT NULL,
    campaign_id   TEXT        NOT NULL UNIQUE,
    meeting_note  TEXT        NOT NULL DEFAULT '',
    pain_point    TEXT        NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Vertical → Instantly campaign routing map (global)
CREATE TABLE IF NOT EXISTS instantly_routing (
    vertical      TEXT        PRIMARY KEY,
    campaign_id   TEXT        NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Accumulated meeting notes per campaign (one row per pasted note)
CREATE TABLE IF NOT EXISTS campaign_notes (
    id            SERIAL      PRIMARY KEY,
    campaign_id   TEXT        NOT NULL,
    note_text     TEXT        NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_campaign_notes_campaign ON campaign_notes(campaign_id);

CREATE TABLE IF NOT EXISTS skill_files (
    id          SERIAL      PRIMARY KEY,
    campaign_id TEXT        NOT NULL,
    filename    TEXT        NOT NULL,
    content     TEXT        NOT NULL DEFAULT '',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (campaign_id, filename)
);
CREATE INDEX IF NOT EXISTS idx_skill_files_campaign ON skill_files(campaign_id);

CREATE TABLE IF NOT EXISTS company_profiles (
    id                        BIGSERIAL PRIMARY KEY,
    canonical_domain          TEXT,
    company_name              TEXT        NOT NULL,
    normalized_company_name   TEXT        NOT NULL,
    latest_structured_profile JSONB       NOT NULL DEFAULT '{}',
    latest_summary            TEXT        NOT NULL DEFAULT '',
    latest_fit_by_campaign    JSONB       NOT NULL DEFAULT '{}',
    first_seen_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    times_seen                INT         NOT NULL DEFAULT 1,
    last_run_id               TEXT        REFERENCES runs(run_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_company_profiles_domain
    ON company_profiles(canonical_domain)
    WHERE canonical_domain IS NOT NULL AND canonical_domain <> '';
CREATE INDEX IF NOT EXISTS idx_company_profiles_norm_name
    ON company_profiles(normalized_company_name);

CREATE TABLE IF NOT EXISTS person_profiles (
    id                       BIGSERIAL PRIMARY KEY,
    person_name              TEXT        NOT NULL,
    normalized_person_name   TEXT        NOT NULL,
    linkedin_url             TEXT,
    company_profile_id       BIGINT      REFERENCES company_profiles(id) ON DELETE SET NULL,
    latest_title             TEXT        NOT NULL DEFAULT '',
    latest_summary           TEXT        NOT NULL DEFAULT '',
    latest_fit_by_campaign   JSONB       NOT NULL DEFAULT '{}',
    first_seen_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    times_seen               INT         NOT NULL DEFAULT 1,
    last_run_id              TEXT        REFERENCES runs(run_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_person_profiles_linkedin
    ON person_profiles(linkedin_url)
    WHERE linkedin_url IS NOT NULL AND linkedin_url <> '';
CREATE INDEX IF NOT EXISTS idx_person_profiles_norm_name
    ON person_profiles(normalized_person_name);

CREATE TABLE IF NOT EXISTS lead_memory (
    id                   BIGSERIAL PRIMARY KEY,
    run_id               TEXT        NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    lead_id              TEXT        NOT NULL,
    campaign_id          TEXT        NOT NULL REFERENCES campaigns(id),
    campaign_version     TEXT        NOT NULL DEFAULT 'v1',
    policy_version       TEXT        NOT NULL DEFAULT 'v1',
    company_profile_id   BIGINT      REFERENCES company_profiles(id) ON DELETE SET NULL,
    person_profile_id    BIGINT      REFERENCES person_profiles(id) ON DELETE SET NULL,
    raw_input            JSONB       NOT NULL DEFAULT '{}',
    enrichment_snapshot  JSONB       NOT NULL DEFAULT '{}',
    evidence_snapshot    JSONB       NOT NULL DEFAULT '{}',
    decision_snapshot    JSONB       NOT NULL DEFAULT '{}',
    routing              TEXT        NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_lead_memory_run_id ON lead_memory(run_id);
CREATE INDEX IF NOT EXISTS idx_lead_memory_campaign_id ON lead_memory(campaign_id);
CREATE INDEX IF NOT EXISTS idx_lead_memory_company_profile_id ON lead_memory(company_profile_id);
CREATE INDEX IF NOT EXISTS idx_lead_memory_person_profile_id ON lead_memory(person_profile_id);
CREATE INDEX IF NOT EXISTS idx_lead_memory_lead_id ON lead_memory(lead_id);

CREATE TABLE IF NOT EXISTS review_feedback (
    id                  BIGSERIAL PRIMARY KEY,
    run_id              TEXT        REFERENCES runs(run_id) ON DELETE CASCADE,
    lead_id             TEXT        NOT NULL,
    campaign_id         TEXT        NOT NULL REFERENCES campaigns(id),
    company_profile_id  BIGINT      REFERENCES company_profiles(id) ON DELETE SET NULL,
    person_profile_id   BIGINT      REFERENCES person_profiles(id) ON DELETE SET NULL,
    feedback_type       TEXT        NOT NULL,
    feedback_reason     TEXT        NOT NULL DEFAULT '',
    corrected_decision  TEXT,
    fit_outcome         TEXT,
    owner               TEXT        NOT NULL DEFAULT '',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_review_feedback_campaign_id ON review_feedback(campaign_id);
CREATE INDEX IF NOT EXISTS idx_review_feedback_run_id ON review_feedback(run_id);
CREATE INDEX IF NOT EXISTS idx_review_feedback_lead_id ON review_feedback(lead_id);

CREATE TABLE IF NOT EXISTS campaign_versions (
    id                   BIGSERIAL PRIMARY KEY,
    campaign_id          TEXT        NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    version              TEXT        NOT NULL,
    status               TEXT        NOT NULL DEFAULT 'draft',
    config_json          TEXT        NOT NULL DEFAULT '',
    product_md           TEXT        NOT NULL DEFAULT '',
    icp_md               TEXT        NOT NULL DEFAULT '',
    signals_md           TEXT        NOT NULL DEFAULT '',
    disqualification_md  TEXT        NOT NULL DEFAULT '',
    qualification_md     TEXT        NOT NULL DEFAULT '',
    created_by           TEXT        NOT NULL DEFAULT '',
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    approved_by          TEXT,
    approved_at          TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_campaign_versions_unique
    ON campaign_versions(campaign_id, version);

CREATE TABLE IF NOT EXISTS policy_versions (
    id                     BIGSERIAL PRIMARY KEY,
    campaign_id            TEXT        NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    policy_version         TEXT        NOT NULL,
    score_weights          JSONB       NOT NULL DEFAULT '{}',
    qualified_threshold    INT         NOT NULL,
    disqualified_threshold INT         NOT NULL,
    routing_rules          JSONB       NOT NULL DEFAULT '{}',
    status                 TEXT        NOT NULL DEFAULT 'draft',
    created_by             TEXT        NOT NULL DEFAULT '',
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    approved_by            TEXT,
    approved_at            TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_policy_versions_unique
    ON policy_versions(campaign_id, policy_version);
"""


# ── Pool ──────────────────────────────────────────────────────────────────────

async def create_pool(database_url: str) -> asyncpg.Pool:
    async def _init(conn: asyncpg.Connection) -> None:
        await conn.set_type_codec(
            "jsonb",
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )
    return await asyncpg.create_pool(database_url, init=_init, min_size=2, max_size=10)


# ── Bootstrap ─────────────────────────────────────────────────────────────────

async def create_tables(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(_DDL)
        await conn.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS campaign_version TEXT DEFAULT 'v1'")
        await conn.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS policy_version TEXT DEFAULT 'v1'")
        await conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS company_size TEXT")
        await conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS industry TEXT")
        await conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS linkedin_url TEXT")
        await conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS company_website TEXT")
        await conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS signal_type TEXT")
        await conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS source_url TEXT")
        # Known email on person profiles — persists manual entries across runs
        await conn.execute("ALTER TABLE person_profiles ADD COLUMN IF NOT EXISTS known_email TEXT")
        # Email columns
        await conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS email TEXT")
        await conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS email_sent BOOLEAN DEFAULT FALSE")
        await conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS email_sent_at TIMESTAMPTZ")
        await conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS manual_email_status TEXT")
        # Multi-campaign routing: which Instantly campaign each lead is destined for
        await conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS instantly_campaign_id TEXT")
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS instantly_campaigns (
                   id SERIAL PRIMARY KEY, label TEXT NOT NULL,
                   campaign_id TEXT NOT NULL UNIQUE,
                   created_at TIMESTAMPTZ NOT NULL DEFAULT now())"""
        )
        # Per-campaign meeting note + extracted pain point (for {{pain_point}})
        await conn.execute("ALTER TABLE instantly_campaigns ADD COLUMN IF NOT EXISTS meeting_note TEXT NOT NULL DEFAULT ''")
        await conn.execute("ALTER TABLE instantly_campaigns ADD COLUMN IF NOT EXISTS pain_point TEXT NOT NULL DEFAULT ''")
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS instantly_routing (
                   vertical TEXT PRIMARY KEY, campaign_id TEXT NOT NULL,
                   updated_at TIMESTAMPTZ NOT NULL DEFAULT now())"""
        )
        # Accumulated meeting notes per campaign (batch for pain-point synthesis)
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS campaign_notes (
                   id SERIAL PRIMARY KEY, campaign_id TEXT NOT NULL,
                   note_text TEXT NOT NULL,
                   created_at TIMESTAMPTZ NOT NULL DEFAULT now())"""
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_campaign_notes_campaign ON campaign_notes(campaign_id)"
        )
        # Email status on runs
        await conn.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS email_status TEXT DEFAULT 'not_sent'")
        await conn.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS apollo_enabled BOOLEAN DEFAULT FALSE")
        # Campaign support columns (idempotent migration)
        await conn.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS campaign_id TEXT REFERENCES campaigns(id)")
        await conn.execute("ALTER TABLE exclusions ADD COLUMN IF NOT EXISTS campaign_id TEXT REFERENCES campaigns(id)")
        await conn.execute("ALTER TABLE email_templates ADD COLUMN IF NOT EXISTS campaign_id TEXT REFERENCES campaigns(id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_campaign ON runs(campaign_id)")
        # Seed known campaigns before backfilling FKs — order matters
        for cid, name, desc in [
            ("seedance", "Seedance",  "ByteDance AI video generation platform"),
            ("kling",    "Kling AI",   "Kuaishou cinematic AI video generation platform"),
        ]:
            await conn.execute(
                "INSERT INTO campaigns (id, name, description) VALUES ($1,$2,$3) ON CONFLICT (id) DO NOTHING",
                cid, name, desc,
            )
        # Backfill existing rows that pre-date campaign support
        await conn.execute("UPDATE runs SET campaign_id = 'seedance' WHERE campaign_id IS NULL")
        await conn.execute("UPDATE exclusions SET campaign_id = 'seedance' WHERE campaign_id IS NULL")
        await conn.execute("UPDATE email_templates SET campaign_id = 'seedance' WHERE campaign_id IS NULL")


async def mark_stale_runs(pool: asyncpg.Pool) -> None:
    """Runs that were 'running' when the server last stopped never completed."""
    await pool.execute("UPDATE runs SET status='error' WHERE status='running'")


# ── Runs ──────────────────────────────────────────────────────────────────────

async def seed_campaigns(pool: asyncpg.Pool) -> None:
    """Ensure campaign registry rows exist for all known campaigns."""
    known = [
        ("seedance", "Seedance",  "ByteDance AI video generation platform"),
        ("kling",    "Kling AI",   "Kuaishou cinematic AI video generation platform"),
    ]
    for cid, name, desc in known:
        await pool.execute(
            """INSERT INTO campaigns (id, name, description)
               VALUES ($1, $2, $3)
               ON CONFLICT (id) DO NOTHING""",
            cid, name, desc,
        )


async def list_campaigns(pool: asyncpg.Pool) -> list:
    rows = await pool.fetch(
        "SELECT id, name, description, is_active FROM campaigns ORDER BY id"
    )
    return [{"id": r["id"], "name": r["name"], "description": r["description"], "is_active": r["is_active"]} for r in rows]


async def insert_run(
    pool: asyncpg.Pool,
    run_id: str,
    filename: str,
    concurrency: int,
    campaign_id: str = "seedance",
    campaign_version: str = "v1",
    policy_version: str = "v1",
    apollo_enabled: bool = False,
) -> None:
    await pool.execute(
        """INSERT INTO runs (run_id, campaign_id, filename, concurrency, campaign_version, policy_version, apollo_enabled)
           VALUES ($1, $2, $3, $4, $5, $6, $7)""",
        run_id, campaign_id, filename, concurrency, campaign_version, policy_version, apollo_enabled,
    )


async def update_run(
    pool: asyncpg.Pool,
    run_id: str,
    status: str,
    returncode: Optional[int] = None,
    lead_count: int = 0,
) -> None:
    if status in ("done", "error"):
        await pool.execute(
            """UPDATE runs
                  SET status=$1, returncode=$2, lead_count=$3, completed_at=now()
                WHERE run_id=$4""",
            status, returncode, lead_count, run_id,
        )
    else:
        await pool.execute(
            "UPDATE runs SET status=$1 WHERE run_id=$2", status, run_id
        )


async def list_runs(pool: asyncpg.Pool, campaign_id: Optional[str] = None) -> list:
    if campaign_id:
        rows = await pool.fetch(
            """
            SELECT r.run_id, r.campaign_id, r.filename, r.status, r.lead_count,
                   r.created_at, r.completed_at, r.email_status, r.apollo_enabled,
                   r.campaign_version, r.policy_version,
                   COUNT(l.id) AS n_total,
                   COUNT(CASE WHEN l.decision='qualified'    THEN 1 END) AS n_qualified,
                   COUNT(CASE WHEN l.decision='review'       THEN 1 END) AS n_review,
                   COUNT(CASE WHEN l.decision='disqualified' THEN 1 END) AS n_disqualified
              FROM runs r
              LEFT JOIN leads l ON l.run_id = r.run_id
             WHERE r.campaign_id = $1
             GROUP BY r.run_id
             ORDER BY r.created_at DESC
            """,
            campaign_id,
        )
    else:
        rows = await pool.fetch(
            """
            SELECT r.run_id, r.campaign_id, r.filename, r.status, r.lead_count,
                   r.created_at, r.completed_at, r.email_status, r.apollo_enabled,
                   r.campaign_version, r.policy_version,
                   COUNT(l.id) AS n_total,
                   COUNT(CASE WHEN l.decision='qualified'    THEN 1 END) AS n_qualified,
                   COUNT(CASE WHEN l.decision='review'       THEN 1 END) AS n_review,
                   COUNT(CASE WHEN l.decision='disqualified' THEN 1 END) AS n_disqualified
              FROM runs r
              LEFT JOIN leads l ON l.run_id = r.run_id
             GROUP BY r.run_id
             ORDER BY r.created_at DESC
            """,
        )
    return [
        {
            "run_id":         r["run_id"],
            "campaign_id":    r["campaign_id"] or "seedance",
            "campaign_version": r["campaign_version"] or "v1",
            "policy_version": r["policy_version"] or "v1",
            "filename":       r["filename"],
            "status":         r["status"],
            "lead_count":     max(r["lead_count"] or 0, r["n_total"] or 0),
            "n_qualified":    r["n_qualified"],
            "n_review":       r["n_review"],
            "n_disqualified": r["n_disqualified"],
            "created_at":     r["created_at"].isoformat() if r["created_at"] else None,
            "email_status":   r["email_status"] or "not_sent",
            "apollo_enabled": bool(r["apollo_enabled"]),
        }
        for r in rows
    ]


async def get_run(pool: asyncpg.Pool, run_id: str) -> Optional[dict]:
    row = await pool.fetchrow(
        """
        SELECT r.run_id, r.campaign_id, r.filename, r.status, r.lead_count,
               r.created_at, r.completed_at, r.email_status, r.apollo_enabled,
               r.campaign_version, r.policy_version,
               COUNT(l.id) AS n_total,
               COUNT(CASE WHEN l.decision='qualified'    THEN 1 END) AS n_qualified,
               COUNT(CASE WHEN l.decision='review'       THEN 1 END) AS n_review,
               COUNT(CASE WHEN l.decision='disqualified' THEN 1 END) AS n_disqualified
          FROM runs r
          LEFT JOIN leads l ON l.run_id = r.run_id
         WHERE r.run_id = $1
         GROUP BY r.run_id
        """,
        run_id,
    )
    if not row:
        return None
    return {
        "run_id": row["run_id"],
        "campaign_id": row["campaign_id"] or "seedance",
        "campaign_version": row["campaign_version"] or "v1",
        "policy_version": row["policy_version"] or "v1",
        "filename": row["filename"],
        "status": row["status"],
        "lead_count": max(row["lead_count"] or 0, row["n_total"] or 0),
        "n_qualified": row["n_qualified"],
        "n_review": row["n_review"],
        "n_disqualified": row["n_disqualified"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "completed_at": row["completed_at"].isoformat() if row["completed_at"] else None,
        "email_status": row["email_status"] or "not_sent",
        "apollo_enabled": bool(row["apollo_enabled"]),
    }


async def delete_run(pool: asyncpg.Pool, run_id: str) -> None:
    await pool.execute("DELETE FROM runs WHERE run_id=$1", run_id)


# ── Leads ─────────────────────────────────────────────────────────────────────

async def upsert_lead(pool: asyncpg.Pool, run_id: str, lead: dict) -> None:
    evidence = {
        "observed":          lead.get("observed", []),
        "inferred":          lead.get("inferred", []),
        "unknowns":          lead.get("unknowns", []),
        "hard_disqualifiers": lead.get("hard_disqualifiers", []),
    }
    await pool.execute(
        """
        INSERT INTO leads (
            run_id, lead_id, person_name, company, company_size, industry, linkedin_url, company_website, title,
            signal_type, source_url, decision, score, confidence,
            segment_fit, buyer_fit, pain_fit, workflow_fit, commercial_fit,
            reasoning, use_case, signal_strength,
            evidence, qual_reasons, disqual_reasons, email
        ) VALUES (
            $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23,$24,$25,$26
        )
        ON CONFLICT (run_id, lead_id) DO UPDATE SET
            company_size=$5, industry=$6, linkedin_url=$7, company_website=$8, signal_type=$10, source_url=$11,
            decision=$12, score=$13, confidence=$14,
            segment_fit=$15, buyer_fit=$16, pain_fit=$17,
            workflow_fit=$18, commercial_fit=$19,
            reasoning=$20, use_case=$21, signal_strength=$22,
            evidence=$23, qual_reasons=$24, disqual_reasons=$25, email=$26
        """,
        run_id,
        lead.get("lead_id", lead.get("name", "")),
        lead.get("name", ""),
        lead.get("company", ""),
        lead.get("company_size", ""),
        lead.get("industry", ""),
        lead.get("linkedin_url", ""),
        lead.get("company_website", ""),
        lead.get("role", ""),
        lead.get("signal_type", ""),
        lead.get("source_url", ""),
        lead["decision"],
        lead["score"],
        lead.get("conf", ""),
        lead.get("segment", 0),
        lead.get("buyer", 0),
        lead.get("pain", 0),
        lead.get("workflow", 0),
        lead.get("commercial", 0),
        lead.get("reasoning", ""),
        lead.get("usecase", ""),
        lead.get("signal_strength", ""),
        evidence,
        lead.get("reasons", []),
        lead.get("disreasons", []),
        lead.get("email") or None,
    )


async def get_leads(pool: asyncpg.Pool, run_id: str) -> list:
    rows = await pool.fetch(
        "SELECT * FROM leads WHERE run_id=$1 ORDER BY score DESC", run_id
    )
    return [_row_to_lead(r) for r in rows]


def _row_to_lead(row: asyncpg.Record) -> dict:
    ev = row["evidence"] or {}
    return {
        "lead_id":        row["lead_id"],
        "name":           row["person_name"] or "",
        "company":        row["company"] or "",
        "company_size":   row["company_size"] or "",
        "industry":       row["industry"] or "",
        "linkedin_url":   row["linkedin_url"] or "",
        "company_website": row["company_website"] or "",
        "role":           row["title"] or "",
        "signal_type":    row["signal_type"] or "",
        "source_url":     row["source_url"] or "",
        "decision":       row["decision"],
        "score":          row["score"],
        "conf":           row["confidence"] or "",
        "segment":        row["segment_fit"],
        "buyer":          row["buyer_fit"],
        "pain":           row["pain_fit"],
        "workflow":       row["workflow_fit"],
        "commercial":     row["commercial_fit"],
        "reasoning":      row["reasoning"] or "",
        "usecase":        row["use_case"] or "",
        "signal_strength": row["signal_strength"] or "",
        "observed":       ev.get("observed", []),
        "inferred":       ev.get("inferred", []),
        "unknowns":       ev.get("unknowns", []),
        "hard_disqualifiers": ev.get("hard_disqualifiers", []),
        "reasons":        row["qual_reasons"] or [],
        "disreasons":     row["disqual_reasons"] or [],
        "email":                row["email"] or "",
        "email_sent":           row["email_sent"] or False,
        "manual_email_status":  row["manual_email_status"] or None,
        "instantly_campaign_id": (row["instantly_campaign_id"]
                                  if "instantly_campaign_id" in row.keys() else None),
    }


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _normalize_domain(domain: str) -> str:
    value = _normalize_text(domain).lower()
    value = re.sub(r"^https?://", "", value)
    value = re.sub(r"^www\.", "", value)
    return value.rstrip("/")


def normalize_company_key(company_name: str, domain: str = "") -> dict:
    clean_name = _normalize_text(company_name)
    normalized_company_name = re.sub(r"[^a-z0-9]+", " ", clean_name.lower()).strip()
    canonical_domain = _normalize_domain(domain)
    return {
        "company_name": clean_name,
        "normalized_company_name": normalized_company_name,
        "canonical_domain": canonical_domain or None,
    }


def normalize_person_key(person_name: str, linkedin_url: str = "") -> dict:
    clean_name = _normalize_text(person_name)
    normalized_person_name = re.sub(r"[^a-z0-9]+", " ", clean_name.lower()).strip()
    normalized_linkedin = _normalize_text(linkedin_url)
    if normalized_linkedin and not normalized_linkedin.startswith("http"):
        normalized_linkedin = f"https://{normalized_linkedin}"
    return {
        "person_name": clean_name,
        "normalized_person_name": normalized_person_name,
        "linkedin_url": normalized_linkedin or None,
    }


def _row_to_company_profile(row: asyncpg.Record) -> dict:
    return {
        "id": row["id"],
        "canonical_domain": row["canonical_domain"] or "",
        "company_name": row["company_name"],
        "normalized_company_name": row["normalized_company_name"],
        "latest_structured_profile": row["latest_structured_profile"] or {},
        "latest_summary": row["latest_summary"] or "",
        "latest_fit_by_campaign": row["latest_fit_by_campaign"] or {},
        "first_seen_at": row["first_seen_at"].isoformat() if row["first_seen_at"] else None,
        "last_seen_at": row["last_seen_at"].isoformat() if row["last_seen_at"] else None,
        "times_seen": row["times_seen"] or 0,
        "last_run_id": row["last_run_id"],
    }


def _row_to_person_profile(row: asyncpg.Record) -> dict:
    return {
        "id": row["id"],
        "person_name": row["person_name"],
        "normalized_person_name": row["normalized_person_name"],
        "linkedin_url": row["linkedin_url"] or "",
        "company_profile_id": row["company_profile_id"],
        "latest_title": row["latest_title"] or "",
        "latest_summary": row["latest_summary"] or "",
        "latest_fit_by_campaign": row["latest_fit_by_campaign"] or {},
        "first_seen_at": row["first_seen_at"].isoformat() if row["first_seen_at"] else None,
        "last_seen_at": row["last_seen_at"].isoformat() if row["last_seen_at"] else None,
        "times_seen": row["times_seen"] or 0,
        "last_run_id": row["last_run_id"],
        "known_email": row["known_email"] if "known_email" in row.keys() else None,
    }


def _row_to_lead_memory(row: asyncpg.Record) -> dict:
    return {
        "id": row["id"],
        "run_id": row["run_id"],
        "lead_id": row["lead_id"],
        "campaign_id": row["campaign_id"],
        "campaign_version": row["campaign_version"] or "v1",
        "policy_version": row["policy_version"] or "v1",
        "company_profile_id": row["company_profile_id"],
        "person_profile_id": row["person_profile_id"],
        "raw_input": row["raw_input"] or {},
        "enrichment_snapshot": row["enrichment_snapshot"] or {},
        "evidence_snapshot": row["evidence_snapshot"] or {},
        "decision_snapshot": row["decision_snapshot"] or {},
        "routing": row["routing"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


def _row_to_review_feedback(row: asyncpg.Record) -> dict:
    return {
        "id": row["id"],
        "run_id": row["run_id"],
        "lead_id": row["lead_id"],
        "campaign_id": row["campaign_id"],
        "company_profile_id": row["company_profile_id"],
        "person_profile_id": row["person_profile_id"],
        "feedback_type": row["feedback_type"],
        "feedback_reason": row["feedback_reason"] or "",
        "corrected_decision": row["corrected_decision"],
        "fit_outcome": row["fit_outcome"],
        "owner": row["owner"] or "",
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


def _row_to_campaign_version(row: asyncpg.Record) -> dict:
    return {
        "id": row["id"],
        "campaign_id": row["campaign_id"],
        "version": row["version"],
        "status": row["status"],
        "config_json": row["config_json"] or "",
        "product_md": row["product_md"] or "",
        "icp_md": row["icp_md"] or "",
        "signals_md": row["signals_md"] or "",
        "disqualification_md": row["disqualification_md"] or "",
        "qualification_md": row["qualification_md"] or "",
        "created_by": row["created_by"] or "",
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "approved_by": row["approved_by"] or "",
        "approved_at": row["approved_at"].isoformat() if row["approved_at"] else None,
    }


def _row_to_policy_version(row: asyncpg.Record) -> dict:
    return {
        "id": row["id"],
        "campaign_id": row["campaign_id"],
        "policy_version": row["policy_version"],
        "score_weights": row["score_weights"] or {},
        "qualified_threshold": row["qualified_threshold"],
        "disqualified_threshold": row["disqualified_threshold"],
        "routing_rules": row["routing_rules"] or {},
        "status": row["status"],
        "created_by": row["created_by"] or "",
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "approved_by": row["approved_by"] or "",
        "approved_at": row["approved_at"].isoformat() if row["approved_at"] else None,
    }


async def update_run_email_status(pool: asyncpg.Pool, run_id: str, status: str) -> None:
    await pool.execute(
        "UPDATE runs SET email_status=$1 WHERE run_id=$2", status, run_id
    )


async def mark_leads_email_sent(pool: asyncpg.Pool, run_id: str, lead_ids: list) -> None:
    await pool.execute(
        """UPDATE leads SET email_sent=TRUE, email_sent_at=now()
             WHERE run_id=$1 AND lead_id = ANY($2::text[])""",
        run_id, lead_ids,
    )


async def set_lead_manual_email_status(
    pool: asyncpg.Pool, run_id: str, lead_id: str, status: str
) -> None:
    await pool.execute(
        "UPDATE leads SET manual_email_status=$1 WHERE run_id=$2 AND lead_id=$3",
        status, run_id, lead_id,
    )


async def get_leads_for_email(pool: asyncpg.Pool, run_id: str, min_score: int) -> list:
    rows = await pool.fetch(
        """SELECT * FROM leads
             WHERE run_id=$1
               AND decision IN ('qualified','review')
               AND score >= $2
               AND email_sent = FALSE
             ORDER BY score DESC""",
        run_id, min_score,
    )
    return [_row_to_lead(r) for r in rows]


# ── Agent memory ───────────────────────────────────────────────────────────────

async def get_company_profile_by_domain(
    pool: asyncpg.Pool, domain: str
) -> Optional[dict]:
    canonical_domain = _normalize_domain(domain)
    if not canonical_domain:
        return None
    row = await pool.fetchrow(
        "SELECT * FROM company_profiles WHERE canonical_domain = $1",
        canonical_domain,
    )
    return _row_to_company_profile(row) if row else None


async def upsert_company_profile(
    pool: asyncpg.Pool,
    company_name: str,
    domain: str = "",
    *,
    structured_profile: Optional[dict] = None,
    summary: str = "",
    campaign_id: Optional[str] = None,
    fit_summary: Optional[dict] = None,
    run_id: Optional[str] = None,
) -> dict:
    normalized = normalize_company_key(company_name, domain)
    if not normalized["company_name"] and not normalized["canonical_domain"]:
        raise ValueError("company_name or domain is required")

    row = None
    if normalized["canonical_domain"]:
        row = await pool.fetchrow(
            "SELECT * FROM company_profiles WHERE canonical_domain = $1",
            normalized["canonical_domain"],
        )
    if row is None and normalized["normalized_company_name"]:
        row = await pool.fetchrow(
            """SELECT * FROM company_profiles
                 WHERE normalized_company_name = $1
                 ORDER BY times_seen DESC, id ASC
                 LIMIT 1""",
            normalized["normalized_company_name"],
        )

    latest_fit_by_campaign = dict((row["latest_fit_by_campaign"] if row else None) or {})
    if campaign_id and fit_summary is not None:
        latest_fit_by_campaign[campaign_id] = fit_summary

    structured_profile = structured_profile or ((row["latest_structured_profile"] if row else None) or {})
    latest_summary = summary or ((row["latest_summary"] if row else None) or "")

    if row:
        updated = await pool.fetchrow(
            """
            UPDATE company_profiles
               SET canonical_domain = COALESCE($2, canonical_domain),
                   company_name = CASE WHEN $3 <> '' THEN $3 ELSE company_name END,
                   normalized_company_name = CASE WHEN $4 <> '' THEN $4 ELSE normalized_company_name END,
                   latest_structured_profile = $5,
                   latest_summary = $6,
                   latest_fit_by_campaign = $7,
                   last_seen_at = now(),
                   times_seen = times_seen + 1,
                   last_run_id = COALESCE($8, last_run_id)
             WHERE id = $1
         RETURNING *
            """,
            row["id"],
            normalized["canonical_domain"],
            normalized["company_name"],
            normalized["normalized_company_name"],
            structured_profile,
            latest_summary,
            latest_fit_by_campaign,
            run_id,
        )
    else:
        updated = await pool.fetchrow(
            """
            INSERT INTO company_profiles (
                canonical_domain, company_name, normalized_company_name,
                latest_structured_profile, latest_summary, latest_fit_by_campaign, last_run_id
            ) VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING *
            """,
            normalized["canonical_domain"],
            normalized["company_name"] or normalized["canonical_domain"] or "unknown",
            normalized["normalized_company_name"] or normalized["canonical_domain"] or "unknown",
            structured_profile,
            latest_summary,
            latest_fit_by_campaign,
            run_id,
        )
    return _row_to_company_profile(updated)


async def search_company_profiles(
    pool: asyncpg.Pool, query: str, limit: int = 20
) -> list[dict]:
    normalized_query = re.sub(r"[^a-z0-9]+", " ", _normalize_text(query).lower()).strip()
    if not normalized_query:
        return []
    rows = await pool.fetch(
        """
        SELECT * FROM company_profiles
         WHERE normalized_company_name LIKE '%' || $1 || '%'
            OR canonical_domain LIKE '%' || $2 || '%'
         ORDER BY times_seen DESC, last_seen_at DESC
         LIMIT $3
        """,
        normalized_query,
        _normalize_domain(query),
        max(1, limit),
    )
    return [_row_to_company_profile(r) for r in rows]


async def get_person_profile(
    pool: asyncpg.Pool,
    *,
    person_name: str = "",
    linkedin_url: str = "",
    company_profile_id: Optional[int] = None,
) -> Optional[dict]:
    normalized = normalize_person_key(person_name, linkedin_url)
    row = None
    if normalized["linkedin_url"]:
        row = await pool.fetchrow(
            "SELECT * FROM person_profiles WHERE linkedin_url = $1",
            normalized["linkedin_url"],
        )
    if row is None and normalized["normalized_person_name"]:
        if company_profile_id is not None:
            row = await pool.fetchrow(
                """
                SELECT * FROM person_profiles
                 WHERE normalized_person_name = $1 AND company_profile_id = $2
                 ORDER BY times_seen DESC, id ASC
                 LIMIT 1
                """,
                normalized["normalized_person_name"],
                company_profile_id,
            )
        else:
            row = await pool.fetchrow(
                """
                SELECT * FROM person_profiles
                 WHERE normalized_person_name = $1
                 ORDER BY times_seen DESC, id ASC
                 LIMIT 1
                """,
                normalized["normalized_person_name"],
            )
    return _row_to_person_profile(row) if row else None


async def upsert_person_profile(
    pool: asyncpg.Pool,
    person_name: str,
    *,
    linkedin_url: str = "",
    company_profile_id: Optional[int] = None,
    title: str = "",
    summary: str = "",
    campaign_id: Optional[str] = None,
    fit_summary: Optional[dict] = None,
    run_id: Optional[str] = None,
    known_email: Optional[str] = None,
) -> dict:
    normalized = normalize_person_key(person_name, linkedin_url)
    if not normalized["person_name"] and not normalized["linkedin_url"]:
        raise ValueError("person_name or linkedin_url is required")

    current = await get_person_profile(
        pool,
        person_name=normalized["person_name"],
        linkedin_url=normalized["linkedin_url"] or "",
        company_profile_id=company_profile_id,
    )

    latest_fit_by_campaign = dict((current["latest_fit_by_campaign"] if current else None) or {})
    if campaign_id and fit_summary is not None:
        latest_fit_by_campaign[campaign_id] = fit_summary

    latest_title = title or ((current["latest_title"] if current else None) or "")
    latest_summary = summary or ((current["latest_summary"] if current else None) or "")
    # Only update known_email if a non-empty value is explicitly passed
    resolved_email = known_email if known_email else (current["known_email"] if current else None)

    if current:
        row = await pool.fetchrow(
            """
            UPDATE person_profiles
               SET person_name = CASE WHEN $2 <> '' THEN $2 ELSE person_name END,
                   normalized_person_name = CASE WHEN $3 <> '' THEN $3 ELSE normalized_person_name END,
                   linkedin_url = COALESCE($4, linkedin_url),
                   company_profile_id = COALESCE($5, company_profile_id),
                   latest_title = $6,
                   latest_summary = $7,
                   latest_fit_by_campaign = $8,
                   last_seen_at = now(),
                   times_seen = times_seen + 1,
                   last_run_id = COALESCE($9, last_run_id),
                   known_email = COALESCE($10, known_email)
             WHERE id = $1
         RETURNING *
            """,
            current["id"],
            normalized["person_name"],
            normalized["normalized_person_name"],
            normalized["linkedin_url"],
            company_profile_id,
            latest_title,
            latest_summary,
            latest_fit_by_campaign,
            run_id,
            resolved_email,
        )
    else:
        row = await pool.fetchrow(
            """
            INSERT INTO person_profiles (
                person_name, normalized_person_name, linkedin_url,
                company_profile_id, latest_title, latest_summary,
                latest_fit_by_campaign, last_run_id, known_email
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            RETURNING *
            """,
            normalized["person_name"] or normalized["linkedin_url"] or "unknown",
            normalized["normalized_person_name"] or normalized["linkedin_url"] or "unknown",
            normalized["linkedin_url"],
            company_profile_id,
            latest_title,
            latest_summary,
            latest_fit_by_campaign,
            run_id,
            resolved_email,
        )
    return _row_to_person_profile(row)


async def search_person_profiles(
    pool: asyncpg.Pool, query: str, limit: int = 20
) -> list[dict]:
    normalized_query = re.sub(r"[^a-z0-9]+", " ", _normalize_text(query).lower()).strip()
    if not normalized_query:
        return []
    rows = await pool.fetch(
        """
        SELECT * FROM person_profiles
         WHERE normalized_person_name LIKE '%' || $1 || '%'
            OR linkedin_url LIKE '%' || $2 || '%'
         ORDER BY times_seen DESC, last_seen_at DESC
         LIMIT $3
        """,
        normalized_query,
        _normalize_text(query),
        max(1, limit),
    )
    return [_row_to_person_profile(r) for r in rows]


async def insert_lead_memory(
    pool: asyncpg.Pool,
    *,
    run_id: str,
    lead_id: str,
    campaign_id: str,
    routing: str,
    campaign_version: str = "v1",
    policy_version: str = "v1",
    company_profile_id: Optional[int] = None,
    person_profile_id: Optional[int] = None,
    raw_input: Optional[dict] = None,
    enrichment_snapshot: Optional[dict] = None,
    evidence_snapshot: Optional[dict] = None,
    decision_snapshot: Optional[dict] = None,
) -> dict:
    row = await pool.fetchrow(
        """
        INSERT INTO lead_memory (
            run_id, lead_id, campaign_id, campaign_version, policy_version,
            company_profile_id, person_profile_id, raw_input, enrichment_snapshot,
            evidence_snapshot, decision_snapshot, routing
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
        RETURNING *
        """,
        run_id,
        lead_id,
        campaign_id,
        campaign_version,
        policy_version,
        company_profile_id,
        person_profile_id,
        raw_input or {},
        enrichment_snapshot or {},
        evidence_snapshot or {},
        decision_snapshot or {},
        routing,
    )
    return _row_to_lead_memory(row)


async def get_lead_memory_for_company(
    pool: asyncpg.Pool,
    company_profile_id: int,
    campaign_id: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    if campaign_id:
        rows = await pool.fetch(
            """
            SELECT * FROM lead_memory
             WHERE company_profile_id = $1 AND campaign_id = $2
             ORDER BY created_at DESC
             LIMIT $3
            """,
            company_profile_id,
            campaign_id,
            max(1, limit),
        )
    else:
        rows = await pool.fetch(
            """
            SELECT * FROM lead_memory
             WHERE company_profile_id = $1
             ORDER BY created_at DESC
             LIMIT $2
            """,
            company_profile_id,
            max(1, limit),
        )
    return [_row_to_lead_memory(r) for r in rows]


async def get_lead_memory_for_person(
    pool: asyncpg.Pool,
    person_profile_id: int,
    campaign_id: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    if campaign_id:
        rows = await pool.fetch(
            """
            SELECT * FROM lead_memory
             WHERE person_profile_id = $1 AND campaign_id = $2
             ORDER BY created_at DESC
             LIMIT $3
            """,
            person_profile_id,
            campaign_id,
            max(1, limit),
        )
    else:
        rows = await pool.fetch(
            """
            SELECT * FROM lead_memory
             WHERE person_profile_id = $1
             ORDER BY created_at DESC
             LIMIT $2
            """,
            person_profile_id,
            max(1, limit),
        )
    return [_row_to_lead_memory(r) for r in rows]


async def get_lead_memory_for_run(pool: asyncpg.Pool, run_id: str) -> list[dict]:
    rows = await pool.fetch(
        "SELECT * FROM lead_memory WHERE run_id = $1 ORDER BY created_at DESC",
        run_id,
    )
    return [_row_to_lead_memory(r) for r in rows]


async def get_lead_memory_item(
    pool: asyncpg.Pool, run_id: str, lead_id: str
) -> Optional[dict]:
    row = await pool.fetchrow(
        """
        SELECT * FROM lead_memory
         WHERE run_id = $1 AND lead_id = $2
         ORDER BY created_at DESC
         LIMIT 1
        """,
        run_id,
        lead_id,
    )
    return _row_to_lead_memory(row) if row else None


async def add_review_feedback(
    pool: asyncpg.Pool,
    *,
    lead_id: str,
    campaign_id: str,
    feedback_type: str,
    run_id: Optional[str] = None,
    company_profile_id: Optional[int] = None,
    person_profile_id: Optional[int] = None,
    feedback_reason: str = "",
    corrected_decision: Optional[str] = None,
    fit_outcome: Optional[str] = None,
    owner: str = "",
) -> dict:
    row = await pool.fetchrow(
        """
        INSERT INTO review_feedback (
            run_id, lead_id, campaign_id, company_profile_id, person_profile_id,
            feedback_type, feedback_reason, corrected_decision, fit_outcome, owner
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
        RETURNING *
        """,
        run_id,
        lead_id,
        campaign_id,
        company_profile_id,
        person_profile_id,
        feedback_type,
        feedback_reason,
        corrected_decision,
        fit_outcome,
        owner,
    )
    return _row_to_review_feedback(row)


async def list_review_feedback(
    pool: asyncpg.Pool,
    *,
    campaign_id: Optional[str] = None,
    feedback_type: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    clauses = []
    params: list = []
    if campaign_id:
        params.append(campaign_id)
        clauses.append(f"campaign_id = ${len(params)}")
    if feedback_type:
        params.append(feedback_type)
        clauses.append(f"feedback_type = ${len(params)}")
    params.extend([max(1, limit), max(0, offset)])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = await pool.fetch(
        f"""
        SELECT * FROM review_feedback
        {where}
        ORDER BY created_at DESC
        LIMIT ${len(params) - 1} OFFSET ${len(params)}
        """,
        *params,
    )
    return [_row_to_review_feedback(r) for r in rows]


# ── Campaign versions ──────────────────────────────────────────────────────────

async def create_campaign_version(
    pool: asyncpg.Pool,
    campaign_id: str,
    version: str,
    *,
    config_json: str = "",
    product_md: str = "",
    icp_md: str = "",
    signals_md: str = "",
    disqualification_md: str = "",
    qualification_md: str = "",
    created_by: str = "",
    status: str = "draft",
) -> dict:
    row = await pool.fetchrow(
        """
        INSERT INTO campaign_versions (
            campaign_id, version, status, config_json, product_md, icp_md,
            signals_md, disqualification_md, qualification_md, created_by
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
        ON CONFLICT (campaign_id, version) DO UPDATE SET
            status = EXCLUDED.status,
            config_json = EXCLUDED.config_json,
            product_md = EXCLUDED.product_md,
            icp_md = EXCLUDED.icp_md,
            signals_md = EXCLUDED.signals_md,
            disqualification_md = EXCLUDED.disqualification_md,
            qualification_md = EXCLUDED.qualification_md,
            created_by = EXCLUDED.created_by
        RETURNING *
        """,
        campaign_id, version, status, config_json, product_md, icp_md,
        signals_md, disqualification_md, qualification_md, created_by,
    )
    return _row_to_campaign_version(row)


async def list_campaign_versions(pool: asyncpg.Pool, campaign_id: str) -> list[dict]:
    rows = await pool.fetch(
        """
        SELECT * FROM campaign_versions
         WHERE campaign_id = $1
         ORDER BY created_at DESC, id DESC
        """,
        campaign_id,
    )
    return [_row_to_campaign_version(r) for r in rows]


async def get_active_campaign_version(pool: asyncpg.Pool, campaign_id: str) -> Optional[dict]:
    row = await pool.fetchrow(
        """
        SELECT * FROM campaign_versions
         WHERE campaign_id = $1 AND status = 'active'
         ORDER BY approved_at DESC NULLS LAST, created_at DESC, id DESC
         LIMIT 1
        """,
        campaign_id,
    )
    return _row_to_campaign_version(row) if row else None


async def get_campaign_version(
    pool: asyncpg.Pool, campaign_id: str, version: str
) -> Optional[dict]:
    row = await pool.fetchrow(
        """
        SELECT * FROM campaign_versions
         WHERE campaign_id = $1 AND version = $2
         LIMIT 1
        """,
        campaign_id,
        version,
    )
    return _row_to_campaign_version(row) if row else None


async def activate_campaign_version(
    pool: asyncpg.Pool, campaign_id: str, version: str, approved_by: str
) -> Optional[dict]:
    row = await pool.fetchrow(
        """
        SELECT * FROM campaign_versions
         WHERE campaign_id = $1 AND version = $2
         LIMIT 1
        """,
        campaign_id,
        version,
    )
    if not row:
        return None
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE campaign_versions
                   SET status = CASE WHEN status = 'active' THEN 'archived' ELSE status END
                 WHERE campaign_id = $1
                """,
                campaign_id,
            )
            updated = await conn.fetchrow(
                """
                UPDATE campaign_versions
                   SET status='active', approved_by=$3, approved_at=now()
                 WHERE campaign_id = $1 AND version = $2
             RETURNING *
                """,
                campaign_id,
                version,
                approved_by,
            )
    return _row_to_campaign_version(updated) if updated else None


# ── Policy versions ────────────────────────────────────────────────────────────

async def create_policy_version(
    pool: asyncpg.Pool,
    campaign_id: str,
    policy_version: str,
    *,
    score_weights: Optional[dict] = None,
    qualified_threshold: int = 70,
    disqualified_threshold: int = 60,
    routing_rules: Optional[dict] = None,
    created_by: str = "",
    status: str = "draft",
) -> dict:
    row = await pool.fetchrow(
        """
        INSERT INTO policy_versions (
            campaign_id, policy_version, score_weights, qualified_threshold,
            disqualified_threshold, routing_rules, status, created_by
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
        ON CONFLICT (campaign_id, policy_version) DO UPDATE SET
            score_weights = EXCLUDED.score_weights,
            qualified_threshold = EXCLUDED.qualified_threshold,
            disqualified_threshold = EXCLUDED.disqualified_threshold,
            routing_rules = EXCLUDED.routing_rules,
            status = EXCLUDED.status,
            created_by = EXCLUDED.created_by
        RETURNING *
        """,
        campaign_id,
        policy_version,
        score_weights or {},
        qualified_threshold,
        disqualified_threshold,
        routing_rules or {},
        status,
        created_by,
    )
    return _row_to_policy_version(row)


async def list_policy_versions(pool: asyncpg.Pool, campaign_id: str) -> list[dict]:
    rows = await pool.fetch(
        """
        SELECT * FROM policy_versions
         WHERE campaign_id = $1
         ORDER BY created_at DESC, id DESC
        """,
        campaign_id,
    )
    return [_row_to_policy_version(r) for r in rows]


async def get_active_policy_version(pool: asyncpg.Pool, campaign_id: str) -> Optional[dict]:
    row = await pool.fetchrow(
        """
        SELECT * FROM policy_versions
         WHERE campaign_id = $1 AND status = 'active'
         ORDER BY approved_at DESC NULLS LAST, created_at DESC, id DESC
         LIMIT 1
        """,
        campaign_id,
    )
    return _row_to_policy_version(row) if row else None


async def get_policy_version(
    pool: asyncpg.Pool, campaign_id: str, policy_version: str
) -> Optional[dict]:
    row = await pool.fetchrow(
        """
        SELECT * FROM policy_versions
         WHERE campaign_id = $1 AND policy_version = $2
         LIMIT 1
        """,
        campaign_id,
        policy_version,
    )
    return _row_to_policy_version(row) if row else None


async def activate_policy_version(
    pool: asyncpg.Pool, campaign_id: str, policy_version: str, approved_by: str
) -> Optional[dict]:
    row = await pool.fetchrow(
        """
        SELECT * FROM policy_versions
         WHERE campaign_id = $1 AND policy_version = $2
         LIMIT 1
        """,
        campaign_id,
        policy_version,
    )
    if not row:
        return None
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE policy_versions
                   SET status = CASE WHEN status = 'active' THEN 'archived' ELSE status END
                 WHERE campaign_id = $1
                """,
                campaign_id,
            )
            updated = await conn.fetchrow(
                """
                UPDATE policy_versions
                   SET status='active', approved_by=$3, approved_at=now()
                 WHERE campaign_id = $1 AND policy_version = $2
             RETURNING *
                """,
                campaign_id,
                policy_version,
                approved_by,
            )
    return _row_to_policy_version(updated) if updated else None


# ── Settings ──────────────────────────────────────────────────────────────────

_SETTINGS_DEFAULTS = {"qualified_threshold": "70", "disqualified_threshold": "60"}


async def get_settings(pool: asyncpg.Pool, campaign_id: str = "seedance") -> dict:
    # Try campaign_settings first; fall back to legacy settings table
    rows = await pool.fetch(
        "SELECT key, value FROM campaign_settings WHERE campaign_id = $1", campaign_id
    )
    if not rows:
        rows = await pool.fetch("SELECT key, value FROM settings")
    result = dict(_SETTINGS_DEFAULTS)
    for r in rows:
        result[r["key"]] = r["value"]
    return result


async def save_settings(pool: asyncpg.Pool, settings: dict, campaign_id: str = "seedance") -> None:
    for key, value in settings.items():
        await pool.execute(
            """INSERT INTO campaign_settings(campaign_id, key, value)
               VALUES($1, $2, $3)
               ON CONFLICT(campaign_id, key) DO UPDATE SET value=$3, updated_at=now()""",
            campaign_id, key, str(value),
        )
        # Keep legacy settings in sync for backward compatibility
        if campaign_id == "seedance":
            await pool.execute(
                """INSERT INTO settings(key, value) VALUES($1, $2)
                   ON CONFLICT(key) DO UPDATE SET value=$2, updated_at=now()""",
                key, str(value),
            )


# ── Exclusions ────────────────────────────────────────────────────────────────

async def get_exclusions(pool: asyncpg.Pool, campaign_id: str = "seedance") -> list:
    rows = await pool.fetch(
        "SELECT id, value FROM exclusions WHERE campaign_id = $1 ORDER BY created_at DESC",
        campaign_id,
    )
    if not rows:
        # Fall back to legacy rows without campaign_id for Seedance
        rows = await pool.fetch(
            "SELECT id, value FROM exclusions WHERE campaign_id IS NULL ORDER BY created_at DESC"
        )
    return [{"id": r["id"], "value": r["value"]} for r in rows]


async def add_exclusion(pool: asyncpg.Pool, value: str, campaign_id: str = "seedance") -> None:
    await pool.execute(
        "INSERT INTO exclusions(campaign_id, value) VALUES($1, $2) ON CONFLICT (campaign_id, value) DO NOTHING",
        campaign_id, value.strip(),
    )


async def remove_exclusion(pool: asyncpg.Pool, exclusion_id: int) -> None:
    await pool.execute("DELETE FROM exclusions WHERE id=$1", exclusion_id)


# ── Instantly campaigns & routing ─────────────────────────────────────────────

async def list_instantly_campaigns(pool: asyncpg.Pool) -> list:
    rows = await pool.fetch(
        """SELECT id, label, campaign_id, meeting_note, pain_point, created_at
             FROM instantly_campaigns ORDER BY label"""
    )
    return [
        {"id": r["id"], "label": r["label"], "campaign_id": r["campaign_id"],
         "meeting_note": r["meeting_note"] or "", "pain_point": r["pain_point"] or "",
         "created_at": r["created_at"].isoformat() if r["created_at"] else None}
        for r in rows
    ]


async def set_campaign_note(pool: asyncpg.Pool, campaign_id: str,
                            meeting_note: str, pain_point: str) -> None:
    """Persist a campaign's meeting note + extracted pain point."""
    await pool.execute(
        """UPDATE instantly_campaigns SET meeting_note=$2, pain_point=$3
             WHERE campaign_id=$1""",
        campaign_id.strip(), meeting_note or "", pain_point or "",
    )


async def get_campaign_pain_points(pool: asyncpg.Pool) -> dict:
    """Return {campaign_id: pain_point} for all campaigns that have one set."""
    rows = await pool.fetch(
        "SELECT campaign_id, pain_point FROM instantly_campaigns WHERE pain_point <> ''"
    )
    return {r["campaign_id"]: r["pain_point"] for r in rows}


# ── Campaign meeting notes (batch for synthesis) ──────────────────────────────

async def add_campaign_note(pool: asyncpg.Pool, campaign_id: str, note_text: str) -> dict:
    row = await pool.fetchrow(
        """INSERT INTO campaign_notes(campaign_id, note_text) VALUES($1, $2)
           RETURNING id, campaign_id, note_text, created_at""",
        campaign_id.strip(), note_text,
    )
    return {"id": row["id"], "campaign_id": row["campaign_id"],
            "note_text": row["note_text"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None}


async def list_campaign_notes(pool: asyncpg.Pool, campaign_id: str) -> list:
    rows = await pool.fetch(
        """SELECT id, note_text, created_at FROM campaign_notes
             WHERE campaign_id=$1 ORDER BY created_at""",
        campaign_id.strip(),
    )
    return [
        {"id": r["id"], "note_text": r["note_text"],
         "created_at": r["created_at"].isoformat() if r["created_at"] else None}
        for r in rows
    ]


async def get_campaign_note_texts(pool: asyncpg.Pool, campaign_id: str) -> list:
    """Return just the note strings for a campaign (for synthesis)."""
    rows = await pool.fetch(
        "SELECT note_text FROM campaign_notes WHERE campaign_id=$1 ORDER BY created_at",
        campaign_id.strip(),
    )
    return [r["note_text"] for r in rows]


async def delete_campaign_note(pool: asyncpg.Pool, note_id: int) -> None:
    await pool.execute("DELETE FROM campaign_notes WHERE id=$1", note_id)


async def count_campaign_notes(pool: asyncpg.Pool) -> dict:
    """Return {campaign_id: note_count} for surfacing counts in the UI list."""
    rows = await pool.fetch(
        "SELECT campaign_id, COUNT(*) AS n FROM campaign_notes GROUP BY campaign_id"
    )
    return {r["campaign_id"]: r["n"] for r in rows}


async def add_instantly_campaign(pool: asyncpg.Pool, label: str, campaign_id: str) -> dict:
    row = await pool.fetchrow(
        """INSERT INTO instantly_campaigns(label, campaign_id) VALUES($1, $2)
           ON CONFLICT (campaign_id) DO UPDATE SET label=$1
           RETURNING id, label, campaign_id, created_at""",
        label.strip(), campaign_id.strip(),
    )
    return {"id": row["id"], "label": row["label"], "campaign_id": row["campaign_id"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None}


async def remove_instantly_campaign(pool: asyncpg.Pool, row_id: int) -> None:
    await pool.execute("DELETE FROM instantly_campaigns WHERE id=$1", row_id)


async def get_routing_map(pool: asyncpg.Pool) -> dict:
    """Return {vertical: campaign_id} for all configured routing rules."""
    rows = await pool.fetch("SELECT vertical, campaign_id FROM instantly_routing")
    return {r["vertical"]: r["campaign_id"] for r in rows}


async def get_routing_rules(pool: asyncpg.Pool) -> list:
    rows = await pool.fetch(
        "SELECT vertical, campaign_id FROM instantly_routing ORDER BY vertical"
    )
    return [{"vertical": r["vertical"], "campaign_id": r["campaign_id"]} for r in rows]


async def set_routing(pool: asyncpg.Pool, vertical: str, campaign_id: str) -> None:
    """Upsert a vertical→campaign rule. Empty campaign_id clears the rule."""
    vertical = (vertical or "").strip()
    campaign_id = (campaign_id or "").strip()
    if not campaign_id:
        await pool.execute("DELETE FROM instantly_routing WHERE vertical=$1", vertical)
        return
    await pool.execute(
        """INSERT INTO instantly_routing(vertical, campaign_id) VALUES($1, $2)
           ON CONFLICT (vertical) DO UPDATE SET campaign_id=$2, updated_at=now()""",
        vertical, campaign_id,
    )


async def set_lead_instantly_campaign(
    pool: asyncpg.Pool, run_id: str, lead_id: str, campaign_id: str
) -> None:
    await pool.execute(
        "UPDATE leads SET instantly_campaign_id=$1 WHERE run_id=$2 AND lead_id=$3",
        campaign_id, run_id, lead_id,
    )


# ── Email Templates ───────────────────────────────────────────────────────────

_SEED_TEMPLATES = [
    {
        "vertical": "ai_tools",
        "name": "AI Tools – Day 0 Partner",
        "subject": "Seedance 2.0 + {company}?",
        "body": """Hi {first_name},

X community went wild about new Seedance 2.0 teasers.

What about {company} as a Day 0 launch partner, right next to Freepik, Krea, Artlist, Picsart, and Higgsfield?

If interested, let's chat:
Book 30min: https://bytedance.sg.larkoffice.com/scheduler/d18e3ecd70eae8c6

--
Antti Eskola
Enterprise Partnerships & Solutions
Email: antti.eskola@bytedance.com
Mobile: +44 7999 826 189
Address: 4 Lindsey St, London""",
    },
    {
        "vertical": "film_production",
        "name": "Film Production – First Email",
        "subject": "Seedance 2.0 + {company}?",
        "body": """Hi {first_name},

Film production enthusiasts are calling Seedance 2.0 a real-time disruption of the entertainment industry.
Model can produce a usable video about 90% of the time on the very first try, due to strong prompt adherence.

Relevant for {company}?

---
Antti Eskola
Enterprise Partnerships & Solutions
Email: antti.eskola@bytedance.com
Mobile: +44 7999 826 189
Book 30min: https://bytedance.sg.larkoffice.com/scheduler/d18e3ecd70eae8c6""",
    },
    {
        "vertical": "vfx_production",
        "name": "VFX Production – Omni Feature",
        "subject": "Seedance 2.0 + {company}?",
        "body": """Hi {first_name},

You might find this interesting.

For VFX projects, the Seedance Omni feature excels in the following areas:
- Physical dynamics and motion
- Large visual effects shots
- Cinematic movement
- Complex environments

OMNI allows combining multiple references in single generation: images, video, and audio.

Relevant for {company}?

ps. 4K output is coming soon.

--
Antti Eskola
Enterprise Partnerships & Solutions
Email: antti.eskola@bytedance.com
Mobile: +44 7999 826 189
Book 30min: https://bytedance.sg.larkoffice.com/scheduler/d18e3ecd70eae8c6""",
    },
    {
        "vertical": "animation",
        "name": "Animation – Real-time Disruption",
        "subject": "Seedance 2.0 + {company}?",
        "body": """Hi {first_name},

Film production enthusiasts are calling Seedance 2.0 a real-time disruption of the entertainment industry.
Model can produce a usable video about 90% of the time on the very first try, due to strong prompt adherence.

Relevant for {company}?

--
Antti Eskola
Enterprise Partnerships & Solutions
Email: antti.eskola@bytedance.com
Mobile: +44 7999 826 189
Book 30min: https://bytedance.sg.larkoffice.com/scheduler/d18e3ecd70eae8c6""",
    },
    {
        "vertical": "short_drama",
        "name": "Short Drama – Day 0 Access",
        "subject": "Seedance 2.0 + {company}?",
        "body": """Hi {first_name},

New Seedance 2.0 is coming soon.
It will be the most advanced AI video model across all parameters.
Ideal fit for vertical dramas and film studios.
So far 15 studios have signed up for Day 0 access.

Would this be a good time to explore ByteDance models and potential partnerships?

--
Antti Eskola
Enterprise Partnerships & Solutions
Email: antti.eskola@bytedance.com
Mobile: +44 7999 826 189
Book 30min: https://bytedance.sg.larkoffice.com/scheduler/d18e3ecd70eae8c6""",
    },
    {
        "vertical": "ecommerce",
        "name": "Ecommerce – Knowledge Session",
        "subject": "Seedance 2.0 + {company}?",
        "body": """Hi {first_name},

In the retail sector, new ByteDance AI models are becoming popular.
Due to demand, we are creating knowledge sessions.

30min Knowledge Session
- Who are ByteDance
- Seedance 2.0 & Seedream 5.0
- What the models can and can't do
- Where do we expect to be in 12 months

20 min presentation with 10 mins for questions.
At the end, we offer test credits so you & your team can test yourselves.

Interested?

--
Antti Eskola
Enterprise Partnerships & Solutions
Email: antti.eskola@bytedance.com
Mobile: +44 7999 826 189
Book 30min: https://bytedance.sg.larkoffice.com/scheduler/d18e3ecd70eae8c6""",
    },
    {
        "vertical": "ai_generic",
        "name": "AI Generic – Advanced Model",
        "subject": "Seedance 2.0 + {company}?",
        "body": """Hi {first_name},

You might find this interesting as this is relevant in your space.

New Seedance 2.0 is the most advanced AI video model in the world.
Ideal fit for creative production, your industry, and film studios.
So far 20+ companies have signed up for Day 0 access.

Would this be a good time to explore ByteDance models and potential partnerships?

ps. direct API comes with advanced capabilities.

Happy to explain more if it seems relevant.

--
Antti Eskola
Enterprise Partnerships & Solutions
Email: antti.eskola@bytedance.com
Mobile: +44 7999 826 189
Book 30min: https://bytedance.sg.larkoffice.com/scheduler/d18e3ecd70eae8c6""",
    },
]


async def seed_email_templates(pool: asyncpg.Pool, campaign_id: str = "seedance") -> None:
    count = await pool.fetchval(
        "SELECT COUNT(*) FROM email_templates WHERE campaign_id = $1", campaign_id
    )
    if count and count > 0:
        return
    if campaign_id == "seedance":
        for t in _SEED_TEMPLATES:
            await pool.execute(
                "INSERT INTO email_templates (campaign_id, vertical, name, subject, body) VALUES ($1,$2,$3,$4,$5)",
                campaign_id, t["vertical"], t["name"], t["subject"], t["body"],
            )


async def get_email_templates(pool: asyncpg.Pool, campaign_id: str = "seedance") -> list:
    rows = await pool.fetch(
        """SELECT id, campaign_id, vertical, name, subject, body, created_at
             FROM email_templates
            WHERE campaign_id = $1
            ORDER BY vertical, id""",
        campaign_id,
    )
    if not rows:
        # Fall back to legacy rows without campaign_id
        rows = await pool.fetch(
            "SELECT id, vertical, name, subject, body, created_at FROM email_templates WHERE campaign_id IS NULL ORDER BY vertical, id"
        )
    return [
        {
            "id":       r["id"],
            "vertical": r["vertical"],
            "name":     r["name"],
            "subject":  r["subject"],
            "body":     r["body"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]


async def add_email_template(
    pool: asyncpg.Pool, vertical: str, name: str, subject: str, body: str,
    campaign_id: str = "seedance",
) -> dict:
    row = await pool.fetchrow(
        """INSERT INTO email_templates (campaign_id, vertical, name, subject, body)
           VALUES ($1,$2,$3,$4,$5)
           RETURNING id, campaign_id, vertical, name, subject, body, created_at""",
        campaign_id, vertical, name, subject, body,
    )
    return {
        "id":       row["id"],
        "vertical": row["vertical"],
        "name":     row["name"],
        "subject":  row["subject"],
        "body":     row["body"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


async def update_email_template(
    pool: asyncpg.Pool, template_id: int, vertical: str, name: str, subject: str, body: str
) -> Optional[dict]:
    row = await pool.fetchrow(
        """UPDATE email_templates
              SET vertical=$2, name=$3, subject=$4, body=$5
            WHERE id=$1
        RETURNING id, vertical, name, subject, body, created_at""",
        template_id, vertical, name, subject, body,
    )
    if not row:
        return None
    return {
        "id":       row["id"],
        "vertical": row["vertical"],
        "name":     row["name"],
        "subject":  row["subject"],
        "body":     row["body"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


async def delete_email_template(pool: asyncpg.Pool, template_id: int) -> None:
    await pool.execute("DELETE FROM email_templates WHERE id=$1", template_id)


# ── Skill files ───────────────────────────────────────────────────────────────

async def seed_skill_file(
    pool: asyncpg.Pool, campaign_id: str, filename: str, content: str
) -> None:
    """Insert a skill file row only if it does not already exist. Never overwrites."""
    await pool.execute(
        """INSERT INTO skill_files (campaign_id, filename, content)
           VALUES ($1, $2, $3)
           ON CONFLICT (campaign_id, filename) DO NOTHING""",
        campaign_id, filename, content,
    )


async def upsert_skill_file(
    pool: asyncpg.Pool, campaign_id: str, filename: str, content: str
) -> None:
    """Insert or update a skill file (explicit user save — always overwrites)."""
    await pool.execute(
        """INSERT INTO skill_files (campaign_id, filename, content, updated_at)
           VALUES ($1, $2, $3, now())
           ON CONFLICT (campaign_id, filename)
           DO UPDATE SET content = EXCLUDED.content, updated_at = now()""",
        campaign_id, filename, content,
    )


async def list_skill_files(pool: asyncpg.Pool, campaign_id: str) -> list:
    """Return file metadata (not content) for a campaign, ordered by filename."""
    rows = await pool.fetch(
        """SELECT filename, length(content) AS size, updated_at
             FROM skill_files
            WHERE campaign_id = $1
            ORDER BY filename""",
        campaign_id,
    )
    return [
        {
            "name":       r["filename"],
            "size":       r["size"],
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        }
        for r in rows
    ]


async def get_skill_file(
    pool: asyncpg.Pool, campaign_id: str, filename: str
) -> Optional[str]:
    """Return the content of a single skill file, or None if not found."""
    row = await pool.fetchrow(
        "SELECT content FROM skill_files WHERE campaign_id = $1 AND filename = $2",
        campaign_id, filename,
    )
    return row["content"] if row else None


async def get_skill_files_for_loader(
    pool: asyncpg.Pool, campaign_id: str
) -> dict[str, str]:
    """
    Return all files needed to build a CampaignContext:
    common/ files first (evidence-extraction.md etc.), then campaign-specific
    files which take precedence on any name collision.
    """
    result: dict[str, str] = {}
    # Common files (shared across all campaigns)
    common_rows = await pool.fetch(
        "SELECT filename, content FROM skill_files WHERE campaign_id = 'common'"
    )
    for r in common_rows:
        result[r["filename"]] = r["content"]
    # Campaign-specific files (overwrite common if same filename)
    campaign_rows = await pool.fetch(
        "SELECT filename, content FROM skill_files WHERE campaign_id = $1",
        campaign_id,
    )
    for r in campaign_rows:
        result[r["filename"]] = r["content"]
    return result


async def list_skill_campaign_ids(pool: asyncpg.Pool) -> list[str]:
    """Return all distinct campaign_ids present in skill_files (includes 'common')."""
    rows = await pool.fetch(
        "SELECT DISTINCT campaign_id FROM skill_files ORDER BY campaign_id"
    )
    return [r["campaign_id"] for r in rows]


async def seed_skill_files_from_disk(pool: asyncpg.Pool, skills_root) -> None:
    """
    Sync skill_files table with the skills directory on disk.

    Rules:
    - common/*  : always force-upsert from disk (developer-managed, never edited by operators)
    - campaigns : INSERT … DO NOTHING (operator edits in DB take priority)
    - Orphaned campaign_ids in DB (directory deleted from disk) are removed
    """
    import logging as _logging
    from pathlib import Path as _Path
    _log = _logging.getLogger(__name__)
    root = _Path(skills_root)
    editable = {".md", ".json"}

    # 1. Remove DB rows for campaign_ids whose directory no longer exists on disk,
    #    but only if they are NOT registered in the campaigns table.
    #    Campaigns created via the UI (no disk directory) must be preserved.
    disk_ids = {d.name for d in root.iterdir() if d.is_dir()}
    db_ids = set(await list_skill_campaign_ids(pool))
    registered_ids = {r["id"] for r in await pool.fetch("SELECT id FROM campaigns")}
    orphaned = (db_ids - disk_ids) - registered_ids
    for cid in orphaned:
        await pool.execute("DELETE FROM skill_files WHERE campaign_id = $1", cid)
        _log.info("skill_files: removed orphaned campaign_id '%s' from DB", cid)

    # 2. Force-upsert common/ files (developer-managed — disk is always source of truth)
    common_dir = root / "common"
    if common_dir.is_dir():
        for f in sorted(common_dir.iterdir()):
            if f.is_file() and f.suffix in editable and not f.name.startswith("."):
                await upsert_skill_file(pool, "common", f.name, f.read_text(encoding="utf-8"))

    # 3. INSERT … DO NOTHING for campaign-specific files (DB wins for operator edits)
    seeded = 0
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or directory.name == "common":
            continue
        for f in sorted(directory.iterdir()):
            if f.is_file() and f.suffix in editable and not f.name.startswith("."):
                await seed_skill_file(pool, directory.name, f.name, f.read_text(encoding="utf-8"))
                seeded += 1

    _log.info(
        "skill_files: synced from disk — %d orphaned removed, common/ force-upserted, %d campaign files seeded",
        len(orphaned), seeded,
    )


async def ensure_campaign(
    pool: asyncpg.Pool, campaign_id: str, name: str, description: str = ""
) -> None:
    """Insert a campaign registry row if it does not already exist."""
    await pool.execute(
        """INSERT INTO campaigns (id, name, description)
           VALUES ($1, $2, $3)
           ON CONFLICT (id) DO NOTHING""",
        campaign_id, name, description,
    )


async def delete_campaign_skill_files(pool: asyncpg.Pool, campaign_id: str) -> None:
    """Remove all skill files for a campaign (used when deleting a campaign)."""
    await pool.execute(
        "DELETE FROM skill_files WHERE campaign_id = $1", campaign_id
    )


async def delete_campaign(pool: asyncpg.Pool, campaign_id: str) -> None:
    """
    Fully remove a campaign: skill files, campaign row.
    Runs that referenced this campaign have their campaign_id set to NULL first
    to avoid FK violation (run history is preserved, just unlinked).
    """
    await pool.execute("DELETE FROM skill_files WHERE campaign_id = $1", campaign_id)
    await pool.execute("UPDATE runs SET campaign_id = NULL WHERE campaign_id = $1", campaign_id)
    await pool.execute("DELETE FROM campaign_settings WHERE campaign_id = $1", campaign_id)
    await pool.execute("DELETE FROM exclusions WHERE campaign_id = $1", campaign_id)
    await pool.execute("DELETE FROM email_templates WHERE campaign_id = $1", campaign_id)
    await pool.execute("DELETE FROM campaigns WHERE id = $1", campaign_id)
