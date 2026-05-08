-- Migration 001: Add multi-campaign support
-- Safe to run multiple times (all statements use IF NOT EXISTS / ON CONFLICT DO NOTHING).
-- Existing Seedance data is preserved: all existing runs, leads, settings,
-- exclusions, and email_templates are backfilled to campaign_id = 'seedance'.

BEGIN;

-- 1. Campaign registry
CREATE TABLE IF NOT EXISTS campaigns (
    id          TEXT        PRIMARY KEY,
    name        TEXT        NOT NULL,
    description TEXT,
    is_active   BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 2. Seed known campaigns (idempotent)
INSERT INTO campaigns (id, name, description)
VALUES
    ('seedance', 'Seedance', 'ByteDance AI video generation platform'),
    ('kling',    'Kling AI',  'Kuaishou cinematic AI video generation platform')
ON CONFLICT (id) DO NOTHING;

-- 3. Add campaign_id to runs (nullable first for safe backfill)
ALTER TABLE runs ADD COLUMN IF NOT EXISTS campaign_id TEXT REFERENCES campaigns(id);

-- 4. Backfill all existing runs to seedance
UPDATE runs SET campaign_id = 'seedance' WHERE campaign_id IS NULL;

-- 5. Add campaign_id to settings (new composite PK approach via new table)
CREATE TABLE IF NOT EXISTS campaign_settings (
    campaign_id TEXT        NOT NULL REFERENCES campaigns(id),
    key         TEXT        NOT NULL,
    value       TEXT        NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (campaign_id, key)
);

-- 6. Migrate existing settings rows to campaign_settings for seedance
INSERT INTO campaign_settings (campaign_id, key, value, updated_at)
SELECT 'seedance', key, value, updated_at FROM settings
ON CONFLICT (campaign_id, key) DO NOTHING;

-- 7. Add campaign_id to exclusions
ALTER TABLE exclusions ADD COLUMN IF NOT EXISTS campaign_id TEXT REFERENCES campaigns(id);
UPDATE exclusions SET campaign_id = 'seedance' WHERE campaign_id IS NULL;

-- 8. Add campaign_id to email_templates
ALTER TABLE email_templates ADD COLUMN IF NOT EXISTS campaign_id TEXT REFERENCES campaigns(id);
UPDATE email_templates SET campaign_id = 'seedance' WHERE campaign_id IS NULL;

-- 9. Add index on runs.campaign_id for filtered queries
CREATE INDEX IF NOT EXISTS idx_runs_campaign ON runs(campaign_id);

COMMIT;
