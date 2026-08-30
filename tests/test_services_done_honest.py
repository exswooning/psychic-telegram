"""A service is 'done' only if it actually moved data -- skipping everything
is not migrating. Chat skipped all 398 of its not-a-space entities and was
being listed under services done as if it had migrated."""
import main


def test_all_skipped_is_not_done():
    # chat: nothing moved, everything skipped -> NOT done
    got = main._services_that_succeeded(
        {"chat": {"spaces": 0, "messages": 0, "skipped": 398, "failed": 0}})
    assert "chat" not in got


def test_real_moves_count_as_done():
    got = main._services_that_succeeded(
        {"drive": {"files": 500, "skipped": 3, "failed": 1}})
    assert "drive" in got


def test_nothing_to_process_is_done():
    # a user with genuinely empty tasks (nothing skipped, nothing failed)
    got = main._services_that_succeeded({"tasks": {"tasks": 0}})
    assert "tasks" in got


def test_all_failed_stays_unmarked():
    # the original guard: an all-or-nothing failure must not be marked done
    got = main._services_that_succeeded(
        {"contacts": {"contacts": 0, "failed": 42}})
    assert "contacts" not in got
