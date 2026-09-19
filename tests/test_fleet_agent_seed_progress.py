"""
_seed_progress() reads a helper node's own seed_sandbox.py log so it shows
up as more than an idle row on /nodes -- the seeder runs outside main.py
entirely, so _active_job() alone cannot describe it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fleet_agent  # noqa: E402


def test_parses_the_real_heartbeat_line(tmp_path):
    log = tmp_path / "seed.log"
    log.write_text(
        "Seeding 20 users in source.example.com at scale 'huge'\n"
        "  [seeduser281@source.example.com] starting (Engineering, PRJ-001)\n"
        "  ... still seeding: 3/20 users done after 62m00s (14 in flight), "
        "16.3 req/s, 287 retried (0.5%)\n"
    )
    got = fleet_agent._seed_progress(str(log))
    assert got == {
        "seed_users_done": 3, "seed_users_total": 20,
        "seed_in_flight": 14, "seed_req_per_sec": 16.3,
        "seed_retried_pct": 0.5,
    }


def test_uses_the_last_line_not_the_first(tmp_path):
    """A tailed log has many heartbeats; only the most recent is current."""
    log = tmp_path / "seed.log"
    log.write_text(
        "  ... still seeding: 1/20 users done after 30m00s (14 in flight), "
        "10.0 req/s, 0 retried (0.0%)\n"
        "  ... still seeding: 5/20 users done after 90m00s (14 in flight), "
        "20.0 req/s, 12 retried (1.2%)\n"
    )
    got = fleet_agent._seed_progress(str(log))
    assert got["seed_users_done"] == 5
    assert got["seed_retried_pct"] == 1.2


def test_no_log_path_is_not_an_error():
    assert fleet_agent._seed_progress(None) == {}


def test_missing_file_is_not_an_error():
    assert fleet_agent._seed_progress("/no/such/file.log") == {}


def test_a_log_with_no_heartbeat_yet_is_not_an_error(tmp_path):
    log = tmp_path / "seed.log"
    log.write_text("Sandbox guard passed for source.example.com.\n")
    assert fleet_agent._seed_progress(str(log)) == {}


def test_build_payload_labels_the_job_from_the_seed_domain(tmp_path, monkeypatch):
    """A node running seed_sandbox.py has no main.py subcommand for
    _active_job() to find -- without this it reports idle while doing
    real, visible work."""
    log = tmp_path / "seed.log"
    log.write_text(
        "  ... still seeding: 2/86 users done after 10m00s (14 in flight), "
        "24.0 req/s, 0 retried (0.0%)\n"
    )
    monkeypatch.setattr(fleet_agent, "_active_job", lambda: (None, None))
    payload = fleet_agent.build_payload(
        "test-node", seed_log=str(log), seed_domain="source.example.com")
    assert payload["active_job"] == "seed source.example.com"
    assert payload["seed_domain"] == "source.example.com"
    assert payload["seed_users_done"] == 2


def test_parses_a_user_failure_line(tmp_path):
    """A failure means the user never produced a result at all -- distinct
    from the per-item warnings the dashboard already groups, which are
    about a user that finished but had some items rejected."""
    log = tmp_path / "seed.log"
    log.write_text(
        "  ... still seeding: 19/20 users done after 130m00s (1 in flight), "
        "13.2 req/s, 2,107 retried (2.0%)\n"
        "  ! seeduser300@source.example.com FAILED: exhausted 6 retries on "
        "HTTP 401 (authError): <HttpError 401 ...>\n"
    )
    got = fleet_agent._seed_progress(str(log))
    assert got["seed_last_failure"].startswith("seeduser300@source.example.com: ")
    assert "HTTP 401" in got["seed_last_failure"]
    # Progress is independent of the failure -- both are read from the
    # same tail, neither one's absence should hide the other.
    assert got["seed_users_done"] == 19


def test_no_failure_line_means_no_failure_field(tmp_path):
    log = tmp_path / "seed.log"
    log.write_text(
        "  ... still seeding: 5/20 users done after 30m00s (14 in flight), "
        "10.0 req/s, 0 retried (0.0%)\n"
    )
    got = fleet_agent._seed_progress(str(log))
    assert "seed_last_failure" not in got


def test_a_failure_before_any_progress_line_is_still_caught(tmp_path):
    """A run whose very first user fails has no heartbeat line yet at
    all -- the failure must not be hidden behind requiring one."""
    log = tmp_path / "seed.log"
    log.write_text(
        "Seeding 5 users in source.example.com at scale 'huge'\n"
        "  [seeduser1@source.example.com] starting (Engineering, PRJ-001)\n"
        "  ! seeduser1@source.example.com FAILED: connection refused\n"
    )
    got = fleet_agent._seed_progress(str(log))
    assert got["seed_last_failure"] == "seeduser1@source.example.com: connection refused"


def test_a_real_main_py_job_is_not_overwritten_by_seed_fields(tmp_path, monkeypatch):
    """If this node is somehow running a real main.py job too, that name
    wins -- seed progress is additive, never a relabel of real work."""
    log = tmp_path / "seed.log"
    log.write_text(
        "  ... still seeding: 1/5 users done after 1m00s (1 in flight), "
        "1.0 req/s, 0 retried (0.0%)\n"
    )
    monkeypatch.setattr(fleet_agent, "_active_job", lambda: ("migrate", 123))
    payload = fleet_agent.build_payload(
        "test-node", seed_log=str(log), seed_domain="source.example.com")
    assert payload["active_job"] == "migrate"
