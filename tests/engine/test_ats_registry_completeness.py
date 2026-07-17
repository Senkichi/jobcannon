"""Completeness + parity guards for the unified ATS platform registry.

These turn the class of bug that motivated ``ats_registry`` into CI failures:
a platform with a working scanner + probe but missing from the liveness
dispatch (iCIMS / oracle_cloud / ultipro fell into exactly this gap, failing
``_verify_live`` 89% of the time). Each invariant is exemptable ONLY via an
explicit capability flag on the spec — never a hardcoded skip-list.

The PARITY tests pin the registry's derived views to the legacy hand-maintained
literals that still live in their old modules, so the incremental consumer
migration (later PRs delete those literals) is provably behaviour-preserving.

Engine-native port of tests/test_ats_registry_completeness.py. Now that
``ats_scanner._probe`` and ``ats_scanner._run_playwright`` (Task 3) are
ported, this file is near-verbatim — earlier in this PR a trimmed rewrite
lived at this path (committed before Task 3 landed) claiming those two
modules "cannot port verbatim"; that rewrite is superseded by this fuller
port now that its blocker is gone.

Two tests are skipped, both because the module they depend on has no engine
equivalent (out of this PR's scope):

- ``test_parity_reconcilable`` needs ``ats_reconciler`` (private-repo-only —
  no engine port).
- ``test_expiry_checker_excludes_workday_and_smartrecruiters`` needs
  ``expiry_checker`` (also private-repo-only). Its sibling
  ``test_equivalence_expiry_checker_posting_id_patterns`` looks like it
  needs the same module but does not: like
  ``test_equivalence_reconciler_posting_id_patterns``, it only compares
  ``ats_registry.EXPIRY_CHECKER_POSTING_ID_PATTERNS`` against a hardcoded
  golden-baseline dict copied from the legacy module's docstring, so it
  ports unchanged.

Neither ``ats_reconciler`` nor ``expiry_checker`` is imported at module level
here — both source-file imports are dropped entirely so a bare import of an
unported module can't break collection of the rest of the file.
"""

import pytest

import jobcannon.engine.ats_prober as ats_prober
from jobcannon.engine import ats_platforms, ats_registry
from jobcannon.engine.ats_scanner import _probe, _run_playwright

PLATFORMS = ats_registry.PLATFORMS
PROBE_PLATFORMS = sorted(n for n, s in PLATFORMS.items() if s.probe_attr is not None)
IDENTITY_PROBE_PLATFORMS = sorted(
    n for n, s in PLATFORMS.items() if s.identity_probe_attr is not None
)


# --------------------------------------------------------------------------- #
# Completeness guards — half-wiring becomes a red build.                       #
# --------------------------------------------------------------------------- #
def test_scannable_platform_has_probe_or_explicit_exemption():
    """Every platform with a fetch transport must be liveness-verifiable, OR
    declare an explicit exemption (keyword_adapter / non_scannable)."""
    offenders = [
        n
        for n in ats_registry.SCANNABLE_PLATFORMS
        if PLATFORMS[n].probe_attr is None
        and not PLATFORMS[n].keyword_adapter
        and not PLATFORMS[n].non_scannable
    ]
    assert not offenders, (
        f"scannable platforms with no probe and no explicit exemption: {offenders}. "
        "Add a _probe_* + probe_attr, or set keyword_adapter/non_scannable."
    )


def test_every_probe_attr_resolves_and_dispatches():
    """Each spec.probe_attr names a real ats_prober function AND verify_live
    routes to it. This is the exact regression that killed iCIMS/oracle/ultipro."""
    for name in PROBE_PLATFORMS:
        attr = PLATFORMS[name].probe_attr
        assert hasattr(ats_prober, attr), f"{name}: ats_prober.{attr} does not exist"
        assert callable(getattr(ats_prober, attr)), f"{name}: ats_prober.{attr} not callable"


