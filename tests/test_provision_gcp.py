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


class TestEnvPropagation:
    """gcloud_browser_auth.login() hands full_setup.py an isolated
    CLOUDSDK_CONFIG per tenant so two clients' credentials on this shared
    box can never cross-contaminate each other -- which only actually
    works if every one of these functions passes the `env` it was given
    all the way down to the real subprocess.run() call, not just to the
    first gcloud invocation each makes."""

    def _fake_run(self, monkeypatch, seen: list):
        def fake_run(argv, capture_output, text, timeout, stdin, env=None):
            seen.append(env)

            class R:
                returncode = 0
                stdout = "ok\n"
                stderr = ""
            return R()
        monkeypatch.setattr(pg.subprocess, "run", fake_run)

    def test_gcloud_ready_passes_env_through(self, monkeypatch):
        seen: list = []
        self._fake_run(monkeypatch, seen)
        monkeypatch.setattr(pg.shutil, "which", lambda name: "/usr/bin/gcloud")
        marker = {"CLOUDSDK_CONFIG": "/tmp/tenant-a"}
        pg.gcloud_ready(env=marker)
        assert seen == [marker]

    def test_detect_org_passes_env_through(self, monkeypatch):
        seen: list = []
        self._fake_run(monkeypatch, seen)
        marker = {"CLOUDSDK_CONFIG": "/tmp/tenant-a"}
        pg.detect_org(env=marker)
        assert seen == [marker]

    def test_ensure_project_passes_env_through_both_the_describe_and_the_create_call(self, monkeypatch):
        seen: list = []
        self._fake_run(monkeypatch, seen)
        monkeypatch.setattr(pg.subprocess, "run", lambda argv, **kw: (
            seen.append(kw.get("env")), type("R", (), {
                "returncode": 1 if "describe" in argv else 0,
                "stdout": "", "stderr": "",
            })())[1])
        marker = {"CLOUDSDK_CONFIG": "/tmp/tenant-a"}
        steps: list[pg.Step] = []
        pg.ensure_project("p1", "", steps, dry_run=False, env=marker)
        assert seen == [marker, marker]

    def test_enable_apis_passes_env_through_every_call(self, monkeypatch):
        seen: list = []
        self._fake_run(monkeypatch, seen)
        marker = {"CLOUDSDK_CONFIG": "/tmp/tenant-a"}
        steps: list[pg.Step] = []
        pg.enable_apis("p1", ["a.googleapis.com", "b.googleapis.com"], steps,
                       dry_run=False, env=marker)
        assert seen == [marker, marker]

    def test_ensure_service_account_passes_env_through(self, monkeypatch):
        seen: list = []
        self._fake_run(monkeypatch, seen)
        marker = {"CLOUDSDK_CONFIG": "/tmp/tenant-a"}
        steps: list[pg.Step] = []
        pg.ensure_service_account("p1", "src-sa", steps, dry_run=False, env=marker)
        assert marker in seen

    def test_grant_service_usage_passes_env_through(self, monkeypatch):
        seen: list = []
        self._fake_run(monkeypatch, seen)
        marker = {"CLOUDSDK_CONFIG": "/tmp/tenant-a"}
        steps: list[pg.Step] = []
        pg.grant_service_usage("p1", "sa@p1.iam.gserviceaccount.com", steps,
                               dry_run=False, env=marker)
        assert seen == [marker]

    def test_relax_key_policy_uses_the_given_env_not_a_bare_os_environ(self, monkeypatch):
        """Regression: this function used to build its own env from a bare
        os.environ, silently dropping any CLOUDSDK_CONFIG override the
        caller passed in -- gcloud would then fall back to whatever config
        directory this process defaults to instead of the tenant's
        ephemeral one."""
        seen: list = []
        self._fake_run(monkeypatch, seen)
        marker = {"CLOUDSDK_CONFIG": "/tmp/tenant-a"}
        steps: list[pg.Step] = []
        pg.relax_key_policy("p1", steps, dry_run=False, env=marker)
        assert seen, "no gcloud call observed"
        assert all(e is not None and e.get("CLOUDSDK_CONFIG") == "/tmp/tenant-a"
                  for e in seen)

    def test_create_key_passes_env_through(self, monkeypatch, tmp_path):
        seen: list = []

        def fake_run(argv, **kw):
            seen.append(kw.get("env"))
            # Emulate `gcloud ... keys create <dest> ...` writing a real key.
            dest = argv[6]
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write('{"client_id": "42"}')

            class R:
                returncode = 0
                stdout = ""
                stderr = ""
            return R()

        monkeypatch.setattr(pg.subprocess, "run", fake_run)
        marker = {"CLOUDSDK_CONFIG": "/tmp/tenant-a"}
        steps: list[pg.Step] = []
        dest = str(tmp_path / "key.json")
        ok = pg.create_key("p1", "sa@p1.iam.gserviceaccount.com", dest, steps,
                           dry_run=False, force=False, env=marker)
        assert ok is True
        assert seen == [marker]

    def test_provision_side_threads_env_into_every_step(self, monkeypatch, tmp_path):
        """The end-to-end path gcloud_browser_auth's caller actually uses --
        every gcloud call this makes for one tenant must land in that
        tenant's own isolated config, not a shared/default one."""
        seen: list = []

        def fake_run(argv, **kw):
            seen.append(kw.get("env"))
            if "describe" in argv:
                return type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})()
            if argv[:4] == ["gcloud", "--quiet", "iam", "service-accounts"] and "keys" in argv:
                dest = argv[argv.index("create") + 1]
                with open(dest, "w", encoding="utf-8") as fh:
                    fh.write('{"client_id": "42"}')
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        monkeypatch.setattr(pg.subprocess, "run", fake_run)
        marker = {"CLOUDSDK_CONFIG": "/tmp/tenant-a"}
        dest = str(tmp_path / "key.json")
        result = pg.provision_side("source", "p1", "", dest, dry_run=False,
                                   force=False, env=marker)
        assert result["ok"] is True
        assert seen, "no gcloud call observed"
        # relax_key_policy() legitimately layers CLOUDSDK_CORE_PROJECT on
        # top for two of these calls -- CLOUDSDK_CONFIG surviving into
        # every single one is the actual property that matters here.
        assert all(e is not None and e.get("CLOUDSDK_CONFIG") == "/tmp/tenant-a"
                  for e in seen)


