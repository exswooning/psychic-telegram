"""Offline end-to-end tests for the error acts in campaign scenarios."""
from __future__ import annotations

from types import SimpleNamespace

import campaign as C
import campaign_changes as CC
import campaign_scenarios as S


class _Result:
    def __init__(self, value=None):
        self.value = value

    def execute(self):
        return self.value or {}


class _Files:
    def __init__(self, state, side):
        self.state, self.side = state, side

    def get(self, **kwargs):
        file_id = kwargs["fileId"]
        source = self.side == "source"
        return _Result({"id": file_id, "name": "sample.txt", "mimeType": "text/plain", "size": "6"})

    def update(self, **kwargs):
        self.state["writes"].append(("drive.update", self.side, kwargs))
        return _Result({})

    def delete(self, **kwargs):
        self.state["writes"].append(("drive.delete", self.side, kwargs))
        return _Result({})


class _Permissions:
    def __init__(self, state):
        self.state = state

    def list(self, **kwargs):
        return _Result({"permissions": [{"id": "perm-1", "type": "user", "role": "reader",
                                         "emailAddress": "outside@example.test", "permissionDetails": []}]})

    def delete(self, **kwargs):
        self.state["writes"].append(("drive.permission.delete", "target", kwargs))
        return _Result({})


class _Drive:
    def __init__(self, state, side):
        self._files = _Files(state, side)
        self._permissions = _Permissions(state)

    def files(self):
        return self._files

    def permissions(self):
        return self._permissions


class _Messages:
    def __init__(self, state):
        self.state = state

    def get(self, **kwargs):
        return _Result({"labelIds": ["INBOX", "Label_123"]})

    def modify(self, **kwargs):
        self.state["writes"].append(("gmail.modify", "target", kwargs))
        return _Result({})


class _Gmail:
    def __init__(self, state):
        self.state = state

    def users(self):
        return self

    def messages(self):
        return _Messages(self.state)


class _People:
    def __init__(self, state):
        self.state = state

    def people(self):
        return self

    def deleteContact(self, **kwargs):
        self.state["writes"].append(("contacts.delete", "target", kwargs))
        return _Result({})

    def createContact(self, **kwargs):
        self.state["writes"].append(("contacts.create", "source", kwargs))
        return _Result({})


class _Tasks:
    def __init__(self, state):
        self.state = state

    def tasklists(self):
        return self

    def delete(self, **kwargs):
        self.state["writes"].append(("tasks.delete_list", "target", kwargs))
        return _Result({})


class _Conn:
    def execute(self, _sql, params):
        kind = params[1]
        return {"file": [("source-file", "target-file")],
                "message": [("source-message", "target-message")],
                "contact": [("source-contact", "people/target-contact")],
                "task_list": [("source-list", "target-list")]}[kind]


class _Seed:
    def __init__(self, state):
        self.state = state

    def build_services(self, _settings, _user):
        return None, _Gmail(self.state), None

    def build_people_tasks(self, _settings, _user):
        return _People(self.state), _Tasks(self.state)

    def insert_message(self, _gmail, _retry, _raw, _labels):
        self.state["writes"].append(("gmail.insert", "source", {}))


class _Auth:
    def __init__(self, state):
        self.state = state

    def target_drive(self, _user):
        return _Drive(self.state, "target")

    def source_drive(self, _user, writable=False):
        assert writable
        return _Drive(self.state, "source")

    def target_gmail(self, _user):
        return _Gmail(self.state)

    def target_people(self, _user):
        return _People(self.state)

    def target_tasks(self, _user):
        return _Tasks(self.state)


class _ErrorActDriver(C.Driver):
    def __init__(self, handled=True):
        self.handled = handled
        self.events = []
        self.user = "t02@a.sandbox.test"
        self.state = {"writes": []}
        self.settings = SimpleNamespace(source_domain="a.sandbox.test", target_domain="b.sandbox.test")
        self.ctx = SimpleNamespace(auth=_Auth(self.state), db=SimpleNamespace(conn=_Conn()), settings=self.settings,
                                   src="t02@a.sandbox.test", tgt="t02@b.sandbox.test")
        self.kind = None
        self.verified = False

    def take_twin(self):
        self.events.append(("twin", self.user))
        return self.user

    def prepare(self, fixtures, user=None):
        self.events.append(("prepare", fixtures))

    def api(self, method, path, body):
        self.events.append(("api", method, path, body))
        return {"ok": True}

    def wait_idle(self, _timeout):
        self.events.append(("wait",))
        return True

    def change(self, kind, user):
        self.events.append(("change", kind, user))
        self.kind = kind
        return CC.CHANGES[kind][0](self.ctx)

    def verify(self, user, service):
        self.events.append(("verify", user, service))
        self.verified = True

    def delta(self, user, service="drive"):
        self.events.append(("delta", user, service))

    def facts(self, _user, since=None):
        if not self.verified or not self.state["writes"]:
            return {"findings": set(), "fixed": set(), "gate": {"accepted": set(), "listed": set()}}
        import known_issues
        import parity_gate
        entry = known_issues.BY_ID[self.kind]
        end = "fixed" if entry[6] else "accepted" if self.kind in parity_gate.ACCEPTED_KINDS else "listed"
        return {"findings": {self.kind}, "fixed": {self.kind} if self.handled and end == "fixed" else set(),
                "gate": {"accepted": {self.kind} if self.handled and end == "accepted" else set(),
                         "listed": {self.kind} if self.handled and end == "listed" else set()}}


