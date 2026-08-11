"""
tests/test_ensure_apis.py
=========================
Cloud API enablement -- the second, independent gate in front of every
Google call, and the one nothing in this project checked.

Domain-wide delegation and API enablement live in different consoles and
fail with different errors. Passing one says nothing about the other: the
source tenant had 17/17 DWD scopes live, including `contacts` and `tasks`,
while People and Tasks were never enabled on the GCP project -- so
seed_contacts and seed_tasks failed on every user, swallowed the exception
into a `note`, and the run reported success having produced nothing.

The property that matters most here is the three-way distinction between
ENABLED, DISABLED and UNKNOWN. Collapsing "cannot ask" into "off" would
send an operator to enable APIs that are already on -- which is exactly the
state of the live source project, where the service account cannot read
serviceusage but Drive and Gmail are demonstrably working.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ensure_apis as ea  # noqa: E402


class TestUnknownIsNotDisabled:
    def test_permission_denied_reads_as_unknown(self, monkeypatch):
        """The live case: the SA cannot read serviceusage on its own project,
        yet Drive and Gmail are plainly enabled -- the migration just used
        them. Reporting those as DISABLED would be a lie that sends someone
        to fix a non-problem."""
        class _Svc:
            def services(self):
                return self

            def get(self, name):
                raise Exception("<HttpError 403 ... PERMISSION_DENIED>")

        monkeypatch.setattr(ea, "_service_usage", lambda key: _Svc())
        states = ea.check("/fake/key.json", "proj-1")
        assert set(states.values()) == {"UNKNOWN: no serviceusage permission"}
        assert "DISABLED" not in states.values()

    def test_a_disabled_api_is_reported_as_disabled(self, monkeypatch):
        class _Svc:
            def services(self):
                return self

            def get(self, name):
                self._n = name
                return self

            def execute(self):
                state = "DISABLED" if "people" in self._n else "ENABLED"
                return {"state": state}

        monkeypatch.setattr(ea, "_service_usage", lambda key: _Svc())
        states = ea.check("/fake/key.json", "proj-1")
        assert states["people.googleapis.com"] == "DISABLED"
        assert states["drive.googleapis.com"] == "ENABLED"

    def test_unknown_does_not_make_the_run_fail(self, monkeypatch, tmp_path):
        """A tool that cannot check must not block a migration that would
        have worked -- `ok` is about known-bad, not about certainty."""
        key = tmp_path / "k.json"
        key.write_text('{"project_id": "proj-1"}')

        monkeypatch.setattr(ea, "check", lambda k, p: {
            a: "UNKNOWN: no serviceusage permission" for a in ea.REQUIRED_APIS})

        class _S:
            source_sa_key = str(key)
            target_sa_key = str(key)
        res = ea.ensure(_S(), "source")
        assert res["ok"] is True
        assert res["disabled"] == []
        assert len(res["unknown"]) == len(ea.REQUIRED_APIS)


class TestTheAdviceIsActionable:
    def test_advice_names_the_project_id_not_a_number(self):
        """Google's own error quotes a project NUMBER ('project 881431668245')
        which nobody recognises. The fix has to name the project ID."""
        out = ea.advice("workspace-migrator-503709", ["people.googleapis.com"])
        assert "workspace-migrator-503709" in out
        assert "gcloud services enable people.googleapis.com" in out

    def test_render_separates_disabled_from_unknown(self):
        res = {"tenant": "source", "project": "p1",
               "states": {"people.googleapis.com": "DISABLED",
                          "drive.googleapis.com": "UNKNOWN: no permission"},
               "disabled": ["people.googleapis.com"],
               "unknown": ["drive.googleapis.com"], "enabled_now": {}}
        # Only the two named APIs, so render() must not KeyError on the rest.
        res["states"] = {k: v for k, v in res["states"].items()
                         if k in ea.REQUIRED_APIS}
        out = ea.render(res)
        assert "DISABLED" in out
        assert "not the same as 'off'" in out


class TestServiceDisabledHint:
    """resilience turns Google's SERVICE_DISABLED text into the one command
    that fixes it, and says the thing the original never does: that this is
    API enablement, not a missing scope."""

    def test_hint_names_the_api_the_project_and_the_distinction(self):
        import resilience

        blob = ('<HttpError 403 ... "People API has not been used in project '
                'workspace-migrator-503709 before or it is disabled. Enable it '
                'by visiting https://console.developers.google.com/apis/api/'
                'people.googleapis.com/overview?project=workspace-migrator-503709')
        hint = resilience._service_disabled_hint(Exception(blob), "SERVICE_DISABLED")
        assert "people.googleapis.com" in hint
        assert "workspace-migrator-503709" in hint
        assert "not a DWD scope" in hint

    def test_no_hint_for_unrelated_failures(self):
        """An unrelated 403 must not be decorated with irrelevant advice."""
        import resilience

        assert resilience._service_disabled_hint(
            Exception("403 insufficientFilePermissions"),
            "insufficientFilePermissions") == ""


class TestEnabledIsNotAlwaysUsable:
    """
    `gcloud services enable chat.googleapis.com` succeeds, serviceusage
    reports ENABLED, and every Chat call still returns
    `404 Google Chat app not found` until a Chat *app* is configured in the
    Cloud console -- a step with no API and no gcloud command.

    Found by provisioning fresh projects: contacts and tasks started
    working (they were the whole point) and Chat stopped, because the old
    hand-made project had the app configured years earlier and the new one
    did not. "ENABLED" alone would have been a green light onto a 404.
    """

    def test_chat_carries_its_manual_step(self):
        assert "chat.googleapis.com" in ea.NEEDS_CONSOLE_CONFIG
        note = ea.NEEDS_CONSOLE_CONFIG["chat.googleapis.com"]
        assert "404" in note and "hangouts-chat" in note

    def test_the_caveat_is_shown_when_chat_is_enabled(self):
        res = {"tenant": "source", "project": "p1",
               "states": {a: "ENABLED" for a in ea.REQUIRED_APIS},
               "disabled": [], "unknown": [], "enabled_now": {}}
        out = ea.render(res)
        assert "NOT yet usable" in out
        assert "chat.googleapis.com" in out

    def test_no_caveat_when_chat_is_off(self):
        """A disabled API is already reported under DISABLED; repeating it
        as a caveat would imply two separate problems."""
        states = {a: "ENABLED" for a in ea.REQUIRED_APIS}
        states["chat.googleapis.com"] = "DISABLED"
        res = {"tenant": "source", "project": "p1", "states": states,
               "disabled": ["chat.googleapis.com"], "unknown": [],
               "enabled_now": {}}
        assert "NOT yet usable" not in ea.render(res)