class TestOnStepProgress:
    """full_setup.py's progress bar has nothing else to go on but this --
    a caller who only learns "done" at the very end, several minutes in
    for a real run, is the exact gap on_step exists to close."""

    def test_enable_apis_calls_on_step_once_per_api_not_once_per_list(self, monkeypatch):
        monkeypatch.setattr(pg, "run", lambda *a, **k: (0, "ok"))
        seen = []
        steps: list[pg.Step] = []
        pg.enable_apis("p1", ["a.googleapis.com", "b.googleapis.com", "c.googleapis.com"],
                       steps, dry_run=False, on_step=lambda s: seen.append(s.name))
        assert len(seen) == 3
        assert seen[0].startswith("enable a.googleapis.com")
        assert seen[2].startswith("enable c.googleapis.com")

    def test_enable_apis_on_step_fires_even_under_dry_run(self, monkeypatch):
        """Preview doesn't call provision_side with on_step today, but the
        callback itself must not silently skip dry-run steps -- a future
        caller that wants a preview progress bar should get one for free."""
        seen = []
        steps: list[pg.Step] = []
        pg.enable_apis("p1", ["a.googleapis.com"], steps, dry_run=True,
                       on_step=lambda s: seen.append(s.status))
        assert seen == ["skipped"]

    def test_enable_apis_on_step_reports_failures_too(self, monkeypatch):
        monkeypatch.setattr(pg, "run", lambda *a, **k: (1, "PERMISSION_DENIED"))
        seen = []
        steps: list[pg.Step] = []
        pg.enable_apis("p1", ["a.googleapis.com"], steps, dry_run=False,
                       on_step=lambda s: seen.append(s.status))
        assert seen == ["failed"]

    def test_provision_side_on_step_covers_every_step_not_just_apis(self, monkeypatch):
        """The project-create, service-account, grant, relax, and key steps
        each fire on_step too -- a progress bar built only on the API loop
        would sit frozen through the rest of provisioning."""
        monkeypatch.setattr(pg, "run", lambda *a, **k: (0, "ok"))
        seen = []
        result = pg.provision_side(
            "source", "p1", "", "/tmp/nope-key.json", dry_run=True, force=False,
            on_step=lambda done, total, name: seen.append((done, total, name)))
        assert result["ok"] is True
        # 1 project + N APIs + service account + grant + self-grant-orgpolicy + relax + key
        expected_total = 6 + len(pg.SUPPORT_APIS + pg.APIS)
        assert seen[-1][1] == expected_total
        assert seen[-1][0] == expected_total
        names = [s[2] for s in seen]
        assert "service account source-sa" in names
        assert any(n.startswith("key -> ") for n in names)

    def test_on_step_done_count_strictly_increases(self, monkeypatch):
        monkeypatch.setattr(pg, "run", lambda *a, **k: (0, "ok"))
        seen = []
        pg.provision_side("source", "p1", "", "/tmp/nope-key2.json", dry_run=True,
                          force=False,
                          on_step=lambda done, total, name: seen.append(done))
        assert seen == sorted(seen)
        assert len(seen) == len(set(seen)), "on_step fired twice for the same step count"

    def test_a_failed_project_create_still_reports_one_step(self, monkeypatch):
        """ensure_project() returning False short-circuits provision_side
        before any API is touched -- on_step must still fire once, so a
        progress bar shows SOMETHING moved rather than staying at 0%
        through to the final failure."""
        monkeypatch.setattr(pg, "run", lambda *a, **k: (1, "quota exceeded"))
        seen = []
        result = pg.provision_side(
            "source", "p1", "", "/tmp/nope-key3.json", dry_run=False, force=False,
            on_step=lambda done, total, name: seen.append((done, total)))
        assert result["ok"] is False
        assert seen == [(1, 6 + len(pg.SUPPORT_APIS + pg.APIS))]


