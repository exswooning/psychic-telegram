"""
tests/test_provision_gcp.py
===========================
Provisioning the Cloud side without a human in the loop.

Every behaviour pinned here is one that actually went wrong while doing
this by hand against a real org, and each failed in a way that looked like
success:

  * a batched `services enable` that fails naming none of the APIs
  * an org policy that blocks key creation and leaves a ZERO-BYTE file
    where the key should be, so "does the file exist" says yes
  * gcloud prompting (y/N) with no tty, which hangs a UI-launched job
    forever rather than failing
  * an IAM binding applied to a service account Google has not finished
    creating

These are cheap to assert and expensive to rediscover.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import provision_gcp as pg  # noqa: E402


class TestGcloudReadyDetection:
    """gcloud_ready() decides whether Quick Setup even attempts to run --
    a false "ready" here surfaces as a much more confusing failure deep
    inside provision_side() instead of the clean, actionable message this
    function exists to give up front."""

    def _fake_run(self, monkeypatch, stdout: str, returncode: int = 0):
        def fake_run(argv, capture_output, text, timeout, stdin, env=None):
            class R:
                pass
            r = R()
            r.returncode = returncode
            r.stdout = stdout
            r.stderr = ""
            return r
        monkeypatch.setattr(pg.subprocess, "run", fake_run)
        monkeypatch.setattr(pg.shutil, "which", lambda name: "/usr/bin/gcloud")

    def test_a_real_active_account_is_recognised(self, monkeypatch):
        self._fake_run(monkeypatch, "someone@example.com\n")
        ready, account = pg.gcloud_ready()
        assert ready is True
        assert account == "someone@example.com"

    def test_a_gcloud_warning_line_is_not_mistaken_for_an_account(self, monkeypatch):
        """Regression: an empty `gcloud auth list` on some gcloud versions
        prints a diagnostic line to the combined stdout+stderr this reads
        ("WARNING: The following filter keys were not present in any
        resource : status") instead of just being blank -- that line was
        being read as if it were the account name, reporting ready=True
        with no real authenticated account at all."""
        self._fake_run(
            monkeypatch,
            "WARNING: The following filter keys were not present in any resource : status\n")
        ready, detail = pg.gcloud_ready()
        assert ready is False
        assert "no active gcloud account" in detail

    def test_truly_empty_output_is_not_ready(self, monkeypatch):
        self._fake_run(monkeypatch, "")
        ready, detail = pg.gcloud_ready()
        assert ready is False
        assert "no active gcloud account" in detail

    def test_gcloud_not_on_path_fails_before_any_gcloud_call(self, monkeypatch):
        monkeypatch.setattr(pg.shutil, "which", lambda name: None)
        ready, detail = pg.gcloud_ready()
        assert ready is False
        assert "not installed" in detail


class TestGcloudIsNeverInteractive:
    """A prompt with no tty is a job that hangs to its timeout with nothing
    in the log explaining why. From a UI button that is indistinguishable
    from a crash, and much harder to diagnose than a clean failure."""

    def test_quiet_is_injected_into_every_gcloud_call(self, monkeypatch):
        seen = {}

        def fake_run(argv, capture_output, text, timeout, stdin, env=None):
            seen["argv"] = argv
            seen["stdin"] = stdin

            class R:
                returncode = 0
                stdout = ""
                stderr = ""
            return R()

        monkeypatch.setattr(pg.subprocess, "run", fake_run)
        pg.run(["gcloud", "projects", "describe", "p1"])
        assert seen["argv"][:2] == ["gcloud", "--quiet"]
        assert seen["stdin"] == pg.subprocess.DEVNULL

    def test_quiet_is_not_duplicated(self, monkeypatch):
        seen = {}

        def fake_run(argv, **kw):
            seen["argv"] = argv

            class R:
                returncode = 0
                stdout = ""
                stderr = ""
            return R()

        monkeypatch.setattr(pg.subprocess, "run", fake_run)
        pg.run(["gcloud", "--quiet", "projects", "list"])
        assert seen["argv"].count("--quiet") == 1

    def test_a_non_gcloud_command_is_left_alone(self, monkeypatch):
        seen = {}

        def fake_run(argv, **kw):
            seen["argv"] = argv

            class R:
                returncode = 0
                stdout = ""
                stderr = ""
            return R()

        monkeypatch.setattr(pg.subprocess, "run", fake_run)
        pg.run(["echo", "hi"])
        assert "--quiet" not in seen["argv"]


class TestApisAreEnabledIndividually:
    def test_one_call_per_api_not_one_batched_call(self, monkeypatch):
        """`gcloud services enable a b c ...` fails on a freshly created
        project with SERVICE_CONFIG_NOT_FOUND_OR_PERMISSION_DENIED, naming
        none of them, while the same list enabled one at a time succeeds
        every time. Slower and correct beats faster and flaky for a step
        that runs once per tenant."""
        calls = []
        monkeypatch.setattr(pg, "run",
                            lambda argv, **kw: (calls.append(argv), (0, ""))[1])
        steps: list[pg.Step] = []
        pg.enable_apis("p1", ["a.googleapis.com", "b.googleapis.com"],
                       steps, dry_run=False)
        assert len(calls) == 2
        for c in calls:
            apis = [a for a in c if a.endswith(".googleapis.com")]
            assert len(apis) == 1, f"batched enable: {c}"

    def test_a_failed_api_is_reported_not_swallowed(self, monkeypatch):
        monkeypatch.setattr(pg, "run", lambda argv, **kw: (1, "PERMISSION_DENIED"))
        steps: list[pg.Step] = []
        ok = pg.enable_apis("p1", ["a.googleapis.com"], steps, dry_run=False)
        assert ok is False
        assert steps[0].status == "failed"
        assert "PERMISSION_DENIED" in steps[0].detail


class TestKeyCreationIsVerifiedNotAssumed:
    """The org policy iam.managed.disableServiceAccountKeyCreation makes
    gcloud fail AND leave a zero-byte file behind. A caller that checks only
    for the file's existence carries on with an empty credential and fails
    later, somewhere unrelated."""

    def test_a_zero_byte_key_is_a_failure_and_is_removed(self, tmp_path, monkeypatch):
        dest = tmp_path / "sa.json"

        def fake_run(argv, **kw):
            dest.write_text("")          # exactly what gcloud does here
            return 1, "CUSTOM_ORG_POLICY_VIOLATION"

        monkeypatch.setattr(pg, "run", fake_run)
        steps: list[pg.Step] = []
        ok = pg.create_key("p1", "sa@p1", str(dest), steps, False, False)
        assert ok is False
        assert steps[0].status == "failed"
        assert not dest.exists(), "an empty file that looks like a key was left behind"

    def test_a_non_json_key_is_a_failure(self, tmp_path, monkeypatch):
        dest = tmp_path / "sa.json"

        def fake_run(argv, **kw):
            dest.write_text("<html>error page</html>")
            return 0, ""

        monkeypatch.setattr(pg, "run", fake_run)
        steps: list[pg.Step] = []
        assert pg.create_key("p1", "sa@p1", str(dest), steps, False, False) is False

    def test_a_real_key_succeeds_and_is_chmod_600(self, tmp_path, monkeypatch):
        dest = tmp_path / "sa.json"

        def fake_run(argv, **kw):
            dest.write_text(json.dumps({"client_id": "123", "project_id": "p1"}))
            return 0, ""

        monkeypatch.setattr(pg, "run", fake_run)
        steps: list[pg.Step] = []
        assert pg.create_key("p1", "sa@p1", str(dest), steps, False, False) is True
        assert oct(dest.stat().st_mode)[-3:] == "600"
        assert pg.client_id_of(str(dest)) == "123"

    def test_an_existing_key_is_not_clobbered(self, tmp_path, monkeypatch):
        """Replacing a working key strands whatever is already deployed with
        the old file."""
        dest = tmp_path / "sa.json"
        dest.write_text(json.dumps({"client_id": "original"}))
        monkeypatch.setattr(pg, "run",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("must not call gcloud")))
        steps: list[pg.Step] = []
        assert pg.create_key("p1", "sa@p1", str(dest), steps, False, force=False)
        assert steps[0].status == "skipped"
        assert pg.client_id_of(str(dest)) == "original"


class TestServiceAccountPropagation:
    def test_iam_binding_retries_while_the_sa_propagates(self, monkeypatch):
        """Service account creation is eventually consistent -- binding a
        role to an identity Google has not finished creating fails, and
        did so on the live run."""
        attempts = {"n": 0}

        def fake_run(argv, **kw):
            attempts["n"] += 1
            return (0, "") if attempts["n"] >= 3 else (1, "does not exist")

        monkeypatch.setattr(pg, "run", fake_run)
        monkeypatch.setattr(pg.time, "sleep", lambda s: None)
        steps: list[pg.Step] = []
        pg.grant_service_usage("p1", "sa@p1", steps, dry_run=False)
        assert steps[0].status == "ok"
        assert attempts["n"] == 3


class TestOrgDetection:
    def test_exactly_one_org_is_used(self, monkeypatch):
        monkeypatch.setattr(pg, "run", lambda argv, **kw: (0, "35602275582\n"))
        assert pg.detect_org() == "35602275582"

    def test_ambiguity_is_not_guessed(self, monkeypatch):
        """Silently picking the wrong org puts every project somewhere the
        operator did not intend, inheriting policies they did not choose."""
        monkeypatch.setattr(pg, "run", lambda argv, **kw: (0, "111\n222\n"))
        assert pg.detect_org() == ""

    def test_failure_to_list_is_not_fatal(self, monkeypatch):
        monkeypatch.setattr(pg, "run", lambda argv, **kw: (1, "SERVICE_DISABLED"))
        assert pg.detect_org() == ""


class TestDryRunTouchesNothing:
    def test_dry_run_never_shells_out(self, monkeypatch):
        monkeypatch.setattr(pg, "run",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("dry run must not call gcloud")))
        steps: list[pg.Step] = []
        pg.enable_apis("p1", ["a.googleapis.com"], steps, dry_run=True)
        pg.create_key("p1", "sa@p1", "/tmp/nope.json", steps, True, False)
        pg.grant_service_usage("p1", "sa@p1", steps, dry_run=True)
        assert all(s.status == "skipped" for s in steps)
        assert not os.path.exists("/tmp/nope.json")
