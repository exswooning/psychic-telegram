"""The DMS import must run parallel to the migration and never take mail's
place in the engine run."""
import webui


def test_dms_action_exists_and_is_parallel():
    spec = webui.ACTIONS["dms_import"]
    assert spec.get("parallel") is True     # exempt from one-heavy-job admission
    assert spec.get("browser") is True      # needs DISPLAY + admin login
    assert spec.get("confirm") == "DMS"


def test_dms_argv_hands_mail_to_google_not_the_engine():
    argv = webui.ACTIONS["dms_import"]["argv"]
    assert "dms_migrate.py" in " ".join(argv)
    assert "--apply" in argv
    # it reads SOURCE_DOMAIN/TARGET_ADMIN/SOURCE_ADMIN from the env, so the
    # argv must not hard-code a tenant
    assert not any("rohitrokaya" in a for a in argv)


def test_parallel_jobs_are_separate_instances():
    a = webui.get_parallel_job(66, "dms_import")
    b = webui.get_job(66)
    assert a is not b                       # must not block the migration Job
    assert webui.get_parallel_job(66, "dms_import") is a   # stable per key


def test_dms_env_defaults_email_to_target_admin(monkeypatch, tmp_path):
    # no creds file -> DWD_EMAIL falls back to the account's target admin,
    # DISPLAY is set for the headless browser
    monkeypatch.setattr(webui, "DWD_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setattr(webui, "_account_env",
                        lambda aid, base=None: {"TARGET_ADMIN": "info@t.example"})
    env = webui._dms_env(66)
    assert env["DWD_EMAIL"] == "info@t.example"
    assert env["DISPLAY"]


def test_dms_metrics_refresh_is_readonly_parallel():
    spec = webui.ACTIONS["dms_metrics_refresh"]
    assert spec.get("parallel") is True and spec.get("browser") is True
    assert "--status" in spec["argv"]      # reads, never touches the import
    assert "confirm" not in spec           # read-only, no gate


def test_metrics_payload_handles_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(webui, "DMS_METRICS_FILE", str(tmp_path / "none.json"))
    p = webui._dms_metrics_payload()
    assert p == {"data": None, "ageSeconds": None}


def test_metrics_payload_reads_and_ages(monkeypatch, tmp_path):
    import json as _j, time as _t
    f = tmp_path / "m.json"
    f.write_text(_j.dumps({"status": "In progress",
                           "metrics": {"Emails imported": 20900},
                           "read_at": int(_t.time()) - 30}))
    monkeypatch.setattr(webui, "DMS_METRICS_FILE", str(f))
    p = webui._dms_metrics_payload()
    assert p["data"]["metrics"]["Emails imported"] == 20900
    assert 25 <= p["ageSeconds"] <= 40


def test_read_metrics_parses_labelled_counters():
    import dms_migrate

    class _Page:
        def inner_text(self, _):
            return ("Import data In progress Stop import Discovered tasks "
                    "272,327 Warning 0 Failed 0 Skipped 0 Successful 272,327 "
                    "Users processed 78 Emails imported 20,900 Emails failed 2")

    r = dms_migrate.read_metrics(_Page())
    assert r["status"] == "In progress"
    assert r["metrics"]["Discovered tasks"] == 272327
    assert r["metrics"]["Emails imported"] == 20900
