"""fableplan2 P1.4: a wipe starts only if every service answers, and its summary tells the truth.

Live, 16 Nov 2026: both sandbox tenants had stopped serving Drive, Calendar and Tasks (a lapsed
subscription) while tokens still minted. The wipe's own preflight was a token mint, so it started,
deleted 72,132 Gmail messages and nothing else, and its summary listed every service it had TRIED."""
import pytest

import wipe_tenant as wt


class Chain:
    """Any Google call chain; .execute() answers, or raises what the tenant would."""
    def __init__(self, fail=None):
        self.fail = fail

    def __getattr__(self, _name):
        return self

    def __call__(self, *a, **k):
        return self

    def execute(self):
        if self.fail:
            raise self.fail
        return {}


SERVICES = ("drive", "gmail", "calendar", "contacts", "tasks")


def test_every_service_answering_for_every_user_means_no_problem():
    assert wt.preflight(["a@x.test", "b@x.test"], lambda s, u: Chain(), "admin@x.test", SERVICES) == []


def test_a_service_that_refuses_one_user_is_named_with_the_reason():
    def clients(service, user):
        return Chain(RuntimeError("401 Active session is invalid")) if (service, user) == ("drive", "b@x.test") else Chain()
    got = wt.preflight(["a@x.test", "b@x.test"], clients, "admin@x.test", SERVICES)
    assert len(got) == 1 and got[0].startswith("b@x.test drive: RuntimeError: 401 Active session")


def test_the_admins_drive_is_read_too_because_shared_drives_are_wiped_as_the_admin():
    seen = []
    wt.preflight(["a@x.test"], lambda s, u: seen.append((s, u)) or Chain(), "admin@x.test", SERVICES)
    assert ("drive", "admin@x.test") in seen


def test_chat_and_groups_are_not_read_probed():
    seen = []
    wt.preflight(["a@x.test"], lambda s, u: seen.append(s) or Chain(), "admin@x.test", ("chat", "groups"))
    assert seen == []          # Chat's scope only deletes; groups are one admin call checked by granted()


class _S:
    source_domain, target_domain = "a.example", "b.example"
    source_admin, target_admin = "admin@a.example", "admin@b.example"
    source_sa_key = target_sa_key = "/nowhere"


@pytest.fixture
def main_env(monkeypatch, tmp_path):
    import reset_target
    s = _S()
    s.db_path = str(tmp_path / "m.db")
    monkeypatch.setattr(wt, "Settings", lambda: s)
    monkeypatch.setattr(reset_target, "assert_sandbox", lambda *a, **k: None)
    monkeypatch.setattr(wt, "granted", lambda key, admin, service: True)
    monkeypatch.setattr(wt, "_users", lambda settings, side, db: ["a@b.example", "b@b.example"])
    monkeypatch.setattr(wt, "key_clients", lambda key: (lambda service, user: Chain()))
    monkeypatch.setattr(wt, "readiness_problems", lambda settings, side: [])
    calls = []
    monkeypatch.setattr(wt, "wipe", lambda *a, **k: calls.append(a) or {
        "totals": {"drive": 3, "gmail": 5}, "failed": {"drive": [], "gmail": ["b@b.example"]},
        "shared_drives": 0, "groups": None, "emptied": ["drive"]})
    monkeypatch.setattr(wt, "reset_ledger", lambda *a, **k: {})
    return calls


def _run(argv=("--side", "target", "--confirm-domain", "b.example", "--yes")):
    return wt.main(list(argv))


def test_a_tenant_that_is_not_ready_is_refused_and_nothing_is_deleted(main_env, monkeypatch, capsys):
    monkeypatch.setattr(wt, "readiness_problems", lambda settings, side: ["service: Drive: 401 Active session is invalid"])
    assert _run() == 2
    out = capsys.readouterr().out
    assert main_env == [] and "NOT WIPED -- refused: nothing was deleted on b.example" in out and "401" in out


def test_one_user_whose_service_refuses_stops_the_whole_wipe(main_env, monkeypatch, capsys):
    monkeypatch.setattr(wt, "key_clients", lambda key: (lambda service, user: Chain(
        RuntimeError("403 notACalendarUser") if (service, user) == ("calendar", "b@b.example") else None)))
    assert _run() == 2
    assert main_env == [] and "b@b.example calendar" in capsys.readouterr().out


def test_a_scope_that_is_not_granted_stops_the_wipe_too(main_env, monkeypatch, capsys):
    monkeypatch.setattr(wt, "granted", lambda key, admin, service: service != "tasks")
    assert _run() == 2
    assert main_env == [] and "tasks: its scope" in capsys.readouterr().out


def test_allow_partial_wipes_what_answers_and_says_what_it_left(main_env, monkeypatch, capsys):
    monkeypatch.setattr(wt, "readiness_problems", lambda settings, side: ["service: Drive: 401"])
    rc = _run(("--side", "target", "--confirm-domain", "b.example", "--yes", "--allow-partial"))
    out = capsys.readouterr().out
    assert len(main_env) == 1 and rc == 1            # the wipe ran; gmail was not emptied for one user
    assert "Removed drive: emptied for 2 of 2 user(s), 3 item(s)" in out
    assert "Removed gmail: emptied for 1 of 2 user(s), 5 item(s)" in out
    assert "NOT WIPED -- still on b.example: gmail" in out


def test_the_ledger_summary_names_what_it_forgot_not_what_it_tried(db, identity):
    lines = []
    wt.reset_ledger(db, [], shared_drives_gone=False, out=lambda m, **k: lines.append(m),
                    failed={"drive": ["tgt@nowhere.test"], "gmail": []})
    assert lines[0].startswith("Ledger reset: forgot ")
    assert "drive for" in lines[0] and "gmail for" in lines[0]
