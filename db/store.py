"""
PostgreSQL persistence layer.
Uses asyncpg with a connection pool. JSONB columns are auto-serialised via
a per-connection codec registered in create_pool().
"""

import json
from typing import Optional

import asyncpg


# ── Schema ────────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT         PRIMARY KEY,
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

CREATE TABLE IF NOT EXISTS exclusions (
    id         SERIAL      PRIMARY KEY,
    value      TEXT        NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
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
        await conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS company_size TEXT")
        await conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS industry TEXT")
        await conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS linkedin_url TEXT")
        await conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS company_website TEXT")
        await conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS signal_type TEXT")
        await conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS source_url TEXT")
        # Email columns
        await conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS email TEXT")
        await conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS email_sent BOOLEAN DEFAULT FALSE")
        await conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS email_sent_at TIMESTAMPTZ")
        # Email status on runs
        await conn.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS email_status TEXT DEFAULT 'not_sent'")


async def mark_stale_runs(pool: asyncpg.Pool) -> None:
    """Runs that were 'running' when the server last stopped never completed."""
    await pool.execute("UPDATE runs SET status='error' WHERE status='running'")


# ── Runs ──────────────────────────────────────────────────────────────────────

async def insert_run(
    pool: asyncpg.Pool, run_id: str, filename: str, concurrency: int
) -> None:
    await pool.execute(
        "INSERT INTO runs (run_id, filename, concurrency) VALUES ($1, $2, $3)",
        run_id, filename, concurrency,
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


async def list_runs(pool: asyncpg.Pool) -> list:
    rows = await pool.fetch(
        """
        SELECT r.run_id, r.filename, r.status, r.lead_count,
               r.created_at, r.completed_at, r.email_status,
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
            "filename":       r["filename"],
            "status":         r["status"],
            "lead_count":     max(r["lead_count"] or 0, r["n_total"] or 0),
            "n_qualified":    r["n_qualified"],
            "n_review":       r["n_review"],
            "n_disqualified": r["n_disqualified"],
            "created_at":     r["created_at"].isoformat() if r["created_at"] else None,
            "email_status":   r["email_status"] or "not_sent",
        }
        for r in rows
    ]


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
            evidence, qual_reasons, disqual_reasons
        ) VALUES (
            $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23,$24,$25
        )
        ON CONFLICT (run_id, lead_id) DO UPDATE SET
            company_size=$5, industry=$6, linkedin_url=$7, company_website=$8, signal_type=$10, source_url=$11,
            decision=$12, score=$13, confidence=$14,
            segment_fit=$15, buyer_fit=$16, pain_fit=$17,
            workflow_fit=$18, commercial_fit=$19,
            reasoning=$20, use_case=$21, signal_strength=$22,
            evidence=$23, qual_reasons=$24, disqual_reasons=$25
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
        "email":          row["email"] or "",
        "email_sent":     row["email_sent"] or False,
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


async def get_leads_for_email(pool: asyncpg.Pool, run_id: str, max_score: int) -> list:
    rows = await pool.fetch(
        """SELECT * FROM leads
             WHERE run_id=$1
               AND decision IN ('qualified','review')
               AND score <= $2
               AND email_sent = FALSE
             ORDER BY score DESC""",
        run_id, max_score,
    )
    return [_row_to_lead(r) for r in rows]


# ── Settings ──────────────────────────────────────────────────────────────────

_SETTINGS_DEFAULTS = {"qualified_threshold": "70", "disqualified_threshold": "60"}


async def get_settings(pool: asyncpg.Pool) -> dict:
    rows = await pool.fetch("SELECT key, value FROM settings")
    result = dict(_SETTINGS_DEFAULTS)
    for r in rows:
        result[r["key"]] = r["value"]
    return result


async def save_settings(pool: asyncpg.Pool, settings: dict) -> None:
    for key, value in settings.items():
        await pool.execute(
            """INSERT INTO settings(key, value) VALUES($1, $2)
               ON CONFLICT(key) DO UPDATE SET value=$2, updated_at=now()""",
            key, str(value),
        )


# ── Exclusions ────────────────────────────────────────────────────────────────

async def get_exclusions(pool: asyncpg.Pool) -> list:
    rows = await pool.fetch(
        "SELECT id, value FROM exclusions ORDER BY created_at DESC"
    )
    return [{"id": r["id"], "value": r["value"]} for r in rows]


async def add_exclusion(pool: asyncpg.Pool, value: str) -> None:
    await pool.execute(
        "INSERT INTO exclusions(value) VALUES($1) ON CONFLICT DO NOTHING",
        value.strip(),
    )


async def remove_exclusion(pool: asyncpg.Pool, exclusion_id: int) -> None:
    await pool.execute("DELETE FROM exclusions WHERE id=$1", exclusion_id)
