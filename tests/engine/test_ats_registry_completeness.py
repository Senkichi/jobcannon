"""Engine-native ATS registry completeness guard.

The private repo's test_ats_registry_completeness.py imports
job_finder.web.ats_reconciler (stays private, in no task's scope) and
ats_scanner._probe/_run_playwright (Task 3 modules), so it cannot port
verbatim. This is a trimmed rewrite using only Task 1 + Task 2 symbols,
covering the same invariants that matter at this layer: every scanner has
a registry entry, every probe-dispatch string resolves against ats_prober,
and non-scannable bookkeeping is internally consistent.
"""

import jobcannon.engine.ats_prober as prober
import jobcannon.engine.ats_registry as ar


def test_every_scanner_has_a_platform_spec():
    for name, scanner in ar.SCANNERS_BY_NAME.items():
        assert name in ar.PLATFORMS, f"{name} in SCANNERS_BY_NAME but missing from PLATFORMS"
        assert ar.PLATFORMS[name].requests_scanner is scanner, (
            f"{name}: SCANNERS_BY_NAME entry is not PLATFORMS[{name}].requests_scanner"
        )


def test_every_playwright_platform_has_a_platform_spec():
    for name in ar.PLAYWRIGHT_PLATFORMS:
        assert name in ar.PLATFORMS, f"{name} in PLAYWRIGHT_PLATFORMS but missing from PLATFORMS"
        spec = ar.PLATFORMS[name]
        assert isinstance(spec.playwright_scanner, ar.PlaywrightPlatformScanner), (
            f"{name}: PLAYWRIGHT_PLATFORMS entry has no PlaywrightPlatformScanner"
        )
        assert ar.PLAYWRIGHT_SCANNERS.get(name) is spec.playwright_scanner, (
            f"{name}: PLAYWRIGHT_SCANNERS entry does not match PLATFORMS[{name}].playwright_scanner"
        )


def test_probe_attrs_resolve_against_ats_prober():
    for name, spec in ar.PLATFORMS.items():
        for attr_field in ("probe_attr", "identity_probe_attr"):
            attr = getattr(spec, attr_field)
            if attr is None:
                continue
            assert hasattr(prober, attr), (
                f"{name}.{attr_field} = {attr!r} does not resolve on jobcannon.engine.ats_prober"
            )
            assert callable(getattr(prober, attr)), (
                f"{name}.{attr_field} = {attr!r} resolves but is not callable"
            )


def test_non_scannable_platforms_set_matches_spec_field():
    for name, spec in ar.PLATFORMS.items():
        assert spec.non_scannable == (name in ar.NON_SCANNABLE_PLATFORMS), (
            f"{name}: PlatformSpec.non_scannable={spec.non_scannable} but "
            f"membership in NON_SCANNABLE_PLATFORMS={name in ar.NON_SCANNABLE_PLATFORMS}"
        )


def test_non_scannable_platforms_have_no_dispatchable_scanner():
    """A platform stays out of both SCANNERS_BY_NAME and PLAYWRIGHT_PLATFORMS
    only when it has neither a requests_scanner nor a playwright_scanner —
    platforms with a scanner but flagged non_scannable (e.g. jobvite, google)
    are a deliberate, separate category (probable but not full-board-scannable)
    and are exempted here."""
    for name, spec in ar.PLATFORMS.items():
        if spec.requests_scanner is None and spec.playwright_scanner is None:
            assert name not in ar.SCANNERS_BY_NAME
            assert name not in ar.PLAYWRIGHT_PLATFORMS
