"""Migration 1 — Wave-1 hosted schema (1B spec §3.3, corrected; pgvector deferred to Wave 2).

byo_key_credentials carries ENABLE + FORCE ROW LEVEL SECURITY with zero
policies defined. That is deliberate default-deny for ALL roles, including
the table owner — FORCE closes the normal RLS owner-bypass, and superusers
aside (who always bypass RLS regardless of FORCE), nothing can read or
write this table until the Phase-2 BYO-key feature adds the user-scoped
policies that carve out access.
"""

from __future__ import annotations

from jobcannon.db.migrations.types import Migration

MIGRATION = Migration(
    version=1,
    description="initial hosted schema: shared corpus + per-user tables (no pgvector yet)",
    sql=[
        """
        CREATE TABLE companies (
            id                bigserial PRIMARY KEY,
            name              text NOT NULL UNIQUE,
            ats_platform      text,
            ats_slug          text,
            ats_probe_status  text NOT NULL DEFAULT 'pending'
                              CHECK (ats_probe_status IN ('pending','hit','miss')),
            homepage_url      text,
            careers_url       text,
            scan_enabled      boolean NOT NULL DEFAULT true,
            last_scanned_at   timestamptz,
            consecutive_empty_scans integer NOT NULL DEFAULT 0,
            created_at        timestamptz NOT NULL DEFAULT now(),
            updated_at        timestamptz NOT NULL DEFAULT now(),
            UNIQUE (ats_platform, ats_slug),
            CHECK (ats_probe_status <> 'hit' OR (ats_platform IS NOT NULL AND ats_slug IS NOT NULL))
        )
        """,
        """
        CREATE TABLE postings (
            id                        bigserial PRIMARY KEY,
            dedup_key                 text NOT NULL UNIQUE,
            company_id                bigint NOT NULL REFERENCES companies(id),
            title                     text NOT NULL,
            company                   text NOT NULL,
            location                  text,
            locations_raw             jsonb NOT NULL DEFAULT '[]',
            locations_structured      jsonb,
            workplace_type            text,
            primary_country_code      text,
            sources                   jsonb NOT NULL DEFAULT '[]',
            source_urls               jsonb NOT NULL DEFAULT '[]',
            source_id                 text,
            sightings                 jsonb NOT NULL DEFAULT '[]',
            description               text,
            jd_full                   text,
            salary_min                numeric,
            salary_max                numeric,
            salary_currency           text NOT NULL DEFAULT 'USD'
                CHECK (salary_currency IN ('USD','GBP','EUR','CAD','AUD','INR','SGD','UNKNOWN')),
            salary_period             text NOT NULL DEFAULT 'unknown'
                CHECK (salary_period IN ('annual','hourly','monthly','unknown')),
            salary_observations       jsonb NOT NULL DEFAULT '[]',
            posted_date               date,
            posted_date_precision     text
                CHECK (posted_date_precision IN ('exact','approximate','proxy')),
            first_seen                timestamptz NOT NULL DEFAULT now(),
            last_seen                 timestamptz NOT NULL DEFAULT now(),
            is_stale                  boolean NOT NULL DEFAULT false,
            expiry_status             text,
            direct_url                text,
            ats_platform              text,
            employment_type           text,
            is_remote                 boolean,
            department                text,
            comp_data_json            text,
            ats_refreshed_at          timestamptz,
            unresolved_reasons        jsonb NOT NULL DEFAULT '[]',
            structural_axes           jsonb,
            structural_scoring_method text,
            structural_scored_at      timestamptz,
            embedding_model_version   text,
            CHECK ((posted_date IS NULL) = (posted_date_precision IS NULL))
        )
        """,
        "CREATE INDEX idx_postings_company_source ON postings(company_id, source_id)",
        "CREATE INDEX idx_postings_last_seen ON postings(last_seen)",
        """
        CREATE TABLE users (
            id          text PRIMARY KEY,
            email       text,
            plan_tier   text NOT NULL DEFAULT 'free',
            created_at  timestamptz NOT NULL DEFAULT now()
        )
        """,
        """
        CREATE TABLE profiles (
            user_id             text PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            skills              jsonb,
            experience_summary  text,
            target_titles       jsonb,
            target_locations    jsonb,
            seniority_level     text,
            years_of_experience numeric,
            updated_at          timestamptz NOT NULL DEFAULT now()
        )
        """,
        """
        CREATE TABLE feed_state (
            user_id        text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            posting_id     bigint NOT NULL REFERENCES postings(id),
            owner_fit_axes jsonb,
            rank_score     double precision,
            ranker_version text,
            computed_at    timestamptz,
            PRIMARY KEY (user_id, posting_id)
        )
        """,
        """
        CREATE TABLE watchlists (
            id          bigserial PRIMARY KEY,
            user_id     text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            posting_id  bigint REFERENCES postings(id),
            company_id  bigint REFERENCES companies(id),
            notes       text,
            created_at  timestamptz NOT NULL DEFAULT now(),
            CHECK ((posting_id IS NOT NULL)::int + (company_id IS NOT NULL)::int = 1)
        )
        """,
        "CREATE UNIQUE INDEX watchlists_user_posting_uq ON watchlists(user_id, posting_id) WHERE posting_id IS NOT NULL",
        "CREATE UNIQUE INDEX watchlists_user_company_uq ON watchlists(user_id, company_id) WHERE company_id IS NOT NULL",
        """
        CREATE TABLE pipeline_status (
            user_id           text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            posting_id        bigint NOT NULL REFERENCES postings(id),
            status            text NOT NULL,
            status_changed_at timestamptz NOT NULL DEFAULT now(),
            applied_at        timestamptz,
            notes             text,
            PRIMARY KEY (user_id, posting_id)
        )
        """,
        """
        CREATE TABLE events (
            id                       bigserial PRIMARY KEY,
            user_id                  text REFERENCES users(id) ON DELETE CASCADE,
            event_type               text NOT NULL,
            posting_id               bigint REFERENCES postings(id),
            feed_position            integer,
            ranker_version           text,
            feed_session_id          text,
            interleave_experiment_id text,
            interleave_team          text CHECK (interleave_team IN ('A','B')),
            occurred_at              timestamptz NOT NULL DEFAULT now(),
            payload                  jsonb
        )
        """,
        """
        CREATE TABLE byo_key_credentials (
            user_id       text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            provider      text NOT NULL,
            encrypted_key bytea NOT NULL,
            is_active     boolean NOT NULL DEFAULT true,
            created_at    timestamptz NOT NULL DEFAULT now(),
            last_used_at  timestamptz,
            PRIMARY KEY (user_id, provider)
        )
        """,
        "ALTER TABLE byo_key_credentials ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE byo_key_credentials FORCE ROW LEVEL SECURITY",
        """
        CREATE TABLE company_scan_log (
            id                   bigserial PRIMARY KEY,
            company_id           bigint REFERENCES companies(id),
            scanned_at           timestamptz NOT NULL DEFAULT now(),
            jobs_found           integer,
            skipped_title_filter integer,
            error                text
        )
        """,
    ],
)