@pytest.mark.parametrize("name", PROBE_PLATFORMS)
def test_verify_live_dispatches_each_probe(name, monkeypatch):
    """verify_live(platform, slug) must call the platform's probe and return its
    result — for EVERY probe platform, including icims/oracle_cloud/ultipro."""
    attr = PLATFORMS[name].probe_attr
    monkeypatch.setattr(ats_prober, attr, lambda slug: True)
    assert ats_registry.verify_live(name, "any-slug") is True
    monkeypatch.setattr(ats_prober, attr, lambda slug: False)
    assert ats_registry.verify_live(name, "any-slug") is False


def test_every_identity_probe_attr_resolves_and_dispatches():
    """The identity-probe analog of test_every_probe_attr_resolves_and_dispatches:
    each spec.identity_probe_attr must name a real, callable ats_prober function.
    Without this, a typo'd attr dispatches uncaught through probe_board_identity
    (no try/except at any layer) and aborts the batch reconcile run calling it."""
    for name in IDENTITY_PROBE_PLATFORMS:
        attr = PLATFORMS[name].identity_probe_attr
        assert hasattr(ats_prober, attr), f"{name}: ats_prober.{attr} does not exist"
        assert callable(getattr(ats_prober, attr)), f"{name}: ats_prober.{attr} not callable"


@pytest.mark.parametrize("name", IDENTITY_PROBE_PLATFORMS)
def test_probe_board_identity_dispatches_each_platform(name, monkeypatch):
    """probe_board_identity(platform, slug) must call the platform's identity
    probe and return its result — for EVERY identity-probe platform, not just
    the two hardcoded by name in test_ats_identity_probe.py. A future platform
    wired with a typo'd attr fails this generically, the same structural
    guarantee test_every_probe_attr_resolves_and_dispatches gives probe_attr."""
    attr = PLATFORMS[name].identity_probe_attr
    monkeypatch.setattr(ats_prober, attr, lambda slug: "Board Name")
    assert ats_registry.probe_board_identity(name, "any-slug") == "Board Name"
    monkeypatch.setattr(ats_prober, attr, lambda slug: None)
    assert ats_registry.probe_board_identity(name, "any-slug") is None


@pytest.mark.parametrize("name", ["icims", "oracle_cloud", "ultipro"])
def test_regression_new_platforms_are_verifiable(name, monkeypatch):
    """The motivating bug: these had scanners + probes but verify_live returned
    False for them. Guard that they now dispatch."""
    assert name in PROBE_PLATFORMS, f"{name} lost its probe_attr"
    monkeypatch.setattr(ats_prober, PLATFORMS[name].probe_attr, lambda slug: True)
    assert ats_registry.verify_live(name, "slug") is True


def test_verify_live_false_for_keyword_adapters_and_unknown():
    """Keyword adapters have no probe; unknown platforms are not in the registry.
    Both must return False (never raise)."""
    for name in ats_registry.KEYWORD_ADAPTER_PLATFORMS:
        assert ats_registry.verify_live(name, "slug") is False
    assert ats_registry.verify_live("not_a_platform", "slug") is False


def test_fetch_dispatch_coverage_matches_scanner_registries():
    """Identity guard (the #536 class): the registry's fetch views must equal the
    authoritative scanner registries — no scanner silently dropped from dispatch."""
    assert set(ats_registry.SCANNERS_BY_NAME) == set(ats_platforms.SCANNERS_BY_NAME)
    assert set(ats_registry.PLAYWRIGHT_SCANNERS) == set(_run_playwright._PLAYWRIGHT_SCANNERS)


def test_speculative_and_fp_prone_are_disjoint():
    """Replaces the runtime assert at _probe.py: a platform is never both
    speculative-safe and false-positive-prone."""
    both = [n for n, s in PLATFORMS.items() if s.speculative_safe and s.fp_prone]
    assert not both, f"platforms both speculative_safe and fp_prone: {both}"


def test_fp_prone_are_evidence_only_but_url_fastpath():
    """FP-prone platforms must be excluded from speculation yet reachable via the
    URL-evidence fast-path (the documented promotion route for them)."""
    for n, s in PLATFORMS.items():
        if s.fp_prone:
            assert not s.speculative_safe, f"{n} fp_prone must not be speculative_safe"
            assert s.url_fastpath, f"{n} fp_prone must remain url_fastpath-eligible"


