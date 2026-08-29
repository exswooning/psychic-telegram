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