class TestKnownProjectCreateFailures:
    """Confirmed live against a real, brand-new Google account: project
    creation fails with 'Callers must accept Terms of Service' the first
    time any identity ever tries it. gcloud_browser_auth.py now clears
    that proactively during sign-in, but this is the fallback explanation
    if it somehow doesn't -- and the only place billing/quota gates (which
    nothing here can auto-clear) get explained at all."""

    def test_tos_error_gets_a_clear_actionable_explanation(self):
        raw = (
            "ERROR: (gcloud.projects.create) Operation "
            "[create_project.global.123] failed: 9: Callers must accept "
            "Terms of Service\n- '@type': type.googleapis.com/google.rpc."
            "PreconditionFailure\n  violations:\n  - description: Callers "
            "must accept Terms of Service\n    subject: cloud\n    type: TOS")
        explained = pg._explain_project_create_failure(raw)
        assert "console.cloud.google.com" in explained
        assert "should already have been" not in explained  # not garbled mid-sentence
        assert "gcloud_browser_auth.py" in explained

    def test_billing_error_points_at_billing_not_a_retry(self):
        raw = "ERROR: (gcloud.projects.create) billing account is required"
        explained = pg._explain_project_create_failure(raw)
        assert "billing" in explained.lower()
        assert "console.cloud.google.com/billing" in explained

    def test_quota_error_points_at_quota_not_a_bug(self):
        raw = "ERROR: (gcloud.projects.create) quota exceeded for quota metric"
        explained = pg._explain_project_create_failure(raw)
        assert "quota" in explained.lower()

    def test_an_unrecognised_error_passes_through_unchanged(self):
        """Anything not in the known list must reach the operator as-is --
        guessing wrong here would hide the real cause behind a wrong
        explanation."""
        raw = "ERROR: (gcloud.projects.create) some entirely new failure mode"
        assert pg._explain_project_create_failure(raw) == raw

    def test_ensure_project_uses_the_explanation_not_the_raw_text(self, monkeypatch):
        monkeypatch.setattr(pg, "run", lambda argv, **kw: (
            (1, "no active checkbox") if "describe" in argv
            else (1, "Callers must accept Terms of Service")))
        steps: list[pg.Step] = []
        ok = pg.ensure_project("p1", "", steps, dry_run=False)
        assert ok is False
        assert "console.cloud.google.com" in steps[0].detail