def test_non_scannable_excluded_from_url_fastpath():
    """Generalized jobvite carve-out: a non-scannable stub must not be promotable
    via the fast-path (kept at 'miss' so careers_crawler owns it)."""
    for n, s in PLATFORMS.items():
        if s.non_scannable:
            assert not s.url_fastpath, f"{n} non_scannable must not be url_fastpath"


def test_jd_fetch_priority_is_unique_and_contiguous():
    """jd_fetch_priority ranks the JD-fetch order (domain_priority()'s ATS portion,
    lower = higher priority). A collision or gap would silently produce an unstable
    or wrong PRIORITY_DOMAINS_ATS ordering -- guard the invariant structurally so a
    future platform addition can't introduce either without a red build."""
    priorities = sorted(
        s.jd_fetch_priority for s in PLATFORMS.values() if s.jd_fetch_priority is not None
    )
    assert len(priorities) == len(set(priorities)), (
        f"duplicate jd_fetch_priority values: {priorities}"
    )
    assert priorities == list(range(len(priorities))), (
        f"jd_fetch_priority must be contiguous starting at 0, got: {priorities}"
    )


def test_redirect_domains_requires_domains():
    """A platform can't declare a subdomain-qualified redirect pattern without also
    being a recognized bare-domain ATS -- redirect_domains is a refinement of
    domains, not an independent facet. Guards against a future platform adding
    redirect_domains while forgetting domains (the exact half-wiring class this
    registry exists to prevent)."""
    offenders = [n for n, s in PLATFORMS.items() if s.redirect_domains and not s.domains]
    assert not offenders, f"platforms with redirect_domains but no domains: {offenders}"


def test_jd_fetch_domain_requires_jd_fetch_priority():
    """jd_fetch_domain and jd_fetch_priority are a pair (the exact string
    PRIORITY_DOMAINS ranks, and its rank) -- one without the other is a half-wired
    spec that would silently drop the platform from PRIORITY_DOMAINS_ATS or rank
    an empty string."""
    offenders = [
        n
        for n, s in PLATFORMS.items()
        if (s.jd_fetch_domain is not None) != (s.jd_fetch_priority is not None)
    ]
    assert not offenders, (
        f"platforms with only one of jd_fetch_domain/jd_fetch_priority set: {offenders}"
    )


def test_keyword_adapter_shape():
    """A keyword adapter has a (requests) scanner but no slug-probe — the explicit
    capability that exempts it from the scannable-must-have-probe guard."""
    for n in ats_registry.KEYWORD_ADAPTER_PLATFORMS:
        s = PLATFORMS[n]
        assert s.probe_attr is None, f"{n} keyword_adapter must have no probe_attr"
        assert s.requests_scanner is not None, f"{n} keyword_adapter must have a scanner"


# --------------------------------------------------------------------------- #
# Parity guards — registry views reproduce the legacy literals byte-for-byte.  #
# Delete each parity test in the PR that removes its legacy literal.           #
# --------------------------------------------------------------------------- #
def test_parity_fp_prone():
    assert ats_registry.FP_PRONE_PLATFORMS == _probe._FP_PRONE_PLATFORMS


def test_parity_url_fastpath():
    assert ats_registry.URL_FASTPATH_PLATFORMS == _probe._URL_FASTPATH_PLATFORMS


def test_parity_speculative_ladder_order():
    assert [n for n, _ in ats_registry.SPECULATIVE_PROBES] == [n for n, _ in _probe._PROBES]


@pytest.mark.skip(reason="ats_reconciler not ported to the engine, no equivalent")
def test_parity_reconcilable():
    assert ats_registry.RECONCILABLE_PLATFORMS == ats_reconciler._RECONCILABLE_PLATFORMS  # noqa: F821


def test_parity_scanner_registry():
    assert set(ats_registry.SCANNERS_BY_NAME) == set(ats_platforms.SCANNERS_BY_NAME)


