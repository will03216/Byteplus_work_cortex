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

CREATE TABLE IF NOT EXISTS email_templates (
    id         SERIAL      PRIMARY KEY,
    vertical   TEXT        NOT NULL,
    name       TEXT        NOT NULL,
    subject    TEXT        NOT NULL,
    body       TEXT        NOT NULL,
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


async def seed_email_templates(pool: asyncpg.Pool) -> None:
    count = await pool.fetchval("SELECT COUNT(*) FROM email_templates")
    if count and count > 0:
        return
    for t in _SEED_TEMPLATES:
        await pool.execute(
            "INSERT INTO email_templates (vertical, name, subject, body) VALUES ($1,$2,$3,$4)",
            t["vertical"], t["name"], t["subject"], t["body"],
        )


async def get_email_templates(pool: asyncpg.Pool) -> list:
    rows = await pool.fetch(
        "SELECT id, vertical, name, subject, body, created_at FROM email_templates ORDER BY vertical, id"
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
    pool: asyncpg.Pool, vertical: str, name: str, subject: str, body: str
) -> dict:
    row = await pool.fetchrow(
        """INSERT INTO email_templates (vertical, name, subject, body)
           VALUES ($1,$2,$3,$4)
           RETURNING id, vertical, name, subject, body, created_at""",
        vertical, name, subject, body,
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
