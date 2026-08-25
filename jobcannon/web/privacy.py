"""jobcannon/web/privacy.py — GET /privacy: a static placeholder page for
the site's privacy policy.

Public — added to jobcannon.web.PUBLIC_PATHS. The footer link in
base.html is unconditional (it renders on error_401.html for signed-out
visitors, same as the AGPL source link added for issue #22), and a
visitor deciding whether to sign up or grant analytics consent needs to
be able to read this before creating an account, not after. Making the
route authed-only would 401 the very link a pre-signup visitor clicks.

SCAFFOLD ONLY (issue #94): the page body is a clearly-marked placeholder.
No policy text has been ratified, and this route does not decide it. Two
questions stay open for a maintainer/legal pass:
  - the actual policy text (sub-processors, retention, what analytics
    are collected)
  - whether the posthog distinct_id sent by jobcannon.host.events should
    be treated as PII for policy purposes (#104 pseudonymizes the
    identifier itself at the call sites — a technical fact, not a legal
    characterization)
Do not add policy copy to this route or its template without a
maintainer/legal sign-off; this ships the mechanism, not the text.
"""

from __future__ import annotations

from flask import Blueprint, render_template

privacy_bp = Blueprint("privacy", __name__)


@privacy_bp.get("/privacy", strict_slashes=False)
def privacy():
    return render_template("privacy.html")