class TestKnownKeyCreateFailures:
    """Confirmed live, right after the ToS fix itself worked: project
    creation, all API enables, service account creation, and the IAM
    grant all succeeded -- only key creation failed, because this
    identity (a Workspace super admin, but not automatically an
    Organization Policy Administrator on the GCP side) can't relax the
    org's disableServiceAccountKeyCreation constraint. A real permissions
    gap, not something a retry or a different URL fixes -- the value here
    is explaining it clearly, not attempting to bypass it."""

    def test_org_policy_violation_explains_the_real_permissions_gap(self):
        raw = (
            "ERROR: ... google.rpc.ErrorInfo\n  domain: iam.googleapis.com\n"
            "  metadata:\n    customConstraints: constraints/iam.managed."
            "disableServiceAccountKeyCreation\n"
            "    resource: projects/-/serviceAccounts/123\n"
            "  reason: CUSTOM_ORG_POLICY_VIOLATION")
        explained = pg._explain_key_create_failure(raw)
        assert "Organization Policy Administrator" in explained
        assert "Workspace super admin" in explained
        assert "Manual tab" in explained

    def test_generic_permission_denied_still_points_at_an_org_admin(self):
        raw = "ERROR: (gcloud.iam...) PERMISSION_DENIED... reason: IAM_PERMISSION_DENIED"
        explained = pg._explain_key_create_failure(raw)
        assert "Organization Administrator" in explained

    def test_an_unrecognised_key_error_passes_through_unchanged(self):
        raw = "ERROR: some entirely new failure mode never seen before"
        assert pg._explain_key_create_failure(raw) == raw

    def test_create_key_uses_the_explanation_not_the_raw_text(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pg, "run", lambda argv, **kw: (
            1, "reason: CUSTOM_ORG_POLICY_VIOLATION disableServiceAccountKeyCreation"))
        monkeypatch.setattr(pg.time, "sleep", lambda s: None)
        steps: list[pg.Step] = []
        dest = str(tmp_path / "key.json")
        ok = pg.create_key("p1", "sa@p1.iam.gserviceaccount.com", dest, steps,
                           dry_run=False, force=False)
        assert ok is False
        assert "Organization Policy Administrator" in steps[0].detail


class TestKeyCreationRetriesThroughOrgPolicyPropagation:
    """Confirmed live: relax_key_policy() reported "ok" -- the org-policy
    write genuinely landed -- and the very next call, create_key(), still
    hit the exact same CUSTOM_ORG_POLICY_VIOLATION it had just cleared.
    Google's org-policy service is eventually consistent; retrying only
    THIS specific error (not a wrong project or missing service account,
    where retrying just wastes five minutes before failing the same way)
    is what turns that into a real fix instead of a slow, identical
    failure."""

    def test_retries_and_succeeds_once_the_policy_propagates(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pg.time, "sleep", lambda s: None)
        dest = tmp_path / "sa.json"
        attempts = {"n": 0}

        def fake_run(argv, **kw):
            attempts["n"] += 1
            if attempts["n"] < 3:
                return 1, "reason: CUSTOM_ORG_POLICY_VIOLATION disableServiceAccountKeyCreation"
            dest.write_text(json.dumps({"client_id": "42"}))
            return 0, ""

        monkeypatch.setattr(pg, "run", fake_run)
        steps: list[pg.Step] = []
        ok = pg.create_key("p1", "sa@p1", str(dest), steps, dry_run=False, force=False)
        assert ok is True
        assert attempts["n"] == 3
        assert steps[0].status == "ok"

    def test_gives_up_after_exhausting_the_retry_budget_with_the_explanation(self, monkeypatch, tmp_path):
        """Confirmed live this budget genuinely needs to be generous:
        three separate real runs against a real org each still hit
        CUSTOM_ORG_POLICY_VIOLATION well past the old, shorter budget."""
        monkeypatch.setattr(pg.time, "sleep", lambda s: None)
        dest = tmp_path / "sa.json"
        attempts = {"n": 0}

        def fake_run(argv, **kw):
            attempts["n"] += 1
            return 1, "reason: CUSTOM_ORG_POLICY_VIOLATION disableServiceAccountKeyCreation"

        monkeypatch.setattr(pg, "run", fake_run)
        steps: list[pg.Step] = []
        ok = pg.create_key("p1", "sa@p1", str(dest), steps, dry_run=False, force=False)
        assert ok is False
        assert attempts["n"] == 7
        assert "Organization Policy Administrator" in steps[0].detail
        assert "Retried 7 times" in steps[0].detail

    def test_a_different_error_is_not_retried_at_all(self, monkeypatch, tmp_path):
        """Retrying an error that has nothing to do with policy
        propagation just delays an inevitable, identical failure --
        confirm this only retries the one error it exists for."""
        monkeypatch.setattr(pg.time, "sleep",
                            lambda s: (_ for _ in ()).throw(
                                AssertionError("must not sleep/retry an unrelated error")))
        dest = tmp_path / "sa.json"
        attempts = {"n": 0}

        def fake_run(argv, **kw):
            attempts["n"] += 1
            return 1, "ERROR: service account does not exist"

        monkeypatch.setattr(pg, "run", fake_run)
        steps: list[pg.Step] = []
        ok = pg.create_key("p1", "sa@p1", str(dest), steps, dry_run=False, force=False)
        assert ok is False
        assert attempts["n"] == 1


