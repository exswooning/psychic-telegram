import json

import pytest

import control_plane_db as cpdb
import error_resolution as E
import incident_worker as worker
from db import MigrationDB


@pytest.fixture
def cp(tmp_path, monkeypatch):
    path = str(tmp_path / "control.db")
    cpdb.apply_migrations(path)
    monkeypatch.setattr(cpdb, "_db_path", lambda: path)
    return tmp_path


def line(
    message="HTTP 403 rateLimitExceeded",
    user="alice@source.example",
    item="file1",
    **kw,
):
    return (
        json.dumps(
            dict(
                bitport_event=1,
                kind="error",
                service="gmail",
                source_user=user,
                item_id=item,
                message=message,
                version="a" * 40,
                **kw,
            )
        )
        + "\n"
    )


def record(cp, text=None, account=3, filename="verify.log"):
    path = cp / filename
    path.write_text(text or line())
    E.ingest_file(path, account, "verify")
    return path


def test_cursor_survives_restart_partial_line_and_accounts_are_isolated(cp):
    path = cp / "run.log"
    event = line()
    path.write_text(event[: len(event) // 2])
    assert E.ingest_file(path, 3, "verify") == 0
    with path.open("a") as f:
        f.write(event[len(event) // 2 :])
    assert E.ingest_file(path, 3, "verify") == 1
    assert E.ingest_file(path, 3, "verify") == 0
    assert E.list_cases(3)[0]["occurrences"] == 1
    E.ingest_file(path, 4, "verify")
    assert len(E.list_cases(4)) == 1 and len(E.list_cases()) == 2


def test_same_family_groups_different_items_and_users_without_recounting(cp):
    path = record(cp, line(item="one") + line(user="bob@source.example", item="two"))
    (case,) = E.list_cases(3)
    assert (
        case["category"],
        case["state"],
        case["occurrences"],
        case["affected_users"],
        case["affected_items"],
    ) == ("quota", "RECOVERING", 2, 2, 2)
    E.ingest_file(path, 3, "verify")
    assert E.list_cases(3)[0]["occurrences"] == 2


def test_truncate_and_regrow_same_inode_or_rotate_does_not_skip_new_errors(cp):
    path = record(cp)
    path.write_text(line("TypeError: new failure") + "padding\n" * 200)
    E.ingest_file(path, 3, "verify")
    assert len(E.list_cases(3)) == 2
    path.rename(cp / "old.log")
    path.write_text(line("KeyError: replacement"))
    E.ingest_file(path, 3, "verify")
    assert len(E.list_cases(3)) == 3


def test_redacted_export_never_contains_secrets_email_or_command_instructions(cp):
    message = 'TypeError: password="do-not-export" Authorization: Bearer secret-value https://example.com?a=private&code=otp body=customer email text'
    record(cp, line(message=message))
    case = E.list_cases(3)[0]
    export = json.dumps(E.bundle(case["id"]))
    assert not any(
        s in export
        for s in [
            "do-not-export",
            "secret-value",
            "a=private",
            "otp",
            "customer email text",
            "alice@source.example",
        ]
    )
    assert "untrusted" in export.lower()
    assert "shell" not in E.redact("password=x")


def test_metrics_and_heartbeats_are_not_repeated_error_occurrences(cp):
    record(
        cp,
        "verify: progress alice@source.example | service=gmail | operation=read | items=3,000/40,241\n"
        "Google refused api: HTTP 403 rateLimitExceeded\n"
        "verify: heartbeat alice@source.example | service=gmail | items=3150/40241 | retries=19 | lastError=HttpError 403 rateLimitExceeded\n",
    )
    (case,) = E.list_cases(3)
    assert case["occurrences"] == 1
    with cpdb.ro() as db:
        metric = db.execute(
            "SELECT metrics FROM error_events WHERE kind='metric'"
        ).fetchone()
    assert json.loads(metric[0])["retries"] == 19
    assert (
        case["version"] == "unknown"
    )  # historical logs are not stamped as today's release


@pytest.mark.parametrize(
    "msg,category",
    [
        ("HTTP 403 rateLimitExceeded", "quota"),
        ("HTTP 403 insufficientPermissions", "permissions"),
        ("invalid_grant: Invalid email", "authentication"),
        ("TimeoutError: read timed out", "transient"),
        ("HTTP 404 notFound", "missing_source"),
        ("Feature not supported", "unsupported"),
        ("AttributeError: bad response", "code"),
        ("verdict=DIFFERENCES", "integrity"),
    ],
)
def test_classification_uses_reason_not_just_status(msg, category):
    assert E.classify(msg) == category


def test_claims_are_exclusive_account_scoped_bounded_and_leases_expire(cp, monkeypatch):
    record(cp, line("TypeError: bad result"), account=3)
    record(cp, line("TypeError: other failure"), account=4, filename="other.log")
    rid, token = E.create_runner(3, "source laptop")
    runner = E.authenticate(token)
    assert (
        runner and E.authenticate("incorrect token that is sufficiently long") is None
    )
    with cpdb.ro() as db:
        assert (
            db.execute(
                "SELECT token_hash FROM error_runner_tokens WHERE id=?", (rid,)
            ).fetchone()[0]
            != token
        )
    attempt = E.claim(runner)
    assert attempt["bundle"]["caseId"] == E.list_cases(3)[0]["id"]
    assert E.claim(runner) is None
    with pytest.raises(ValueError):
        E.report(runner, attempt["attemptId"], "bad lease", "TESTING")
    E.heartbeat(runner, attempt["attemptId"], attempt["lease"])
    with cpdb.rw() as db:
        db.execute("UPDATE error_fix_attempts SET lease_until=0")
    assert E.claim(runner) is None
    assert E.list_cases(3)[0]["state"] == "FAILED"
    assert E.list_cases(4)[0]["state"] == "QUEUED"


def test_commit_or_test_success_cannot_resolve_or_deploy_unqualified_case(cp):
    record(cp, line("TypeError: bad result"))
    _, token = E.create_runner(3, "worker")
    r = E.authenticate(token)
    a = E.claim(r)
    with pytest.raises(ValueError):
        E.report(r, a["attemptId"], a["lease"], "RESOLVED")
    E.report(r, a["attemptId"], a["lease"], "TESTING")
    with pytest.raises(ValueError):
        E.report(
            r,
            a["attemptId"],
            a["lease"],
            "WAITING_DEPLOYMENT",
            candidate_commit="b" * 40,
            evidence={"testsPassed": True},
        )
    E.report(
        r,
        a["attemptId"],
        a["lease"],
        "WAITING_QUALIFICATION",
        "No sandbox qualification hook",
        candidate_commit="b" * 40,
    )
    assert E.list_cases(3)[0]["state"] == "WAITING_QUALIFICATION"


def test_unknown_version_and_paused_policy_cannot_spend_model_budget(cp):
    record(cp, "Traceback (most recent call last):\n")
    _, token = E.create_runner(3, "worker")
    r = E.authenticate(token)
    assert E.claim(r) is None
    record(cp, line("TypeError: current release"), filename="current.log")
    E.set_policy(False, 3)
    assert E.claim(r) is None
    E.set_policy(True, 3)
    assert E.claim(r) is not None


def test_only_complete_fresh_all_affected_scope_resolves_case(cp):
    record(cp, line() + line(user="bob@source.example"))
    cid = E.list_cases(3)[0]["id"]
    db = MigrationDB(str(cp / "ledger.db"))
    fresh = "2099-01-01T00:00:00Z"

    def save(user, sampled=False):
        payload = json.dumps(
            {"scopeVersion": 2, "coverageComplete": True, "sampled": sampled}
        )
        db.conn.execute(
            "INSERT OR REPLACE INTO user_verification VALUES(?,?,?,?,?,?,?,?)",
            (
                user,
                "gmail",
                fresh,
                "IDENTICAL",
                10,
                10,
                100 if sampled else None,
                payload,
            ),
        )
        db.conn.commit()

    save("alice@source.example", True)
    save("bob@source.example")
    assert not E.sweep_verified(lambda aid: str(cp / "ledger.db"))
    save("alice@source.example")
    assert E.sweep_verified(lambda aid: str(cp / "ledger.db")) == [cid]
    assert E.list_cases(3)[0]["state"] == "RESOLVED"
    # Recurrence reopens it, with fresh evidence required again.
    record(cp, line(), filename="next-run.log")
    assert E.list_cases(3)[0]["state"] == "RECOVERING"
    db.close()


def test_fractional_last_seen_does_not_accept_earlier_same_second_verification():
    assert not E._after("2026-11-05T12:00:00Z", "2026-11-05T12:00:00.123456Z")


def test_terminal_backlog_is_drained_after_collector_restart(cp):
    record(cp, line() * 100)
    # register a different terminal log with only one event per initial bounded read
    other = cp / "terminal.log"
    other.write_text(line("TypeError: terminal") * 5)
    E.ingest_file(
        other, 3, "migrate", max_bytes=len(line("TypeError: terminal").encode())
    )
    E.drain_sources()
    case = next(c for c in E.list_cases(3) if c["category"] == "code")
    assert case["occurrences"] == 5


def test_worker_refuses_public_http_token_exposure():
    with pytest.raises(ValueError):
        worker.Client({"apiUrl": "http://example.com", "token": "x"})
    assert worker.Client({"apiUrl": "http://100.79.158.38:81", "token": "x"})


def test_candidate_cannot_delete_files_or_weaken_existing_tests(tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_old.py").write_text("assert True\n")
    (tmp_path / "engine.py").write_text("x=1\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "base",
        ],
        cwd=tmp_path,
        check=True,
    )
    base = worker.git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "tests/test_old.py").write_text("pass\n")
    (tmp_path / "engine.py").write_text("x=2\n")
    with pytest.raises(ValueError, match="existing tests"):
        worker.validate_changes(tmp_path, base)


def test_hooks_never_come_from_a_log_or_use_a_shell():
    assert worker.trusted_hook({}, "deployCommand", {"commit": "a" * 40}) is None
    assert (
        worker.trusted_hook({"deployCommand": "rm -rf /"}, "deployCommand", {}) is None
    )
    assert worker.trusted_hook(
        {"deployCommand": ["release", "{commit}"]},
        "deployCommand",
        {"commit": "a" * 40},
    ) == ["release", "a" * 40]
