"""GET /postings/<id>/detail — the expandable card's stateless fragment
(spec §3).

Public via the `public_get` per-view marker (jobcannon/web/__init__.py:122,
issue #171's mechanism), NOT via PUBLIC_PATHS and never a path-prefix
check: PUBLIC_PATHS is an exact-normalized-path frozenset consumed by three
coupled surfaces (clerk_auth's gate, the clerk-js loader gate, and the
issue-#193 Cache-Control hook) and cannot express a dynamic rule, while a
`/postings/` prefix exemption would also open GET /postings (the authed
history page) and the POST action routes. With the marker, POST on this
rule stays 405/401 territory and an authed visitor flows through the
normal identity path — harmless, because this fragment renders posting
content only, no user state, which is exactly what lets a row's
Save/Dismiss/Apply DOM survive expansion untouched.

The spec ratifies the exposure delta: any posting's jd_full becomes
fetchable by id enumeration with no auth (scraped-public content). No
events are logged here — per-row impression logging stays in
jobcannon/web/pages.py's authed feed route.

`comp_data_json` is handed to build_comp_context as a plain one-key dict:
that function reads via `.get()`, which the pooled HybridRow (a Sequence)
does not support.

Module-level names (`connection_factory`, `get_posting_detail`,
`build_comp_context`) are deliberate monkeypatch seams, matching every
other route module in this package.
"""

from __future__ import annotations

from flask import Blueprint, abort, render_template

from jobcannon.db._posting_detail import get_posting_detail
from jobcannon.db.pool import connection_factory
from jobcannon.engine.scoring_types import build_comp_context
from jobcannon.web import public_get

posting_detail_bp = Blueprint("posting_detail", __name__)


@posting_detail_bp.get("/postings/<int:posting_id>/detail")
@public_get
def detail(posting_id: int):
    with connection_factory() as conn:
        row = get_posting_detail(conn, posting_id)
    if row is None:
        abort(404)
    comp_context = None
    if row["comp_data_json"]:
        comp_context = build_comp_context({"comp_data_json": row["comp_data_json"]})
    return render_template("_posting_detail.html", row=row, comp_context=comp_context)