def test_non_scannable_derivation():
    # Derived from each spec's `non_scannable` flag — the registered stubs with no
    # public API. Was a hand-maintained frozenset in ats_platforms (now deleted).
    # Includes jobvite/google (no public API) plus taleo/kronos/modernloop/governmentjobs
    # (ATS domains with no scanner/probe, used only for email-sender/pipeline-signal matching).
    assert (
        frozenset({"jobvite", "google", "taleo", "kronos", "modernloop", "governmentjobs"})
        == ats_registry.NON_SCANNABLE_PLATFORMS
    )


def test_scannable_target_platforms():
    # Promotion-target set for careers-link discovery = scannable minus the
    # non-scannable stubs. A real requests scanner and the Playwright-only iCIMS
    # are promotable; the stubs never are. Replaces _ats_link_discovery's
    # hand-rolled _TARGET_PLATFORMS.
    targets = ats_registry.SCANNABLE_TARGET_PLATFORMS
    assert targets == ats_registry.SCANNABLE_PLATFORMS - ats_registry.NON_SCANNABLE_PLATFORMS
    for p in ("greenhouse", "lever", "icims"):
        assert p in targets
    for stub in ("jobvite", "google"):
        assert stub not in targets


def test_parity_playwright_platforms():
    assert ats_registry.PLAYWRIGHT_PLATFORMS == _run_playwright.PLAYWRIGHT_PLATFORMS


def test_parity_verify_fastpath_dispatch(monkeypatch):
    """The registry SSOT and the PRODUCTION fast-path caller (_probe._verify_fastpath_live,
    invoked at the B2 promotion write) must dispatch identically for every url_fastpath
    platform, and both must gate non-fast-path platforms to False.

    This test's docstring long CLAIMED parity with ``_probe._verify_fastpath_live`` but only
    ever called ``ats_registry.verify_fastpath_live`` — so it was blind to the exact drift it
    named: ``successfactors``/``adp`` were parity-forced into ``_URL_FASTPATH_PLATFORMS`` but
    never got a branch in the old hand-maintained if/elif ladder, silently returning False and
    killing their careers-URL fast-path promotion. Now that _verify_fastpath_live delegates to
    the registry the two are equal by construction; exercising BOTH here keeps it that way —
    any re-introduced ladder that drops a fast-path platform fails this test immediately."""
    for name in ats_registry.URL_FASTPATH_PLATFORMS:
        attr = PLATFORMS[name].probe_attr
        monkeypatch.setattr(ats_prober, attr, lambda slug: True)
        assert ats_registry.verify_fastpath_live(name, "s") is True, name
        assert _probe._verify_fastpath_live(name, "s") is True, name
        monkeypatch.setattr(ats_prober, attr, lambda slug: False)
        assert ats_registry.verify_fastpath_live(name, "s") is False, name
        assert _probe._verify_fastpath_live(name, "s") is False, name
    # platforms NOT in the fast-path set must gate to False even with a live probe
    for name in ("oracle_cloud", "ultipro", "icims", "jobvite"):
        attr = PLATFORMS[name].probe_attr
        monkeypatch.setattr(ats_prober, attr, lambda slug: True)
        assert ats_registry.verify_fastpath_live(name, "s") is False, name
        assert _probe._verify_fastpath_live(name, "s") is False, name


