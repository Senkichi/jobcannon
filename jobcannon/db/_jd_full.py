"""set_jd_full — Postgres port of the sole sanctioned jd_full writer.

Same 4-layer gate chain as the private original, with the two content gates
imported straight from the ENGINE (they were ported in Phase 1A):
  1. empty-text short-circuit
  2. HTML normalization (strip to text only when an HTML signal is present)
  3. I-13 density gate: jobcannon.engine.jd_content_contract._is_jd_junk
  4. I-17 content contract: jobcannon.engine.jd_content_contract.jd_content_reject
Returns True on write, False on any gate hit (no write).

Divergence from the private original, deliberate and Wave-1-scoped: no
score-invalidation side effect on content change — the hosted schema has no
per-posting LLM score tuple yet (structural axes are Wave-2 work and are
recomputed at ingest, not invalidated here). Revisit when owner-fit scoring
lands (Phase 2).

Transaction-boundary note (recorded port deviation, matches _companies.py /
_jobs.py): the write commits via pool.commit_unless_nested() rather than a
bare raw.commit() call, so this also works when `conn` is already inside an
ambient `with conn.transaction():` block (tests/host/conftest.py's db_conn
fixture) — psycopg3 forbids explicit commit() there. See that helper's
docstring for why a naive `with raw.transaction():` wrapper does NOT
substitute for a real commit here (verified empirically: it degrades to a
savepoint whenever the connection already carries an open, non-Transaction-
managed transaction from an earlier bare statement — the common case, since
this is called right after the engine's own bare
`SELECT jd_full FROM jobs ...` read in _run.py).

The UPDATE itself is additionally wrapped in its own `with raw.transaction():`
block purely for SAVEPOINT-based recovery (matches _jobs.py / _companies.py):
if the write raises, that block's __exit__ rolls back to the savepoint and
re-raises, leaving the connection usable for the caller's next statement
instead of stuck in Postgres's aborted-transaction state. commit_unless_nested()
still runs immediately after the block, unchanged.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from jobcannon.db.pool import commit_unless_nested
from jobcannon.engine.description_formatter import strip_html_to_text
from jobcannon.engine.jd_content_contract import _is_jd_junk, jd_content_reject

logger = logging.getLogger(__name__)

_HTML_SIGNAL_RE = re.compile(r"<\s*(p|div|br|li|ul|span|h\d)\b", re.IGNORECASE)


def set_jd_full(
    conn: Any,
    dedup_key: str,
    text: str | None,
    *,
    source: str,
    title: str | None = None,
    config: dict | None = None,
) -> bool:
    """Store a JD body on the posting, gated by the junk and content contracts.

    `config` is accepted (keyword-only) to pin the private chokepoint's full
    signature; it is threaded into the content-gate call by a follow-up
    change and is unused until then.
    """
    raw = conn.raw if hasattr(conn, "raw") else conn
    if not text:
        return False
    if _HTML_SIGNAL_RE.search(text):
        text = strip_html_to_text(text)
    if _is_jd_junk(text):
        logger.warning("set_jd_full: junk-gated [source=%s] prefix=%r", source, text.strip()[:60])
        return False
    rejection = jd_content_reject(text, title)
    if rejection is not None:
        logger.warning(
            "set_jd_full: content-gated [source=%s] reason=%s signal=%s prefix=%r",
            source,
            rejection[0],
            rejection[1],
            text.strip()[:60],
        )
        return False
    with raw.transaction():
        raw.execute("UPDATE postings SET jd_full = %s WHERE dedup_key = %s", (text, dedup_key))
    commit_unless_nested(raw)
    return True
