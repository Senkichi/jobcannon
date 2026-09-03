# PORTED from job_finder/web/dedup_normalizer.py @ 65e5ce021068b70a2369ac279c75395a078e1013 (private job-cannon). Ledger L-0452.
"""Smart deduplication normalization for job dedup keys.

Provides normalization functions that collapse common formatting variations so
that the same real job (same company + same title) always maps to a single
canonical dedup_key regardless of location, suffix spelling, or title
abbreviation differences.

Design decisions:
- Location is INTENTIONALLY EXCLUDED from the dedup_key. Same company + same
  title = same job. A job posted in SF and NYC is the same opening.
- Company suffixes (Inc., LLC, Corp., Ltd., etc.) are stripped after lowercasing.
- Title abbreviations (Sr. -> Senior, Jr. -> Junior, etc.) are expanded.
- Title level suffixes (IC5, Level 3) are stripped — they are formatting noise.

Note: the private repo's ``run_retroactive_dedup`` (and its DB-merge helpers
``_merge_job_data`` / ``_merge_descriptions`` / etc.) are NOT ported here — see
the descope note at the bottom of this file.
"""

# ---------------------------------------------------------------------------
# Title abbreviation expansion + level-suffix stripping previously lived here as
# module-level regexes feeding a local ``normalize_title`` copy. They were a
# byte-for-byte duplicate of the foundation copy; ``normalize_title`` now
# delegates to ``jobcannon.engine.normalizers`` (the single source of truth), so the
# regexes moved out with it.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def normalize_company(company: str) -> str:
    """Normalize a company name for dedup key generation.

    Thin delegating wrapper around ``jobcannon.engine.normalizers.normalize_company``,
    the single source of truth for company normalization. Both modules live inside
    the engine purity boundary (``tests/engine/test_boundary.py``), so
    ``derive_dedup_key`` here computes the exact same key as ``Job.dedup_key`` and
    the upsert path. This eliminates the pre-existing drift where this module's
    earlier standalone copy skipped HTML-entity decode, HTML-tag strip,
    leading-numeric-junk strip, and internal whitespace collapse — a latent
    dedup-correctness hole (architectural-debt-B, canonical-field ownership). See
    the cross-copy parity assertions in the private repo's
    tests/test_dedup_normalizer.py (ported subset: tests/engine/test_dedup_normalizer.py).

    Args:
        company: Raw company name string.

    Returns:
        Lowercased, prefix- and suffix-stripped company name.
    """
    from jobcannon.engine.normalizers import normalize_company as _foundation_normalize_company

    return _foundation_normalize_company(company)


def normalize_title(title: str) -> str:
    """Normalize a job title for dedup key generation.

    Thin delegating wrapper around ``jobcannon.engine.normalizers.normalize_title``,
    the single source of truth for title normalization — mirroring
    ``normalize_company`` above. Previously this was a byte-for-byte COPY of the
    foundation implementation (guarded only by a parity test); delegating closes
    the drift window where a future edit to one copy would silently change
    dedup_key derivation in only one path. Both modules live inside the engine
    purity boundary (``tests/engine/test_boundary.py``), so ``derive_dedup_key``
    here computes the exact same key as ``Job.dedup_key`` and the upsert path.

    Args:
        title: Raw job title string.

    Returns:
        Lowercased, normalized title.
    """
    from jobcannon.engine.normalizers import normalize_title as _foundation_normalize_title

    return _foundation_normalize_title(title)


def derive_dedup_key(company: str, title: str) -> str:
    """Derive the current-version dedup_key using this module's delegating normalizers.

    Sibling of ``jobcannon.engine.normalizers.derive_dedup_key``. Both
    ``normalize_company`` and ``normalize_title`` now delegate directly to the
    foundation copies (the single source of truth), so this function produces
    the same key as ``Job.dedup_key`` and the upsert path. See D-8 and
    ``NORMALIZER_VERSION`` in ``jobcannon.engine.normalizers``.

    Args:
        company: Raw company name.
        title: Raw job title.

    Returns:
        ``"{normalized_company}|{normalized_title}"`` (location excluded).
    """
    return f"{normalize_company(company)}|{normalize_title(title)}"


def normalized_dedup_key(company: str, title: str, location: str = "") -> str:
    """Backward-compat wrapper. Prefer Job.normalized_dedup_key().

    Args:
        company: Raw company name.
        title: Raw job title.
        location: Ignored.

    Returns:
        String in format "{normalized_company}|{normalized_title}"
    """
    from jobcannon.engine.models import Job

    return Job.normalized_dedup_key(company, title, location)


# run_retroactive_dedup and DB-merge helpers deliberately not ported (see plan Task 1 Step 7c)
