"""The repair backlog re-reads only the services with something open (account 3: Gmail was 54 of 165 verify
hours, and it.department@'s repairs re-read the whole mailbox for Drive-only findings). A run without the
scope -- every UI run -- still checks every service it was asked for."""
import types

import verification_repairs as R
import verify_sample as V

OK = {"checked": 1, "identical": 1, "differences": [], "missing": [], "duplicates": [], "extras": [],
      "notCopied": [], "errors": [], "notes": []}


class _Verifier:
    calls = []

    def __init__(self, auth, db, settings, u, t, retry=None, limit=None, on_api_attempt=None, on_progress=None):
        self.u, self.evidence = u, []

    def _report_progress(self, *a, **k):
        pass

    def __getattr__(self, svc):
        def check():
            _Verifier.calls.append((self.u, svc))
            return dict(OK)
        return check


class _DB:
    def all_identities(self):
        return [{"entity_type": "user", "source_email": u, "target_email": u + ".t"} for u in ("a@s", "b@s")]


def _run(monkeypatch, services_for=None):
    monkeypatch.setattr(V, "Verifier", _Verifier)
    monkeypatch.setattr(V.Verifier, "_blank", staticmethod(lambda: dict(OK)), raising=False)
    monkeypatch.setattr("verification_coverage.finish", lambda r, svc: None)
    _Verifier.calls = []
    settings = types.SimpleNamespace(source_domain="s", target_domain="t")
    return V.run(None, _DB(), settings, services=("tasks", "gmail"), services_for=services_for)


def test_scoped_users_read_only_their_own_services_and_the_report_counts_them(monkeypatch):
    report = _run(monkeypatch, lambda u: ("tasks",) if u == "a@s" else ("tasks", "gmail"))
    assert sorted(_Verifier.calls) == [("a@s", "tasks"), ("b@s", "gmail"), ("b@s", "tasks")]
    assert report["coverage"]["servicesExpected"] == 3
    assert report["coverage"]["completion"] == "COMPLETE" and report["verdict"] == "IDENTICAL"


def test_without_a_scope_every_service_is_checked(monkeypatch):
    _run(monkeypatch)
    assert len(_Verifier.calls) == 4


def test_services_to_check_picks_open_and_unproven_services(db):
    for svc, verdict in (("drive", "DIFFERENCES"), ("gmail", "IDENTICAL"), ("calendar", "IDENTICAL")):
        db.save_user_verification("u@s", svc, verdict, 1, 0, None, {})
    R.ensure(db)
    with db.write() as c:
        c.execute("INSERT INTO verification_repair_queue(source_user,service,finding_key,category,item_id,state,"
                  "detail,first_seen,last_seen) VALUES('u@s','calendar','k','notCopied','e','QUEUED','{}','x','x')")
    assert R.services_to_check(db, "u@s", ("drive", "gmail", "calendar", "tasks")) == ("drive", "calendar", "tasks")
