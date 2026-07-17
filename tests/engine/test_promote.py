"""Tests for ``jobcannon.engine.ats_scanner._promote.promote_ats_from_source_urls``.

Not a port: the private repo's tests/test_ats_scanner.py::TestPromoteAtsFromSourceUrls
drives the OLD pre-ScanServices implementation end-to-end (real identity-reconcile
writes, mocked ``ats_prober.requests.get``, DB assertions on ats_platform/
ats_slug/ats_probe_status). That machinery lives in
``job_finder.web.ats_identity_reconcile`` (host-owned, not ported — see the
identity-trio note in ``jobcannon/engine/services.py``'s module docstring),
so those specific assertions can't be reproduced here.

What CAN be tested at the engine layer is the seam itself: the ported
``promote_ats_from_source_urls`` facade in ``_promote.py`` is now a thin
wrapper around the optional ``ScanServices.promote_ats_scheduler_batch``
hook — skip cleanly when unset (fail-closed, matching every other optional
hook's contract), delegate and return its result when set. That contract had
zero direct functional coverage (test_scan_seam.py's
test_optional_hooks_default_to_skip only asserts the field is None on a
fresh bundle; it never calls the facade function itself), so these two tests
close that gap.
"""

from jobcannon.engine import services
from jobcannon.engine.ats_scanner._promote import promote_ats_from_source_urls


def _base_services(**overrides) -> services.ScanServices:
    kwargs: dict = dict(
        connection_factory=lambda **_kw: None,
        upsert_job=lambda *a, **k: None,
        set_jd_full=lambda *a, **k: None,
        upsert_company=lambda conn, name, *a, **k: 1,
        get_secret=lambda name, *, config=None: None,
        config={},
        jd_storage_max_chars=100_000,
    )
    kwargs.update(overrides)
    return services.ScanServices(**kwargs)


def test_promote_skips_when_hook_unset():
    """No ScanServices.promote_ats_scheduler_batch configured -> fail-closed
    skip dict, no attempt to reconcile/promote anything."""
    services.set_services(_base_services())
    try:
        result = promote_ats_from_source_urls("unused.db", {})
    finally:
        services.clear_services()

    assert result == {
        "checked": 0,
        "promoted": 0,
        "skipped": "promote_ats_scheduler_batch_unavailable",
    }


def test_promote_delegates_to_injected_hook_when_set():
    """When the host wires promote_ats_scheduler_batch, the facade calls it
    with (db_path, config) and returns its result verbatim."""
    calls: list = []

    def fake_promote_batch(db_path, config):
        calls.append((db_path, config))
        return {"checked": 3, "promoted": 1}

    services.set_services(_base_services(promote_ats_scheduler_batch=fake_promote_batch))
    try:
        result = promote_ats_from_source_urls("/tmp/jobs.db", {"ats": {}})
    finally:
        services.clear_services()

    assert calls == [("/tmp/jobs.db", {"ats": {}})]
    assert result == {"checked": 3, "promoted": 1}