class TestRelaxKeyPolicyRetriesThroughApiEnablementPropagation:
    """Confirmed live: on a project created moments earlier in the SAME
    run, `gcloud services enable orgpolicy.googleapis.com` can report
    success while the very next call against that API still fails
    SERVICE_DISABLED -- enabling an API and that API actually being
    callable are not the same moment. This is the other half of the
    org-policy propagation gap create_key()'s own retry covers; without
    this one, relax_key_policy() gives up in one shot and reports
    "skipped" as though the org simply doesn't enforce the constraint,
    which is a different, misleading story from "not yet usable"."""

    def test_retries_set_policy_and_succeeds_once_the_api_is_usable(self, monkeypatch):
        monkeypatch.setattr(pg.time, "sleep", lambda s: None)
        attempts = {"enable": 0, "set_policy": 0}

        def fake_run(argv, **kw):
            if "services" in argv and "enable" in argv:
                attempts["enable"] += 1
                return 0, ""
            if "org-policies" in argv:
                attempts["set_policy"] += 1
                if attempts["set_policy"] < 3:
                    return 1, "reason: SERVICE_DISABLED (orgpolicy.googleapis.com)"
                return 0, ""
            raise AssertionError(f"unexpected call: {argv}")

        monkeypatch.setattr(pg, "run", fake_run)
        steps: list[pg.Step] = []
        pg.relax_key_policy("p1", steps, dry_run=False)
        # steps[0] is now the self-grant-orgpolicy step (skipped -- no org
        # id was passed in this test); steps[1] is "allow SA keys".
        assert steps[1].status == "ok"
        assert attempts["set_policy"] == 3

    def test_a_real_policy_rejection_is_not_retried_and_reported_as_skipped(self, monkeypatch):
        """The existing, correct behaviour for an org that genuinely does
        not enforce the constraint (or rejects the policy file for an
        unrelated reason) must still be a quiet "skipped", not treated as
        a propagation gap and retried four times for nothing."""
        monkeypatch.setattr(pg.time, "sleep",
                            lambda s: (_ for _ in ()).throw(
                                AssertionError("must not retry a non-propagation error")))
        attempts = {"set_policy": 0}

        def fake_run(argv, **kw):
            if "services" in argv and "enable" in argv:
                return 0, ""
            if "org-policies" in argv:
                attempts["set_policy"] += 1
                return 1, "ERROR: policy file malformed"
            raise AssertionError(f"unexpected call: {argv}")

        monkeypatch.setattr(pg, "run", fake_run)
        steps: list[pg.Step] = []
        pg.relax_key_policy("p1", steps, dry_run=False)
        # steps[0] is the self-grant-orgpolicy step, steps[1] is "allow SA
        # keys" -- the one this test is actually about.
        assert steps[1].status == "skipped"
        assert attempts["set_policy"] == 1

    def test_env_still_survives_every_retry_attempt(self, monkeypatch):
        """The CLOUDSDK_CONFIG-preserving fix from TestEnvPropagation must
        keep holding across every retry, not just the first attempt."""
        monkeypatch.setattr(pg.time, "sleep", lambda s: None)
        seen_envs = []

        def fake_run(argv, **kw):
            if "org-policies" in argv:
                seen_envs.append(kw.get("env"))
                return (1, "SERVICE_DISABLED") if len(seen_envs) < 3 else (0, "")
            return 0, ""

        monkeypatch.setattr(pg, "run", fake_run)
        marker = {"CLOUDSDK_CONFIG": "/tmp/tenant-a"}
        steps: list[pg.Step] = []
        pg.relax_key_policy("p1", steps, dry_run=False, env=marker)
        assert len(seen_envs) == 3
        assert all(e.get("CLOUDSDK_CONFIG") == "/tmp/tenant-a" for e in seen_envs)