IMPLEMENTED = ("err:gmail.labels_missing", "err:drive.mtime", "err:drive.missing_on_target",
              "err:drive.grant_missing", "err:contacts.missing_on_target", "err:tasks.list_missing",
              "err:gmail.unmapped_source", "err:drive.content_changed", "err:contacts.unmapped_source")
SEEDED_IMPLEMENTED = (
    "err:gmail.no_message_id", "err:drive.embedded_images", "err:tasks.untitled",
    "err:drive.shortcut_target", "err:drive.comments", "err:run.comment_differs",
    "err:drive.group_grant", "err:drive.sheet_too_large", "err:drive.comments_unreadable",
    "err:drive.item_absent",
    "err:run.malware_flag",
    "err:gmail.labels_added", "err:gmail.duplicate", "err:gmail.body_link_copy",
    "err:gmail.link_owner_not_migrated", "err:gmail.link_scan_encrypted", "err:run.mail_owed_to_dms",
    "err:gmail.link_scan_fixed", "err:drive.sheets_scope", "err:drive.office_compared_as_native",
    "err:drive.comment_quote_escaped",
    "err:run.form_rebuild",
)


def _sandbox_domain_guard(monkeypatch):
    import domain_guard
    calls = []
    monkeypatch.setattr(domain_guard, "protected_subject", lambda _address: None)
    monkeypatch.setattr(domain_guard, "is_government", lambda _domain: False)
    monkeypatch.setattr(domain_guard, "refuse_reason", lambda domain, action: calls.append((domain, action)) or None)
    monkeypatch.setattr(CC, "_seed_services", lambda _settings, _user: (_Seed({"writes": []}), None))
    return calls


def test_target_side_error_acts_have_an_offline_pass_and_unhandled_failure(monkeypatch):
    guard_calls = _sandbox_domain_guard(monkeypatch)
    for scenario_id in IMPLEMENTED:
        sc = S.by_id()[scenario_id]
        positive = _ErrorActDriver(handled=True)
        monkeypatch.setattr(CC, "_seed_services", lambda _settings, _user, state=positive.state: (_Seed(state), None))
        result = C.run_scenario(sc, positive, "offline", set())
        assert result["status"] == "PASS", (scenario_id, result)
        events = [event[0] for event in positive.events]
        assert events.index("api") < events.index("change") < events.index("verify"), (scenario_id, positive.events)
        request = next(event[3] for event in positive.events if event[0] == "api")
        assert request["services"] == [sc.expect["service"]]
        assert positive.state["writes"], scenario_id

        negative = _ErrorActDriver(handled=False)
        monkeypatch.setattr(CC, "_seed_services", lambda _settings, _user, state=negative.state: (_Seed(state), None))
        failed = C.run_scenario(sc, negative, "offline", set())
        assert failed["status"] == "FAIL", (scenario_id, failed)

    assert len(guard_calls) == 2 * len(IMPLEMENTED)
    target_side = {"gmail.labels_missing", "drive.mtime", "drive.missing_on_target", "drive.grant_missing",
                   "contacts.missing_on_target", "tasks.list_missing"}
    assert all(domain == ("b.sandbox.test" if sid.split(":", 1)[1] in target_side else "a.sandbox.test")
               for sid, (domain, _action) in zip((sid for sid in IMPLEMENTED for _ in range(2)), guard_calls))