def test_parity_url_detection_order():
    """The registry's URL_DETECTION_ORDER must preserve the exact resolution order
    of the legacy extract_ats_from_url_best if-ladder. A silent reorder is the failure
    mode this registry exists to prevent — this test captures the current order byte-for-byte."""
    from jobcannon.engine.ats_detection import extract_ats_from_url_best

    # Representative URLs that exercise each branch in the legacy if-ladder
    test_urls = [
        ("https://api.lever.co/v0/postings/abc123", "lever", "abc123", 10),
        ("https://boards-api.greenhouse.io/v1/boards/testco/jobs/123", "greenhouse", "testco", 10),
        (
            "https://testco.myworkdayjobs.com/wday/cxs/tenant/testco/jobs",
            "workday",
            "testco/testco",
            10,
        ),
        ("https://api.smartrecruiters.com/v1/companies/testco", "smartrecruiters", "testco", 10),
        ("https://jobs.lever.co/testco", "lever", "testco", 5),
        ("https://boards.greenhouse.io/testco", "greenhouse", "testco", 5),
        ("https://jobs.ashbyhq.com/TestCo", "ashby", "TestCo", 5),  # Case-sensitive
        ("https://testco.myworkdayjobs.com/en-US/testco", "workday", "testco/testco", 5),
        ("https://jobs.smartrecruiters.com/testco", "smartrecruiters", "testco", 5),
        ("https://testco.recruitee.com", "recruitee", "testco", 5),
        ("https://testco.breezy.hr", "breezy", "testco", 5),
        ("https://testco.applytojob.com", "jazzhr", "testco", 5),
        ("https://testco.pinpointhq.com", "pinpoint", "testco", 5),
        ("https://testco.jobs.personio.de", "personio", "testco", 5),
        ("https://testco.bamboohr.com", "bamboohr", "testco", 5),
        ("https://testco.teamtailor.com", "teamtailor", "testco", 5),
        ("https://apply.workable.com/testco", "workable", "testco", 5),
        ("https://jobs.jobvite.com/testco", "jobvite", "testco", 5),
        (
            "https://recruiting.paylocity.com/recruiting/jobs/All/550e8400-e29b-41d4-a716-446655440000",
            "paylocity",
            "550e8400-e29b-41d4-a716-446655440000",
            5,
        ),
        ("https://ats.rippling.com/testco", "rippling", "testco", 5),
        (
            "https://recruiting2.ultipro.com/TENANT/JobBoard/550e8400-e29b-41d4-a716-446655440000",
            "ultipro",
            "recruiting2.ultipro.com/TENANT/550e8400-e29b-41d4-a716-446655440000",
            5,
        ),
        ("https://pod.fa.us.oraclecloud.com", "oracle_cloud", "pod.fa.us.oraclecloud.com|CX_1", 5),
        ("https://careers-testco.icims.com", "icims", "testco", 5),
        (
            "https://career1.successfactors.com?company=testco",
            "successfactors",
            "career1.successfactors.com|testco",
            5,
        ),
        ("https://careers.conduent.com", "phenom", "careers.conduent.com", 5),
        (
            "https://workforcenow.adp.com/jobs?cid=550e8400-e29b-41d4-a716-446655440000",
            "adp",
            "550e8400-e29b-41d4-a716-446655440000",
            5,
        ),
        # MIXED-CASE regression guards: lever/greenhouse slugs and the workday
        # TENANT must preserve case byte-for-byte (a `.lower()` here silently
        # mis-slugs the scanner's API URL -> 404 -> silent scan loss, and drifts
        # DB slug identity). The pre-fix registry lowercased these; the legacy
        # extract_ats_from_url_best preserved them. Lowercase-only inputs (as the
        # rest of this list used) could not catch that — these mixed-case rows do.
        ("https://jobs.lever.co/NimbleAI", "lever", "NimbleAI", 5),
        ("https://api.lever.co/v0/postings/NimbleAI", "lever", "NimbleAI", 10),
        ("https://boards.greenhouse.io/MixedCo", "greenhouse", "MixedCo", 5),
        (
            "https://boards-api.greenhouse.io/v1/boards/MixedCo/jobs/1",
            "greenhouse",
            "MixedCo",
            10,
        ),
        (
            "https://MixedTen.myworkdayjobs.com/en-US/MixedBoard",
            "workday",
            "MixedTen/MixedBoard",
            5,
        ),
    ]

    for url, expected_platform, expected_slug, expected_spec in test_urls:
        result = extract_ats_from_url_best(url)
        assert result is not None, f"Legacy implementation returned None for {url}"
        platform, slug, spec = result
        assert platform == expected_platform, (
            f"URL {url}: expected platform {expected_platform}, got {platform}"
        )
        assert slug == expected_slug, f"URL {url}: expected slug {expected_slug}, got {slug}"
        assert spec == expected_spec, f"URL {url}: expected spec {expected_spec}, got {spec}"


