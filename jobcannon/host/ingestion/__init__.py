"""Host-side per-tenant ingestion lanes. See imap_intake.py (IMAP lane),
_alert_parse.py (shared per-message parse dispatch, FU-D), and
_parse_log.py (sole email_parse_log* writer, FU-C) -- the last two renamed
out of / extracted under issue #358 so a future forwarded-alert lane reuses
them instead of duplicating them."""
