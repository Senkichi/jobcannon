"""Helper for the ats_platforms shared-``requests.Session`` test seam.

Every ``ats_platforms`` consuming module (``_platforms_workday``, ``_registry``,
``_detail_fetchers``, etc.) does ``from jobcannon.engine.ats_platforms._http_session
import get_session`` — a *local* name binding. That means intercepting a
platform's HTTP call requires patching ``<consuming module>.get_session``, not
``jobcannon.engine.ats_platforms._http_session.get_session``: by the time a test
patches the latter, the consuming module already holds its own reference to the
original function and never looks the name up again.

Patch the consuming module's ``get_session`` attribute (decorator or
``with``-block, whichever the test already uses), then pull the verb-level
mock off the fake session's ``return_value`` with `ats_session_method` so the
test's existing response-fixture code (``mock_x.return_value = ...``,
``mock_x.side_effect = ...``, ``mock_x.assert_called_once_with(...)``) keeps
working unchanged:

    @patch("jobcannon.engine.ats_platforms._platforms_workday.get_session")
    def test_x(self, mock_post):
        mock_post = ats_session_method(mock_post, "post")
        mock_post.return_value = MagicMock(status_code=200, ...)
        ...

    with patch("jobcannon.engine.ats_platforms._registry.get_session") as mock_get:
        mock_get = ats_session_method(mock_get, "get")
        mock_get.return_value = fake
        ...

Do NOT patch ``requests.get``/``.post``/``.head`` directly against any
``ats_platforms`` module — that seam went dead when the package was converted
to the shared pooled Session (see ``_http_session.py``); a mock there
intercepts nothing and the test silently falls through to a real network call
against a live vendor ATS API. ``tests/test_ats_http_session_guard.py`` guards
against that regression.
"""

from __future__ import annotations

from unittest.mock import MagicMock


def ats_session_method(mock_get_session: MagicMock, method: str = "get") -> MagicMock:
    """Return the ``.get``/``.post``/``.head`` mock off a patched ``get_session``.

    ``mock_get_session`` is the mock that ``unittest.mock.patch`` substituted
    for a consuming module's ``get_session`` callable. Calling it (as
    production code does via ``get_session().get(...)``) returns
    ``mock_get_session.return_value``, a stand-in ``requests.Session``; this
    returns the named HTTP-verb mock off that stand-in so callers configure it
    exactly like the old bare ``requests.get``/``.post`` mock.
    """
    return getattr(mock_get_session.return_value, method)