def test_equivalence_reconciler_posting_id_patterns():
    """The registry's RECONCILER_POSTING_ID_PATTERNS must exactly match the legacy
    _SIMPLE_POSTING_ID_PATTERNS from ats_reconciler (platform-key set and regex patterns).
    Greenhouse is excluded — ats_reconciler special-cases it with a 3-pattern chain."""
    import re

    # Golden baseline from pre-PR-5 ats_reconciler._SIMPLE_POSTING_ID_PATTERNS
    golden_patterns = {
        "lever": re.compile(r"jobs\.lever\.co/[^/]+/([a-f0-9-]+)", re.IGNORECASE),
        "ashby": re.compile(r"jobs\.ashbyhq\.com/[^/]+/([a-f0-9-]+)"),
        "workday": re.compile(
            r"myworkdayjobs\.com/[^?#]*?/([^/?#]+)(?:/?(?:[?#]|$))", re.IGNORECASE
        ),
        "smartrecruiters": re.compile(
            r"jobs\.smartrecruiters\.com/[^/]+/([A-Za-z0-9_]+)", re.IGNORECASE
        ),
    }

    derived = ats_registry.RECONCILER_POSTING_ID_PATTERNS

    # Platform-key set must match exactly
    assert set(derived.keys()) == set(golden_patterns.keys()), (
        f"Platform key mismatch: derived={set(derived.keys())}, "
        f"golden={set(golden_patterns.keys())}"
    )

    # Regex patterns must be byte-identical (pattern string + flags)
    for platform in golden_patterns:
        assert derived[platform].pattern == golden_patterns[platform].pattern, (
            f"{platform}: pattern mismatch - derived={derived[platform].pattern}, "
            f"golden={golden_patterns[platform].pattern}"
        )
        assert derived[platform].flags == golden_patterns[platform].flags, (
            f"{platform}: flags mismatch - derived={derived[platform].flags}, "
            f"golden={golden_patterns[platform].flags}"
        )


def test_equivalence_expiry_checker_posting_id_patterns():
    """The registry's EXPIRY_CHECKER_POSTING_ID_PATTERNS holds only the platforms whose
    posting id is a single domain-anchored dict pattern.

    Greenhouse is NOT in this dict — like the reconciler, expiry_checker routes it
    through extract_greenhouse_posting_id() (the multi-shape single source of truth:
    canonical/EU host, custom-domain gh_jid, embed token). Workday and SmartRecruiters
    are excluded — they rely on Phase B batch reconciliation.

    Like test_equivalence_reconciler_posting_id_patterns, this only compares the
    registry-derived dict against a hardcoded golden baseline — it never imports
    expiry_checker, so it ports to the engine unchanged even though that module
    itself has no engine equivalent."""
    import re

    # Lever + Ashby resolve via a single dict pattern; greenhouse routes through the
    # canonical extractor (asserted separately in test_expiry_checker_greenhouse_*).
    golden_patterns = {
        "lever": re.compile(r"jobs\.lever\.co/[^/]+/([a-f0-9-]+)", re.IGNORECASE),
        "ashby": re.compile(r"jobs\.ashbyhq\.com/[^/]+/([a-f0-9-]+)"),
    }

    derived = ats_registry.EXPIRY_CHECKER_POSTING_ID_PATTERNS

    # Platform-key set must match exactly
    assert set(derived.keys()) == set(golden_patterns.keys()), (
        f"Platform key mismatch: derived={set(derived.keys())}, "
        f"golden={set(golden_patterns.keys())}"
    )

    # Regex patterns must be byte-identical (pattern string + flags)
    for platform in golden_patterns:
        assert derived[platform].pattern == golden_patterns[platform].pattern, (
            f"{platform}: pattern mismatch - derived={derived[platform].pattern}, "
            f"golden={golden_patterns[platform].pattern}"
        )
        assert derived[platform].flags == golden_patterns[platform].flags, (
            f"{platform}: flags mismatch - derived={derived[platform].flags}, "
            f"golden={golden_patterns[platform].flags}"
        )


