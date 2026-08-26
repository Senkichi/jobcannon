"""extraction_health recorder -> scan_health_log rows.

record(**kwargs) does NOT catch recorder exceptions (verified engine
behavior), so this recorder must never raise on the scan hot path: any
failure is swallowed to a WARN log.

The engine's only call site (jobcannon/engine/ats_platforms/_registry.py)
passes a `conn` kwarg carrying its OWN live engine connection alongside the
health payload fields (source, surface, payload, job_count, detect). That
connection object is not JSON-serializable and is not this recorder's to
use — the recorder opens its own factory connection to write the row, so
`conn` is popped from kwargs before building the jsonb payload.
"""

from __future__ import annotations

import json
import logging

from psycopg.types.json import Jsonb

from jobcannon.db.pool import commit_unless_nested, connection_factory

logger = logging.getLogger(__name__)


def record_scan_health(**kwargs) -> None:
    try:
        fields = dict(kwargs)
        fields.pop("conn", None)  # engine's live connection — ignore, never serialize
        payload = {k: _jsonable(v) for k, v in fields.items()}
        with connection_factory() as conn:
            conn.raw.execute(
                "INSERT INTO scan_health_log (payload) VALUES (%s)",
                (Jsonb(payload),),
            )
            commit_unless_nested(conn.raw)
    except Exception:
        logger.warning("scan_health recorder failed (swallowed)", exc_info=True)


def _jsonable(value):
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)
