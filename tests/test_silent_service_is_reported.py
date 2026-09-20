"""
tests/test_silent_service_is_reported.py
========================================
A run can succeed at every user and still migrate nothing.

On 2026-09-20 a delta asked for all six services against 265 users and
inserted ZERO mail: `--days 2` filters on the MESSAGE's date, so mailboxes
with no recent traffic yield nothing. Every user finished DONE, the batch
summary printed a clean result, and the fact that Gmail had moved not one
item was visible only by adding the run up by hand -- which nothing did.

The per-user view structurally cannot show this. It is a whole-run property.
"""

from __future__ import annotations

import main


def user(src, **services):
    return {"source": src, "status": "DONE", "elapsed_sec": 1.0,
            "services": services}


# The shape the engine really emits, copied from a live run.
GMAIL_SILENT = {"inserted": 0, "failed": 0, "skipped": 4,
                "drafts_inserted": 0, "drafts_failed": 0,
                "drafts_skipped": 0, "filters_inserted": 0,
                "filters_failed": 0, "filters_skipped": 0,
                "links_rewritten": 0}
DRIVE_BUSY = {"folders": 55, "files": 2273, "skipped": 0, "failed": 0,
              "acl_failed": 0, "comments": 131, "links_rewritten": 2}


class TestItNamesTheSilentService:
    def test_the_live_gmail_case(self):
        results = [user(f"u{i}@s.com", gmail=dict(GMAIL_SILENT),
                        drive=dict(DRIVE_BUSY)) for i in range(265)]
        silent = main.warn_services_that_moved_nothing(
            results, {"gmail", "drive"})
        assert silent == ["gmail"], silent

    def test_a_busy_service_is_not_named(self):
        results = [user("a@s.com", drive=dict(DRIVE_BUSY))]
        assert main.warn_services_that_moved_nothing(results, {"drive"}) == []

    def test_a_service_nobody_asked_for_is_not_named(self):
        """Absent from `services` means it was never requested."""
        results = [user("a@s.com", drive=dict(DRIVE_BUSY))]
        assert "chat" not in main.warn_services_that_moved_nothing(
            results, {"drive"})

    def test_one_user_moving_something_clears_the_whole_service(self):
        """It is a RUN-level property: one insert anywhere means Gmail works
        and the operator has no cause to be alarmed."""
        results = [user("a@s.com", gmail=dict(GMAIL_SILENT)) for _ in range(9)]
        results.append(user("j@s.com",
                            gmail=dict(GMAIL_SILENT, inserted=1)))
        assert main.warn_services_that_moved_nothing(results, {"gmail"}) == []


class TestFailuresAndSkipsAreNotProgress:
    def test_skips_alone_do_not_count_as_moved(self):
        """Gmail's live row was `skipped: 4` and nothing else. Counting a
        skip as work is exactly how this stayed invisible."""
        assert main._productive_total(GMAIL_SILENT) == 0

    def test_failures_alone_do_not_count_as_moved(self):
        assert main._productive_total(
            {"files": 0, "failed": 91, "acl_failed": 48148}) == 0

    def test_a_new_counter_counts_without_being_listed_here(self):
        """The rule is the two suffixes, not a hand-kept allowlist -- so a
        counter added to an engine tomorrow is counted tomorrow."""
        assert main._productive_total({"somethingbrandnew": 3}) == 3

    def test_a_missing_service_key_is_zero_not_a_crash(self):
        assert main._productive_total(None) == 0


class TestItIsWiredIntoTheSummary:
    def test_the_summary_passes_the_requested_services(self, capsys):
        results = [user("a@s.com", gmail=dict(GMAIL_SILENT))]
        main._print_batch_summary(results, {"gmail"})
        assert "migrated NOTHING" in capsys.readouterr().out

    def test_no_services_argument_stays_silent(self, capsys):
        """Callers that predate the argument must not start printing."""
        main._print_batch_summary([user("a@s.com", gmail=dict(GMAIL_SILENT))])
        assert "migrated NOTHING" not in capsys.readouterr().out