@pytest.mark.skip(reason="expiry_checker not ported to the engine, no equivalent")
def test_expiry_checker_excludes_workday_and_smartrecruiters():
    """Regression test: expiry_checker must NOT resolve posting IDs for workday or
    smartrecruiters (per its docstring: they rely on Phase B batch reconciliation).
    This guards against a future refactor that might naively expose all posting_id_pattern
    platforms to expiry_checker."""
    from jobcannon.engine.expiry_checker import _extract_posting_id  # noqa: F401 -- not ported

    # Workday URL with posting ID
    workday_url = "https://testco.myworkdayjobs.com/en-US/testco/job/Senior-Data-Scientist_R-12345"
    assert _extract_posting_id(workday_url, "workday") is None, (
        "expiry_checker must return None for workday (relies on Phase B reconciliation)"
    )

    # SmartRecruiters URL with posting ID
    sr_url = "https://jobs.smartrecruiters.com/testco/12345-senior-data-scientist"
    assert _extract_posting_id(sr_url, "smartrecruiters") is None, (
        "expiry_checker must return None for smartrecruiters (relies on Phase B reconciliation)"
    )


class TestExtractGreenhousePostingId:
    """extract_greenhouse_posting_id is the single source of truth for the several
    real-world Greenhouse URL shapes. Consumers (reconciler, expiry_checker, backfill
    migration) all route through it, so these cases lock every shape in one place."""

    def test_canonical_board(self):
        from jobcannon.engine.ats_registry import extract_greenhouse_posting_id

        assert (
            extract_greenhouse_posting_id("https://boards.greenhouse.io/acme/jobs/4567890")
            == "4567890"
        )

    def test_job_boards_host(self):
        from jobcannon.engine.ats_registry import extract_greenhouse_posting_id

        assert (
            extract_greenhouse_posting_id("https://job-boards.greenhouse.io/acme/jobs/123456")
            == "123456"
        )

    def test_eu_region_host(self):
        """EU data-region host (job-boards.eu.greenhouse.io) — the moniepoint case that the
        narrow boards.greenhouse.io pattern silently dropped (#644 fallout)."""
        from jobcannon.engine.ats_registry import extract_greenhouse_posting_id

        assert (
            extract_greenhouse_posting_id(
                "https://job-boards.eu.greenhouse.io/moniepoint/jobs/4808972101"
            )
            == "4808972101"
        )

    def test_self_hosted_gh_jid_redirect(self):
        """Custom career domain with ?gh_jid=<id> — the sofi/airbnb/pinterest/roblox case."""
        from jobcannon.engine.ats_registry import extract_greenhouse_posting_id

        assert (
            extract_greenhouse_posting_id(
                "https://careers.airbnb.com/positions/7662244?gh_jid=7662244"
            )
            == "7662244"
        )

    def test_embed_token(self):
        from jobcannon.engine.ats_registry import extract_greenhouse_posting_id

        assert (
            extract_greenhouse_posting_id(
                "https://boards.greenhouse.io/embed/job_app?for=acme&token=998877"
            )
            == "998877"
        )

    def test_path_id_wins_over_gh_jid_when_both_present(self):
        """Ordering: the canonical path id is returned when a URL carries both shapes."""
        from jobcannon.engine.ats_registry import extract_greenhouse_posting_id

        assert (
            extract_greenhouse_posting_id("https://boards.greenhouse.io/acme/jobs/111?gh_jid=111")
            == "111"
        )

    def test_embedded_greenhouse_path_in_query_value_not_extracted(self):
        """extract_ is component-anchored: a greenhouse host+path embedded as a SUBSTRING
        in an unrelated host's query value (a redirect/tracking wrapper) is not a real
        posting URL and must return None. A raw ``re.search`` over the whole URL accepts
        this (py/incomplete-url-substring-sanitization); the defect must not survive in
        extract_ either. Regression for the PR #695 adversarial finding."""
        from jobcannon.engine.ats_registry import extract_greenhouse_posting_id

        assert (
            extract_greenhouse_posting_id("https://evil.com/p?redir=greenhouse.io/acme/jobs/111")
            is None
        )

    def test_spoofed_host_suffix_not_extracted(self):
        """greenhouse.io.evil.com is a subdomain of evil.com, not greenhouse — host-boundary
        matching (not a substring test) rejects it."""
        from jobcannon.engine.ats_registry import extract_greenhouse_posting_id

        assert extract_greenhouse_posting_id("https://greenhouse.io.evil.com/x/jobs/1") is None

    def test_scheme_less_host_path(self):
        """A stored URL without a scheme (bare host/path form) still resolves — the host is
        recovered into netloc so extraction does not silently regress to a false negative."""
        from jobcannon.engine.ats_registry import extract_greenhouse_posting_id

        assert extract_greenhouse_posting_id("boards.greenhouse.io/acme/jobs/777") == "777"

    def test_non_greenhouse_url_returns_none(self):
        from jobcannon.engine.ats_registry import extract_greenhouse_posting_id

        assert extract_greenhouse_posting_id("https://www.linkedin.com/jobs/view/12345/") is None
        assert extract_greenhouse_posting_id("https://www.amazon.jobs/en/jobs/3133798/x") is None

    def test_empty_url_returns_none(self):
        from jobcannon.engine.ats_registry import extract_greenhouse_posting_id

        assert extract_greenhouse_posting_id("") is None


