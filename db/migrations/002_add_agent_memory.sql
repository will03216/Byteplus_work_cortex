-- Migration 002: Add agent memory foundation
-- Safe to run multiple times.

BEGIN;

ALTER TABLE runs ADD COLUMN IF NOT EXISTS campaign_version TEXT DEFAULT 'v1';
ALTER TABLE runs ADD COLUMN IF NOT EXISTS policy_version   TEXT DEFAULT 'v1';

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

COMMIT;
