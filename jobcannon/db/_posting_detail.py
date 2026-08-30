"""get_posting_detail — the detail fragment's single-posting read (spec §3).

`SELECT *` on purpose, mirroring how jobcannon/db/_jd_full.py and the
engine's scoring path treat full postings rows (this repo has no
JOBS_ALL_COLUMNS-style projection to reuse — see _jd_full.py's
row-projection note): the detail view exists precisely to reach the heavy
columns (`jd_full`, `description`, `comp_data_json`, `locations_structured`,
`sightings`, the structural axes) that `list_feed_postings`' projection
deliberately excludes. Read-only, no user state, no transaction needed.
"""

from __future__ import annotations

from typing import Any


def get_posting_detail(conn: Any, posting_id: int) -> Any:
    """The full postings row for `posting_id`, or None when no such row
    exists (the route 404s)."""
    raw = conn.raw if hasattr(conn, "raw") else conn
    return raw.execute("SELECT * FROM postings WHERE id = %s", (posting_id,)).fetchone()