class _SeededActDriver(C.Driver):
    def __init__(self, scenario, handled=True):
        self.scenario, self.handled, self.events = scenario, handled, []

    def fixture_user(self, name):
        return f"{name}@a.sandbox.test"

    def prepare(self, fixtures, user=None):
        self.events.append(("prepare", fixtures, user))

    def api(self, method, path, body):
        self.events.append(("api", method, path, body))
        return {"ok": True}

    def wait_idle(self, _timeout):
        self.events.append(("wait",))
        return True

    def verify(self, user, service):
        self.events.append(("verify", user, service))

    def source_has(self, kind, user):
        self.events.append(("source_has", kind, user))
        return True

    def facts(self, _user, since=None):
        kind = self.scenario.id.split(":", 1)[1]
        import known_issues
        import parity_gate
        entry = known_issues.BY_ID.get(kind) or known_issues.RUN_BY_ID[kind]
        end = "fixed" if entry[6] else "accepted" if kind in parity_gate.ACCEPTED_KINDS else "listed"
        service = self.scenario.expect.get("service") or ("drive" if kind.startswith("run.") else kind.split(".", 1)[0])
        evidence = self.scenario.expect.get("or_evidence")
        handled_absence = self.scenario.how == "must_not_happen"
        findings = set() if self.handled and (evidence or handled_absence) else {kind}
        audit_counts = {tuple(evidence): 1} if self.handled and evidence else {}
        return {"findings": findings, "fixed": {kind} if self.handled and end == "fixed" else set(),
                "gate": {"accepted": {kind} if self.handled and end == "accepted" else set(),
                         "listed": {kind} if self.handled and end == "listed" else set()},
                "audit_counts": audit_counts, "verdicts": {service: "IDENTICAL"}}


def test_seeded_fixture_acts_run_through_the_driver_and_fail_when_unhandled():
    import json
    import os
    import sys

    sys.path.insert(0, os.path.join(C.HERE, "data-generator"))
    import fixtures

    cohort_path = os.path.join(C.HERE, "data-generator", "cohorts", "fableplan2.json")
    with open(cohort_path, encoding="utf-8") as fh:
        cohort = json.load(fh)
    owners = {u["localpart"]: set(u.get("fixtures") or ()) for u in cohort["users"]}
    for scenario_id in SEEDED_IMPLEMENTED:
        sc = S.by_id()[scenario_id]
        owner = sc.fixtures[0]
        assert sc.act["path"] == "/api/v2/migrate/start"
        assert sc.seed_fixtures and set(sc.seed_fixtures) <= set(fixtures.REGISTRY)
        assert set(sc.seed_fixtures) <= owners[owner]

        positive = _SeededActDriver(sc, handled=True)
        passed = C.run_scenario(sc, positive, "offline", set())
        assert passed["status"] == "PASS", (scenario_id, passed)
        prepare = next(e for e in positive.events if e[0] == "prepare")
        assert prepare[1:] == (sc.seed_fixtures, f"{owner}@a.sandbox.test")
        kinds = [e[0] for e in positive.events]
        assert kinds.index("prepare") < kinds.index("api") < kinds.index("verify")
        if sc.expect.get("or_source") or sc.expect.get("needs_source"):
            assert kinds.index("verify") < kinds.index("source_has")
        else:
            assert "source_has" not in kinds
        followups = sc.act.get("after", ())
        if followups:
            apis = [event for event in positive.events if event[0] == "api"]
            assert len(apis) == 1 + len(followups)
            assert apis[-1][2] == followups[0]["path"]
            assert apis[-1][3] == followups[0]["body"]
            assert positive.events.index(apis[-1]) < max(i for i, event in enumerate(positive.events) if event[0] == "verify")

        negative = _SeededActDriver(sc, handled=False)
        failed = C.run_scenario(sc, negative, "offline", set())
        assert failed["status"] == "FAIL", (scenario_id, failed)


def test_live_fixture_prepare_uses_the_guarded_product_seed_route(monkeypatch):
    import config
    import job_admission
    import webui

    monkeypatch.setattr(config, "Settings", lambda **_kw: SimpleNamespace(source_domain="a.sandbox.test"))
    rows = iter(({"started": 10}, {"started": 20, "finished": 21, "rc": 0}))
    monkeypatch.setattr(webui, "load_job_result", lambda *_args: next(rows))
    monkeypatch.setattr(job_admission, "list_active", lambda: [])

    driver = object.__new__(C.LiveDriver)
    driver.account_id, driver.name, driver.poll = 3, "offline", 1
    driver.sleep = lambda _seconds: None
    calls = []

    def post(method, path, body):
        calls.append((method, path, body))
        return {"ok": True, "queued": False}

    driver.webui = post
    C.LiveDriver.prepare(driver, ("tasks_shapes",), "c07@a.sandbox.test")
    assert calls == [("POST", "/api/seed", {
        "confirm_domain": "a.sandbox.test", "cohort": "fableplan2", "users": "c07",
        "fixtures_only": True, "reason": "campaign offline: fixtures for c07",
    })]

