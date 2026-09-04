# PORTED from job_finder/sources/__init__.py @ e4c6d2e243ca561de7a30b0acd29470f89f0c18c (private job-cannon). Ledger L-0042.
# PORT-SEAM: private docstring named "IMAP, SerpAPI, DataForSEO, portal_search,
# etc." — those source clients are HOLD/out of scope for this port
# (design-aggregators-imap.md §5, DEC-11 trigger). This package currently has
# no other members: it exists only so the L-0042 row has a landing that keeps
# the private package-root reachability property audit-trail-visible, not
# because anything in jobcannon/engine imports through it (email_senders and
# _pii_scrub are flat modules directly under jobcannon.engine, per the
# port_rewrite.py REWRITES for L-0113/L-0043 — see design-aggregators-imap.md
# §1.9-§1.10 and port_rewrite.py's "flattened sources modules" comment).
"""Vestigial package root for ported per-user alert-ingestion sources.

The private original doc'd this as "Job sources - IMAP, SerpAPI, DataForSEO,
portal_search, etc." Those source clients stay HOLD; only the shared parser
layer (jobcannon.engine.email_parsers) and sender registry
(jobcannon.engine.email_senders) have landed as of this port, and neither
lives under this package.
"""
