"""ab_transfer.py's arms and api_server.ab_view: what each arm runs, and what the page may claim."""
import json

import ab_transfer as AB
import api_server


def test_dms_arm_leaves_mail_out_of_the_migrate_pass_and_drive_only_is_unchanged():
    assert AB.services_for(None) == "drive"
    assert "gmail" not in AB.services_for("dms").split(",")
    assert "gmail" in AB.services_for("split").split(",") and "gmail" in AB.services_for("engine").split(",")


def test_split_arm_rewrites_links_link_mail_only_and_reads_every_message():
    env = AB.mail_env("split")
    assert env == {"REWRITE_DRIVE_LINKS": "true", "MAIL_ONLY_WITH_LINKS": "true",
                   "MAIL_LINK_SCAN": "exhaustive"}
    assert AB.mail_env("engine") == AB.mail_env("dms") == AB.mail_env(None) == {}


def _write(tmp_path, progress=None, results=None):
    pp, rp = tmp_path / "p.json", tmp_path / "r.json"
    if progress is not None:
        pp.write_text(json.dumps(progress))
    if results is not None:
        rp.write_text(json.dumps(results))
    return str(pp), str(rp)


def test_no_eta_until_one_arm_has_finished_then_a_real_one(tmp_path):
    import os
    base = {"running": True, "pid": os.getpid(), "total": 6, "done": 0, "armStartedAt": 1000.0,
            "current": {"mode": "server_side", "mailMode": "engine", "stage": "migrate"}}
    pp, rp = _write(tmp_path, base)
    v = api_server.ab_view(pp, rp, now=1100.0)
    assert v["running"] and v["progress"]["etaSeconds"] is None
    assert "no estimate" in v["progress"]["etaNote"]

    pp, rp = _write(tmp_path, {**base, "done": 2, "durations": [300, 500]})
    v = api_server.ab_view(pp, rp, now=1100.0)            # mean 400; 100 into the current arm
    assert v["progress"]["etaSeconds"] == (400 - 100) + 400 * 3


def test_a_progress_file_left_by_a_killed_run_does_not_read_as_running(tmp_path):
    pp, rp = _write(tmp_path, {"running": True, "pid": 2 ** 22 - 3, "total": 6, "done": 1,
                               "durations": [10]})
    v = api_server.ab_view(pp, rp, now=0.0)
    assert not v["running"] and v["progress"]["etaSeconds"] is None


def test_the_ranking_puts_clean_arms_first_fastest_first_and_never_hides_a_fast_dirty_one(tmp_path):
    runs = [
        {"mode": "download_upload", "mailMode": "engine", "seconds": 90, "clean": True, "failed": 0},
        {"mode": "server_side", "mailMode": "engine", "seconds": 40, "clean": False, "failed": 3,
         "verdicts": {"audit/gmail": "DIFFERENCES"}},
        {"mode": "server_side", "mailMode": "split", "seconds": 60, "clean": True, "failed": 0},
    ]
    pp, rp = _write(tmp_path, None, {"source": {}, "runs": runs})
    r = api_server.ab_view(pp, rp)["ranking"]
    assert [a["arm"] for a in r] == ["server_side + split", "download_upload + engine",
                                     "server_side + engine"]
    assert r[-1]["clean"] is False and r[-1]["verdicts"] == {"audit/gmail": "DIFFERENCES"}


def test_a_drive_only_arm_is_clean_when_it_lost_nothing(tmp_path):
    runs = [{"mode": "server_side", "seconds": 5, "exit": 0, "failed": 0, "fidelity": {"missing_on_target": 0}},
            {"mode": "download_upload", "seconds": 4, "exit": 0, "failed": 0, "fidelity": {"missing_on_target": 2}}]
    pp, rp = _write(tmp_path, None, {"runs": runs})
    r = api_server.ab_view(pp, rp)["ranking"]
    assert [(a["arm"], a["clean"]) for a in r] == [("server_side", True), ("download_upload", False)]


def test_a_run_that_failed_says_so_with_its_reason_and_is_not_running(tmp_path):
    pp, rp = _write(tmp_path, {"running": False, "total": 4, "done": 0, "durations": [],
                               "error": "RuntimeError: target reset refused: t.example is protected"})
    v = api_server.ab_view(pp, rp)
    assert not v["running"] and "protected" in v["progress"]["error"]


def test_a_refused_reset_keeps_the_refusals_own_words(monkeypatch):
    import subprocess as sp
    monkeypatch.setattr(AB.subprocess, "run", lambda *a, **k: sp.CompletedProcess(
        a, 1, stdout="", stderr="t.example is protected because it is configured on this deployment"))
    rc, why = AB.run_tail(["reset_target.py"], {})
    assert rc == 1 and "protected" in why


def test_a_fresh_target_arm_deletes_nothing_and_points_the_user_at_a_new_account(monkeypatch, tmp_path):
    """reset_target.py cannot empty migrated mail (audit@t. went 758 -> 1,486 across two arms), so a
    comparison arm gets its own empty account instead of a reset."""
    import inspect
    src = inspect.getsource(AB.one_mode)
    fresh_branch = src[src.index("if fresh_target:"):src.index("else:", src.index("if fresh_target:"))]
    assert "run_tail(" not in fresh_branch and "bulk_seed_identities" in fresh_branch
    main_src = inspect.getsource(AB.main)
    assert "fresh = args.fresh_targets or bool(args.mail_mode)" in main_src
    assert "finally:" in main_src and "original_target" in main_src.split("finally:")[1]
