"""Tests for log_throttle.py — rate-limited logging for scheduled jobs."""

import logging
import time

from jobcannon.engine.log_throttle import _lock, _seen, throttled_log


class TestThrottledLog:
    """throttled_log suppresses duplicate messages within a cooldown window."""

    def setup_method(self):
        """Clear the global suppression state between tests."""
        with _lock:
            _seen.clear()

    def test_first_occurrence_logs_at_requested_level(self, caplog):
        """First occurrence of a message logs at the requested level."""
        logger = logging.getLogger("test.first")
        with caplog.at_level(logging.WARNING, logger="test.first"):
            throttled_log(logger, logging.WARNING, "something broke: %s", "oops")

        assert len(caplog.records) == 1
        assert caplog.records[0].levelno == logging.WARNING
        assert "something broke: oops" in caplog.records[0].message

    def test_duplicate_within_cooldown_suppressed_to_debug(self, caplog):
        """Duplicate messages within cooldown are suppressed to DEBUG."""
        logger = logging.getLogger("test.suppress")
        with caplog.at_level(logging.DEBUG, logger="test.suppress"):
            throttled_log(logger, logging.ERROR, "auth failed", cooldown=60)
            throttled_log(logger, logging.ERROR, "auth failed", cooldown=60)
            throttled_log(logger, logging.ERROR, "auth failed", cooldown=60)

        # First at ERROR, next two at DEBUG
        assert caplog.records[0].levelno == logging.ERROR
        assert caplog.records[1].levelno == logging.DEBUG
        assert caplog.records[2].levelno == logging.DEBUG
        assert "[suppressed" in caplog.records[1].message

    def test_different_messages_not_suppressed(self, caplog):
        """Different message templates are tracked independently."""
        logger = logging.getLogger("test.different")
        with caplog.at_level(logging.WARNING, logger="test.different"):
            throttled_log(logger, logging.WARNING, "error A", cooldown=60)
            throttled_log(logger, logging.WARNING, "error B", cooldown=60)

        assert len(caplog.records) == 2
        assert all(r.levelno == logging.WARNING for r in caplog.records)

    def test_cooldown_expiry_relogs_at_full_level(self, caplog):
        """After cooldown expires, next occurrence logs at full level again."""
        logger = logging.getLogger("test.expiry")

        with caplog.at_level(logging.DEBUG, logger="test.expiry"):
            throttled_log(logger, logging.ERROR, "token expired", cooldown=1)
            # Suppress one
            throttled_log(logger, logging.ERROR, "token expired", cooldown=1)

            # Wait for cooldown to expire
            time.sleep(1.1)

            # Should log at full level again
            throttled_log(logger, logging.ERROR, "token expired", cooldown=1)

        # Records: 1=ERROR (first), 2=DEBUG (suppressed), 3=ERROR (after cooldown), 4=ERROR (suppression summary)
        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(error_records) >= 2
        # The suppression summary should mention count
        summary_records = [
            r
            for r in caplog.records
            if "suppressed" in r.message.lower() and r.levelno == logging.ERROR
        ]
        assert len(summary_records) == 1

    def test_format_args_passed_through(self, caplog):
        """Format args are correctly applied to the log message."""
        logger = logging.getLogger("test.format")
        with caplog.at_level(logging.WARNING, logger="test.format"):
            throttled_log(logger, logging.WARNING, "Drive error: %s (code %d)", "timeout", 504)

        assert "Drive error: timeout (code 504)" in caplog.records[0].message

    def test_different_loggers_tracked_independently(self, caplog):
        """Same message from different loggers is not suppressed."""
        logger_a = logging.getLogger("test.logger_a")
        logger_b = logging.getLogger("test.logger_b")
        with caplog.at_level(logging.WARNING):
            throttled_log(logger_a, logging.WARNING, "same message", cooldown=60)
            throttled_log(logger_b, logging.WARNING, "same message", cooldown=60)

        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warning_records) == 2
