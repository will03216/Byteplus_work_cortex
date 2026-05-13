-- Migration 003: Add campaign and policy version control
-- Safe to run multiple times.

BEGIN;

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

COMMIT;