class TestSelfGrantOrgpolicyAdmin:
    """Confirmed live: a Workspace super admin does not automatically hold
    roles/orgpolicy.policyAdmin (a separate Cloud IAM role) -- a real run
    retried key creation 7 times over ~180s and still hit
    CUSTOM_ORG_POLICY_VIOLATION because of exactly this. An account that
    already holds roles/resourcemanager.organizationAdmin (the org's
    actual owner) CAN self-grant the missing role in one call; this
    function automates the manual fix that used to require an operator
    running gcloud by hand."""

    def test_no_org_id_is_skipped_not_attempted(self, monkeypatch):
        monkeypatch.setattr(pg, "run", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not call gcloud with no org id known")))
        steps: list[pg.Step] = []
        pg._self_grant_orgpolicy_admin("", steps, dry_run=False)
        assert steps[0].status == "skipped"
        assert "no organization id known" in steps[0].detail

    def test_dry_run_is_skipped_without_any_gcloud_call(self, monkeypatch):
        monkeypatch.setattr(pg, "run", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not call gcloud during a dry run")))
        steps: list[pg.Step] = []
        pg._self_grant_orgpolicy_admin("12345", steps, dry_run=True)
        assert steps[0].status == "skipped"
        assert steps[0].detail == "dry run"

    def test_successful_grant_is_reported_ok(self, monkeypatch):
        def fake_run(argv, **kw):
            if argv[:3] == ["gcloud", "config", "get-value"]:
                return 0, "info@target.rohitrokaya.com.np\n"
            if "add-iam-policy-binding" in argv:
                assert "--member=user:info@target.rohitrokaya.com.np" in argv
                assert "--role=roles/orgpolicy.policyAdmin" in argv
                return 0, "updated"
            raise AssertionError(f"unexpected call: {argv}")

        monkeypatch.setattr(pg, "run", fake_run)
        steps: list[pg.Step] = []
        pg._self_grant_orgpolicy_admin("369785633145", steps, dry_run=False)
        assert steps[0].status == "ok"
        assert "info@target.rohitrokaya.com.np" in steps[0].detail

    def test_a_permission_denied_grant_is_skipped_not_fatal(self, monkeypatch):
        """The common case: the account is NOT the org owner and genuinely
        cannot grant IAM roles. Must fall through cleanly (the existing
        manual-intervention message downstream still applies), not raise."""
        def fake_run(argv, **kw):
            if argv[:3] == ["gcloud", "config", "get-value"]:
                return 0, "someone@example.com\n"
            if "add-iam-policy-binding" in argv:
                return 1, "ERROR: PERMISSION_DENIED: caller lacks permission"
            raise AssertionError(f"unexpected call: {argv}")

        monkeypatch.setattr(pg, "run", fake_run)
        steps: list[pg.Step] = []
        pg._self_grant_orgpolicy_admin("369785633145", steps, dry_run=False)
        assert steps[0].status == "skipped"
        assert "cannot grant IAM roles" in steps[0].detail

    def test_cannot_determine_account_is_skipped_not_fatal(self, monkeypatch):
        monkeypatch.setattr(pg, "run", lambda argv, **k: (1, ""))
        steps: list[pg.Step] = []
        pg._self_grant_orgpolicy_admin("369785633145", steps, dry_run=False)
        assert steps[0].status == "skipped"
        assert "could not determine the calling account" in steps[0].detail

    def test_granting_an_already_held_role_is_a_harmless_no_op(self, monkeypatch):
        """gcloud itself returns success for a redundant grant -- this
        function does not need its own "already has it" pre-check, and
        deliberately does not add one (one fewer gcloud round trip, one
        fewer place for the check-then-act race to disagree with reality)."""
        def fake_run(argv, **kw):
            if argv[:3] == ["gcloud", "config", "get-value"]:
                return 0, "already-has-it@example.com\n"
            if "add-iam-policy-binding" in argv:
                return 0, "updated"  # gcloud's own idempotent behaviour
            raise AssertionError(f"unexpected call: {argv}")

        monkeypatch.setattr(pg, "run", fake_run)
        steps: list[pg.Step] = []
        pg._self_grant_orgpolicy_admin("369785633145", steps, dry_run=False)
        assert steps[0].status == "ok"


class TestRelaxKeyPolicyAttemptsSelfGrantFirst:
    """relax_key_policy() itself must call the self-grant before trying
    to relax the constraint -- not just have the function exist."""

    def test_self_grant_runs_before_org_policies_set_policy(self, monkeypatch):
        call_order = []

        def fake_run(argv, **kw):
            if argv[:3] == ["gcloud", "config", "get-value"]:
                call_order.append("self-grant")
                return 0, "admin@example.com\n"
            if "add-iam-policy-binding" in argv:
                return 0, "updated"
            if "services" in argv and "enable" in argv:
                return 0, ""
            if "org-policies" in argv:
                call_order.append("set-policy")
                return 0, ""
            raise AssertionError(f"unexpected call: {argv}")

        monkeypatch.setattr(pg, "run", fake_run)
        steps: list[pg.Step] = []
        pg.relax_key_policy("p1", steps, dry_run=False, org="369785633145")
        assert call_order == ["self-grant", "set-policy"]
        assert steps[0].name == "self-grant orgpolicy.policyAdmin on org 369785633145"
        assert steps[0].status == "ok"

    def test_org_flows_through_from_provision_side(self, monkeypatch):
        """The gap this whole feature closes: provision_side() must
        actually pass its own `org` argument through to relax_key_policy(),
        not just have relax_key_policy() accept one."""
        seen_orgs = []

        def fake_run(argv, **kw):
            if argv[:3] == ["gcloud", "config", "get-value"]:
                seen_orgs.append("called")
                return 0, "admin@example.com\n"
            return 0, ""

        monkeypatch.setattr(pg, "run", fake_run)
        pg.provision_side("source", "p1", "369785633145", "/tmp/nope-key4.json",
                          dry_run=False, force=False)
        assert seen_orgs == ["called"]


class TestDeleteProject:
    """Confirmed live: `gcloud projects delete` soft-deletes (Google holds
    the project 30 days, recoverable via undelete) -- this tool makes no
    promise beyond calling that command and reading its result."""

    def test_success_says_recoverable_not_gone_forever(self, monkeypatch):
        monkeypatch.setattr(pg, "run", lambda argv, **kw: (0, "ok"))
        ok, detail = pg.delete_project("wsmig-src-12345")
        assert ok is True
        assert "30 days" in detail

    def test_already_gone_is_success_not_failure(self, monkeypatch):
        """Deleting a project that is already gone (or already
        soft-deleted) has already reached the caller's goal -- reporting
        that as a failure would make cleanup retries permanently red."""
        monkeypatch.setattr(
            pg, "run",
            lambda argv, **kw: (1, "ERROR: (gcloud.projects.delete) NOT_FOUND: "
                                   "Project 'wsmig-src-12345' not found"))
        ok, detail = pg.delete_project("wsmig-src-12345")
        assert ok is True
        assert "already gone" in detail

    def test_real_failure_is_reported_not_swallowed(self, monkeypatch):
        monkeypatch.setattr(
            pg, "run", lambda argv, **kw: (1, "PERMISSION_DENIED: caller "
                                              "lacks resourcemanager.projects.delete"))
        ok, detail = pg.delete_project("wsmig-src-12345")
        assert ok is False
        assert "PERMISSION_DENIED" in detail

    def test_targets_the_right_project_and_disables_confirmation_prompt(self, monkeypatch):
        seen = {}

        def fake_run(argv, **kw):
            seen["argv"] = argv
            return 0, "ok"

        monkeypatch.setattr(pg, "run", fake_run)
        pg.delete_project("wsmig-src-12345")
        assert seen["argv"] == ["gcloud", "projects", "delete", "wsmig-src-12345"]
        # run() itself injects --quiet for every gcloud call -- not
        # re-asserted here, see TestRun's own coverage of that.
