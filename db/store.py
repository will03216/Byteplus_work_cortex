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

CREATE TABLE IF NOT EXISTS skill_files (
    id          SERIAL      PRIMARY KEY,
    campaign_id TEXT        NOT NULL,
    filename    TEXT        NOT NULL,
    content     TEXT        NOT NULL DEFAULT '',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (campaign_id, filename)
);
CREATE INDEX IF NOT EXISTS idx_skill_files_campaign ON skill_files(campaign_id);
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
        await conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS manual_email_status TEXT")
        # Email status on runs
        await conn.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS email_status TEXT DEFAULT 'not_sent'")
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
) -> None:
    await pool.execute(
        "INSERT INTO runs (run_id, campaign_id, filename, concurrency) VALUES ($1, $2, $3, $4)",
        run_id, campaign_id, filename, concurrency,
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
                   r.created_at, r.completed_at, r.email_status,
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
            "campaign_id":    r["campaign_id"] or "seedance",
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
        "email":                row["email"] or "",
        "email_sent":           row["email_sent"] or False,
        "manual_email_status":  row["manual_email_status"] or None,
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

    # 1. Remove DB rows for campaign_ids whose directory no longer exists on disk
    disk_ids = {d.name for d in root.iterdir() if d.is_dir()}
    db_ids = set(await list_skill_campaign_ids(pool))
    orphaned = db_ids - disk_ids
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
