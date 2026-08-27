"""Guest demo seed (1B Wave 3 PR 11, day-1-stranger prerequisite): a sentinel
`users` row + a canned-but-realistic profile for GUEST_USER_ID, so `/demo`
has a profile card to render instead of the no-profile empty state.

The profile below is GENERIC, invented product-data-scientist content — not
any real person's career history (leak-guard: scripts/leak_guard.py scans
every tracked file for owner-identifying terms before push).

`seed(conn)` is idempotent by construction: the `users` insert is
`ON CONFLICT (id) DO NOTHING` and `upsert_profile` is itself an upsert, so
running this script twice leaves exactly one users row and one current
profile row — safe to re-run against production
after a schema change or a fresh deploy.

Usage (operator, from a shell with DATABASE_URL set):
    python scripts/seed_guest_demo.py
"""

from __future__ import annotations

import logging
from typing import Any

from jobcannon.db._profiles import GUEST_USER_ID, upsert_profile
from jobcannon.db.pool import commit_unless_nested

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("seed_guest_demo")

_GUEST_SKILLS = [
    "SQL",
    "Python",
    "A/B testing",
    "experimentation design",
    "dashboarding",
    "product analytics",
    "causal inference",
    "stakeholder communication",
]

_GUEST_EXPERIENCE_SUMMARY = (
    "Product-focused data scientist with six years across growth and core-product "
    "teams at consumer software companies. Owns the full loop from experiment design "
    "through analysis to shipped recommendation — funnel diagnostics, feature "
    "adoption studies, and pricing/packaging experiments. Comfortable pairing with "
    "engineering to instrument new surfaces from scratch."
)

_GUEST_TARGET_TITLES = ["Product Data Scientist", "Product Analyst", "Data Analyst"]
_GUEST_TARGET_LOCATIONS = ["Remote — US"]
_GUEST_SENIORITY_LEVEL = "senior"
_GUEST_YEARS_OF_EXPERIENCE = 6


def seed(conn: Any) -> None:
    raw = conn.raw if hasattr(conn, "raw") else conn
    raw.execute(
        "INSERT INTO users (id, plan_tier) VALUES (%s, 'free') ON CONFLICT (id) DO NOTHING",
        (GUEST_USER_ID,),
    )
    commit_unless_nested(raw)
    upsert_profile(
        conn,
        GUEST_USER_ID,
        skills=_GUEST_SKILLS,
        experience_summary=_GUEST_EXPERIENCE_SUMMARY,
        target_titles=_GUEST_TARGET_TITLES,
        target_locations=_GUEST_TARGET_LOCATIONS,
        seniority_level=_GUEST_SENIORITY_LEVEL,
        years_of_experience=_GUEST_YEARS_OF_EXPERIENCE,
        workplace_type=None,
    )
    log.info(
        "seeded guest demo user %r: %d skills, target_titles=%s, seniority=%s",
        GUEST_USER_ID,
        len(_GUEST_SKILLS),
        _GUEST_TARGET_TITLES,
        _GUEST_SENIORITY_LEVEL,
    )


def main() -> int:
    from jobcannon.engine import services
    from jobcannon.host.config import load_host_config
    from jobcannon.host.wiring import init_engine_seams, teardown_engine_seams

    host_config = load_host_config()
    init_engine_seams(host_config)
    try:
        svc = services.get_services()
        with svc.connection_factory() as conn:
            seed(conn)
    finally:
        teardown_engine_seams()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