class TestDetectGreenhousePostingId:
    """detect_greenhouse_posting_id is the platform-UNKNOWN variant used by the backfill
    migration. It keys greenhouse via DISCRIMINATING shapes only (host+path or gh_jid=),
    NOT a substring host test and NOT the generic embed token= shape."""

    def test_detects_eu_host_path(self):
        from jobcannon.engine.ats_registry import detect_greenhouse_posting_id

        assert (
            detect_greenhouse_posting_id(
                "https://job-boards.eu.greenhouse.io/moniepoint/jobs/4808972101"
            )
            == "4808972101"
        )

    def test_detects_self_hosted_gh_jid(self):
        from jobcannon.engine.ats_registry import detect_greenhouse_posting_id

        assert (
            detect_greenhouse_posting_id(
                "https://sofi.com/careers/job/7601577003?gh_jid=7601577003"
            )
            == "7601577003"
        )

    def test_bare_token_is_not_a_detection_signal(self):
        """token= is generic; a non-Greenhouse URL carrying it must NOT be misdetected."""
        from jobcannon.engine.ats_registry import detect_greenhouse_posting_id

        assert detect_greenhouse_posting_id("https://foo.com/apply?token=555") is None

    def test_spoofed_host_suffix_not_detected(self):
        """A crafted host like greenhouse.io.evil.com (that a substring 'greenhouse.io in url'
        check would wrongly accept) does not match the anchored host+path shape."""
        from jobcannon.engine.ats_registry import detect_greenhouse_posting_id

        assert detect_greenhouse_posting_id("https://greenhouse.io.evil.com/x/jobs/1") is None

    def test_embedded_greenhouse_path_in_query_value_not_detected(self):
        """A greenhouse host+path embedded as a SUBSTRING in an unrelated host's query
        value (a redirect/tracking wrapper) must NOT be detected. A raw ``re.search`` over
        the whole URL accepts this (py/incomplete-url-substring-sanitization); binding the
        match to the parsed hostname rejects it. This is the exact false-positive that would
        key ("greenhouse", "111") into jobs.postings for a non-greenhouse URL, violating the
        migration's F1 cross-platform guard. Regression for the PR #695 adversarial finding."""
        from jobcannon.engine.ats_registry import detect_greenhouse_posting_id

        assert (
            detect_greenhouse_posting_id("https://evil.com/p?redir=greenhouse.io/acme/jobs/111")
            is None
        )
        # percent-encoded variant: the greenhouse path lives inside an encoded query value
        assert (
            detect_greenhouse_posting_id(
                "https://evil.com/p?redir=https%3A%2F%2Fboards.greenhouse.io%2Fx%2Fjobs%2F111"
            )
            is None
        )

    def test_non_greenhouse_returns_none(self):
        from jobcannon.engine.ats_registry import detect_greenhouse_posting_id

        assert detect_greenhouse_posting_id("https://www.amazon.jobs/en/jobs/3133798/x") is None
        assert detect_greenhouse_posting_id("") is None
