"""ab_transfer.py's arms and api_server.ab_view: what each arm runs, and what the page may claim."""
import json

import pytest

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
        {"mode": "download_upload", "mailMode": "engine", "seconds": 90, "clean": True, "failed": 0,
         "verdicts": {"audit/gmail": "IDENTICAL"}},
        {"mode": "server_side", "mailMode": "engine", "seconds": 40, "clean": False, "failed": 3,
         "verdicts": {"audit/gmail": "DIFFERENCES"}},
        {"mode": "server_side", "mailMode": "split", "seconds": 60, "clean": True, "failed": 0,
         "verdicts": {"audit/gmail": "IDENTICAL"}, "dms": "done"},
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
    assert "finally:" in main_src and "original_target" in main_src.split("finally:")[-1]


def test_differences_survive_as_counts_and_a_few_examples():
    payload = json.dumps({"counts": {"missing": 756}, "completion": "COMPLETE",
                          "missing": [{"source": str(i)} for i in range(9)],
                          "checks": [{"key": "mail.items", "status": "DIFFERENCES"},
                                     {"key": "mail.labels", "status": "IDENTICAL"}]})
    d = AB.difference_summary(payload)
    assert d["counts"] == {"missing": 756} and len(d["missing"]) == 5
    assert d["failedChecks"] == ["mail.items"]
    assert "unreadable" in AB.difference_summary("not json")


def test_a_dms_arm_without_the_console_login_is_skipped_not_failed(monkeypatch):
    import types
    import webui
    monkeypatch.setattr(webui, "_dms_env", lambda account_id: {})
    st = types.SimpleNamespace(account_id=3, target_admin="admin@t.example")
    out, secs = AB.dms_tail(st, ["a@s.example"], ["a-ab1@t.example"], {})
    assert out.startswith("skipped:") and secs == 0.0


def test_the_dms_tail_maps_the_one_user_and_runs_until_done(monkeypatch, tmp_path):
    import csv
    import types
    import webui
    monkeypatch.setattr(webui, "_dms_env", lambda account_id: {"DWD_PASSWORD": "x"})
    monkeypatch.chdir(tmp_path)
    seen = {}
    monkeypatch.setattr(AB, "run", lambda cmd, env: (seen.update(cmd=cmd) or (0, 12.5)))
    st = types.SimpleNamespace(account_id=3, target_admin="admin@t.example", source_domain="s.example")
    out, secs = AB.dms_tail(st, ["a@s.example"], ["a-ab1@t.example"], {})
    assert (out, secs) == ("done", 12.5) and "--until-done" in seen["cmd"] and "--apply" in seen["cmd"]
    rows = list(csv.reader(open(seen["cmd"][seen["cmd"].index("--identities") + 1])))
    assert rows[1] == ["a@s.example", "a-ab1@t.example"]


def test_the_ranking_carries_each_arms_differences_and_mail_import_status(tmp_path):
    runs = [{"mode": "server_side", "mailMode": "engine", "seconds": 90, "clean": False, "failed": 0,
             "verdicts": {"audit/gmail": "DIFFERENCES"},
             "differences": {"audit/gmail": {"counts": {"missing": 756}}}, "dms": None},
            {"mode": "server_side", "mailMode": "split", "seconds": 60, "clean": False, "failed": 5,
             "verdicts": {}, "dms": "skipped: no Admin console login"}]
    pp, rp = _write(tmp_path, None, {"runs": runs})
    r = {a["arm"]: a for a in api_server.ab_view(pp, rp)["ranking"]}
    assert r["server_side + engine"]["differences"] == {"audit/gmail": {"counts": {"missing": 756}}}
    assert r["server_side + split"]["dms"] == "skipped: no Admin console login"


def test_verify_takes_all_and_refuses_a_service_it_does_not_know(monkeypatch):
    """`--services all` was dropped as an unknown name: the A/B's audit checked nothing in any arm."""
    import types

    import pytest

    import config
    import db as dbmod
    import verify_sample
    seen = {}
    monkeypatch.setattr(config, "Settings", lambda account_id=None: types.SimpleNamespace(
        db_path=":memory:", migrate_contacts=False, migrate_tasks=False, max_retries=1))
    monkeypatch.setattr(dbmod, "MigrationDB", lambda path: types.SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(verify_sample, "run_and_save", lambda auth, db, st, users, services, **k: seen.update(s=services))
    import auth
    monkeypatch.setattr(auth, "AuthManager", lambda st: None)
    assert verify_sample.main(["--services", "all"]) == 0 and seen["s"] == verify_sample.ALL_SERVICES
    with pytest.raises(SystemExit) as exc:
        verify_sample.main(["--services", "everything"])
    assert exc.value.code == 2


def test_the_drive_comparison_matches_a_fresh_target_account(monkeypatch):
    """Keyed by address, audit/… never met audit-ab1-…/…: 0 compared, 102 'missing', every arm."""
    import types
    files = [{"id": "1", "name": "Plan", "mimeType": "x", "md5Checksum": "a", "modifiedTime": "2024"}]
    svc = types.SimpleNamespace(files=lambda: types.SimpleNamespace(
        list=lambda **k: types.SimpleNamespace(execute=lambda: {"files": files})))
    auth = types.SimpleNamespace(source_drive=lambda u: svc, target_drive=lambda u: svc)
    src = AB.snapshot_drive(auth, ["audit@s.example"], "source")
    tgt = AB.snapshot_drive(auth, ["audit-ab1-10301748@t.example"], "target")
    assert AB.fidelity(src, tgt)["compared"] == 1


def _arms(tmp_path, runs):
    rp = tmp_path / "r.json"
    rp.write_text(json.dumps({"runs": runs}))
    return str(rp)


ARMS = [
    {"mode": "server_side", "mailMode": "engine", "seconds": 1600, "exit": 0, "failed": 0, "dms": None,
     "verdicts": {"a/drive": "IDENTICAL", "a/chat": "DIFFERENCES"},
     "differences": {"a/chat": {"counts": {"differences": 4, "missing": 0}}}},
    {"mode": "download_upload", "mailMode": "engine", "seconds": 900, "exit": 0, "failed": 0, "dms": None,
     "verdicts": {"a/drive": "DIFFERENCES", "a/chat": "DIFFERENCES"},
     "differences": {"a/drive": {"counts": {"differences": 16, "duplicates": 5}},
                     "a/chat": {"counts": {"differences": 4}}}},
    {"mode": "server_side", "mailMode": "split", "seconds": 300, "exit": 0, "failed": 0,
     "dms": "skipped: no Admin console login", "verdicts": {"a/drive": "IDENTICAL"}},
    {"mode": "download_upload", "mailMode": "split", "seconds": 200, "exit": 0, "failed": 0, "dms": None,
     "verdicts": {"verify": "UNVERIFIED"}},
]


def test_arms_rank_by_errors_then_speed_and_an_unchecked_arm_never_wins(tmp_path):
    """A fast arm wins only on what its own check saw: one whose check never ran, or whose mail
    never reached Google's import, ranks after every arm that was looked at."""
    v = api_server.ab_view(str(tmp_path / "none"), _arms(tmp_path, ARMS))
    r = v["ranking"]
    assert [(a["arm"], a["errors"]) for a in r[:2]] == [("server_side + engine", 4),
                                                         ("download_upload + engine", 25)]
    assert not r[2]["judged"] and not r[3]["judged"] and r[2]["why"] and r[3]["why"]
    assert v["best"] == {"arm": "server_side + engine", "mode": "server_side", "mailMode": "engine",
                         "errors": 4, "seconds": 1600, "errorsPer1k": None, "secondsPer100": None}


def test_new_runs_take_the_winner_and_keep_the_old_defaults_while_none_qualifies(tmp_path, monkeypatch):
    import types
    body = types.SimpleNamespace(services=["all"], sample=None)
    monkeypatch.setattr(api_server, "AB_RESULTS", _arms(tmp_path, ARMS))
    monkeypatch.setattr(api_server.ab_default, "__defaults__", (api_server.AB_RESULTS,))
    assert api_server._default_mail_mode(body) == "engine"
    monkeypatch.setattr(api_server.ab_default, "__defaults__", (_arms(tmp_path, ARMS[2:]),))
    assert api_server.ab_default() is None and api_server._default_mail_mode(body) == "split"
    assert api_server._default_mail_mode(types.SimpleNamespace(services=["all"], sample=25)) == "engine"


def test_a_process_can_be_pointed_at_its_own_copy_of_the_ledger(monkeypatch, tmp_path):
    import sqlite3

    import config
    src = tmp_path / "real.db"
    c = sqlite3.connect(src)
    c.execute("create table t (x)")
    c.execute("insert into t values (1)")
    c.commit()
    c.close()
    path = AB.ledger_copy(config.Settings(db_path=str(src)), str(tmp_path / "arm1"))
    assert sqlite3.connect(path).execute("select x from t").fetchall() == [(1,)] and path != str(src)
    monkeypatch.setenv("BITPORT_LEDGER", path)
    assert config.Settings().db_path == path


def _main_with(monkeypatch, tmp_path, argv, results, users=("a@s.x",), files=None):
    """AB.main with the Google-facing pieces faked; `results` maps arm label -> list of run dicts."""
    import types
    ledgers, calls, users_seen = [], [], []
    _main_with.users_seen = users_seen
    st = types.SimpleNamespace(db_path=str(tmp_path / "real.db"), scratch_dir="s", target_domain="t.x",
                               target_admin="a@t.x", target_sa_key="k", account_id=3)
    import sqlite3
    sqlite3.connect(st.db_path).close()
    monkeypatch.setattr(AB, "Settings", lambda account_id=None: st)
    monkeypatch.setattr(AB, "AuthManager", lambda s: object())
    monkeypatch.setattr(AB, "snapshot_drive", lambda auth, us, side: {
        "files": (files or {}).get(us[0], 1), "folders": 0, "bytes": 1, "native": 0, "binary": 1,
        "mimes": {}, "sample": {}})
    monkeypatch.setattr(AB, "mailbox_size", lambda auth, u: 9)
    db = types.SimpleNamespace(all_identities=lambda: [{"entity_type": "user", "source_email": "a@s.x", "target_email": "a@t.x"}])
    monkeypatch.setattr(AB, "MigrationDB", lambda p: db)
    monkeypatch.setattr(AB, "ledger_copy", lambda s, d: ledgers.append(d) or d + "/migration.db")
    monkeypatch.setattr(AB, "PROGRESS", str(tmp_path / "p.json"))
    monkeypatch.setattr("db.bulk_seed_identities", lambda *a: calls.append("restore"))

    def one_mode(mode, settings, auth, us, ut, snap, mail_mode, fresh_target, ledger=None, side_by_side=False):
        r = dict(results[AB.arm_label(mode, mail_mode)].pop(0))
        calls.append((AB.arm_label(mode, mail_mode), ledger, side_by_side))
        users_seen.append((us[0], fresh_target))
        return {"mode": mode, "mailMode": mail_mode, "exit": 0, "failed": 0, "dms": None, "timing": "side by side" if side_by_side else "alone",
                "verdicts": {"a/drive": "IDENTICAL"}, "differences": {}, "migrated": {}, "host_bytes_in": None,
                "target": {"files": 1, "native": 0}, "fidelity": {"compared": 1, "missing_on_target": 0, "mime_changed": 0,
                                                                  "md5_changed": 0, "mtime_changed": 0, "examples": []}, **r}
    monkeypatch.setattr(AB, "one_mode", one_mode)
    assert AB.main([*argv, "--report", str(tmp_path / "r.md"), *[a for u in users for a in ("--user", u)],
                    "--account-id", "3"]) == 0
    return json.load(open(tmp_path / "r.md.json")), calls, ledgers


def test_arms_run_side_by_side_on_their_own_ledgers_then_the_leader_is_re_timed_alone(monkeypatch, tmp_path):
    runs = {"server_side + engine": [{"seconds": 90}, {"seconds": 40}],
            "server_side + split": [{"seconds": 60, "failed": 3}],
            "download_upload + split": [{"seconds": 70, "failed": 3}]}
    out, calls, ledgers = _main_with(monkeypatch, tmp_path, ["--mode", "server_side", "--mode", "download_upload",
                                                             "--mail-mode", "engine", "--mail-mode", "split"], runs)
    first = [c for c in calls if c != "restore"]
    assert {c[0] for c in first[:3]} == {"server_side + engine", "server_side + split", "download_upload + split"}
    assert all(c[1] and c[2] for c in first[:3]) and len(set(c[1] for c in first[:3])) == 3
    assert first[3][0] == "server_side + engine" and first[3][2] is False       # the 0-error leader, alone
    assert "restore" not in calls                                               # the real ledger was never touched
    lead = next(r for r in out["runs"] if r["mailMode"] == "engine")
    assert lead["seconds"] == 40 and lead["contendedSeconds"] == 90 and lead["timing"] == "alone"
    assert out["skipped"] == ["download_upload + engine"] and out["sideBySide"] is True


def test_sequential_is_the_old_way_and_include_slow_brings_the_slow_arm_back(monkeypatch, tmp_path):
    runs = {k: [{"seconds": 5}] for k in ("server_side + engine", "download_upload + engine")}
    out, calls, ledgers = _main_with(monkeypatch, tmp_path, ["--mode", "server_side", "--mode", "download_upload",
                                                             "--mail-mode", "engine", "--sequential", "--include-slow"], runs)
    assert ledgers == [] and [c[2] for c in calls if c != "restore"] == [False, False]
    assert out["skipped"] == [] and calls[-1] == "restore" and len(out["runs"]) == 2


def test_differences_are_grouped_by_known_kind_with_cause_and_fix():
    """Four Chat findings read as two platform limits and one bot attribution, not as '4 differences'."""
    payload = json.dumps({"counts": {"differences": 4}, "differences": [
        {"diffs": ["posted by the user with an attribution: the sender cannot post there"]},
        {"diffs": ["Original Chat sender identity was not reproduced"], "scope": "chat-thread"},
        {"diffs": ["Chat conversation type differs: DIRECT_MESSAGE -> GROUP_CHAT; x"], "scope": "chat-space-type"},
        {"diffs": ["Chat participants differ: 1 missing (1 app/bot), 0 extra; 0 existing"], "scope": "chat-members"}],
        "missing": [{"why": "not on the target"}] * 3, "checks": []})
    kinds = {k["kind"]: k for k in AB.difference_summary(payload, service="chat")["kinds"]}
    assert set(kinds) == {"chat.attributed", "chat.sender", "chat.space_type", "chat.members", "chat.missing"}
    assert kinds["chat.missing"]["count"] == 3 and kinds["chat.space_type"]["auto"] is False
    assert kinds["chat.space_type"]["cause"] and kinds["chat.space_type"]["fix"]


def test_chat_is_left_out_of_the_arms_and_their_check_until_it_is_put_back():
    assert "chat" not in AB.ALL_SERVICES.split(",") and "chat" not in AB.VERIFY_SERVICES.split(",")
    assert "chat" not in AB.services_for("engine").split(",") and "chat" not in AB.services_for("dms").split(",")
    import verify_sample
    assert set(AB.VERIFY_SERVICES.split(",")) <= set(verify_sample.ALL_SERVICES)


def test_one_user_per_arm_weighted_per_item_so_a_bigger_mailbox_is_not_punished(monkeypatch, tmp_path):
    """Four arms on one mailbox ran Gmail's per-user limit dry (729 unverifiable). One user per arm
    avoids that; users differ in size, so errors and time are weighed per 1,000 / 100 items."""
    files = {"a@s.x": 10, "b@s.x": 1000, "c@s.x": 10}
    runs = {"server_side + engine": [{"seconds": 100, "failed": 2}],
            "server_side + split": [{"seconds": 100, "failed": 2}, {"seconds": 90, "failed": 2}],
            "download_upload + split": [{"seconds": 10, "failed": 3}]}

    out, calls, _ = _main_with(monkeypatch, tmp_path, [
        "--mode", "server_side", "--mode", "download_upload", "--mail-mode", "engine", "--mail-mode", "split"],
        runs, users=("a@s.x", "b@s.x", "c@s.x"), files=files)
    seen = _main_with.users_seen
    # each arm on its own user, each with a fresh target named after that user
    assert {(u, t.split("-ab")[0]) for u, t in seen[:3]} == {("a@s.x", "a"), ("b@s.x", "b"), ("c@s.x", "c")}
    by = {f"{r['mode']} + {r['mailMode']}": r for r in out["runs"]}
    assert by["server_side + split"]["corpus"] == {"user": "b@s.x", "files": 1000, "bytes": 1, "messages": 9, "items": 1009}
    # 2 errors in 1,009 items beats 2 in 19: the big mailbox wins on errors, so it is re-timed alone
    assert AB.arm_errors(by["server_side + split"])["errorsPer1k"] == 1.98
    assert seen[3][0] == "b@s.x" and seen[3][1].split("-ab")[1].startswith("2t")


def test_an_arms_ledger_copy_is_never_taken_for_the_control_plane(monkeypatch, tmp_path):
    """Live: BITPORT_LEDGER pointed the control-plane lookup at a copy with no tenant_configs, so
    every arm's migrate died at startup and the A/B ran Google's import against empty targets."""
    import os

    import control_plane_db
    monkeypatch.delenv("CONTROL_PLANE_DB", raising=False)
    monkeypatch.setenv("BITPORT_LEDGER", str(tmp_path / "data" / "ab" / "x" / "arm1" / "migration.db"))
    assert control_plane_db._db_path() == os.path.join(os.path.dirname(os.path.abspath(control_plane_db.__file__)), "migration.db")


def test_an_arm_whose_migration_died_says_why_and_skips_googles_import(monkeypatch):
    import types
    ran = []
    monkeypatch.setattr(AB, "dms_tail", lambda *a: ran.append("dms") or ("done", 1.0))
    monkeypatch.setattr(AB, "snapshot_drive", lambda *a: {"files": 0, "folders": 0, "bytes": 0, "native": 0, "binary": 0,
                                                          "mimes": {}, "sample": {}})
    monkeypatch.setattr(AB, "run_tail", lambda cmd, env: (2, "sqlite3.OperationalError: no such table: tenant_configs")
                        if "migrate" in cmd else ran.append("verify") or (0, ""))
    class Rows(list):
        def fetchone(self):
            return (0,)
    db = types.SimpleNamespace(conn=types.SimpleNamespace(execute=lambda *a, **k: Rows()),
                               _mapping_cache={}, _mapping_cached_users=set())
    monkeypatch.setattr(AB, "MigrationDB", lambda p: db)
    st = types.SimpleNamespace(db_path="x", target_domain="t.x", scratch_dir="s", target_admin="a@t.x")
    import contextlib
    db.write = contextlib.contextmanager(lambda: (yield types.SimpleNamespace(execute=lambda *a, **k: None)))
    monkeypatch.setattr("db.bulk_seed_identities", lambda *a: None)
    r = AB.one_mode("server_side", st, None, ["a@s.x"], ["a@t.x"], {"sample": {}}, "split", "a-ab1@t.x")
    assert r["exit"] == 2 and "tenant_configs" in r["migrateOutput"] and "dms" not in ran and "verify" not in ran
    assert r["verdicts"] == {"verify": "UNVERIFIED"}
    assert AB.arm_errors(r)["judged"] is False and "exited 2" in AB.arm_errors(r)["why"]


@pytest.mark.parametrize("given, acceptance, check", [({}, "false", "full"),
                                                      ({"FIRST_PASS_ACCEPTANCE": "true",
                                                        "MAIL_COMPLETION_CHECK": "counts"}, "true", "counts")])
def test_an_arm_skips_the_duplicate_acceptance_check_and_keeps_receipts_unless_told_otherwise(
        monkeypatch, given, acceptance, check):
    import types
    seen = {}
    for k in ("FIRST_PASS_ACCEPTANCE", "MAIL_COMPLETION_CHECK"):
        monkeypatch.delenv(k, raising=False)
    for k, v in given.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(AB, "run_tail", lambda cmd, env: seen.update(env) or (2, "stop here"))
    monkeypatch.setattr(AB, "snapshot_drive", lambda *a: {"files": 0, "folders": 0, "bytes": 0, "native": 0,
                                                          "binary": 0, "mimes": {}, "sample": {}})

    class Rows(list):
        def fetchone(self):
            return (None, None)
    import contextlib
    db = types.SimpleNamespace(conn=types.SimpleNamespace(execute=lambda *a, **k: Rows()),
                               _mapping_cache={}, _mapping_cached_users=set())
    db.write = contextlib.contextmanager(lambda: (yield types.SimpleNamespace(execute=lambda *a, **k: None)))
    monkeypatch.setattr(AB, "MigrationDB", lambda p: db)
    monkeypatch.setattr("db.bulk_seed_identities", lambda *a: None)
    st = types.SimpleNamespace(db_path="x", target_domain="t.x", scratch_dir="s", target_admin="a@t.x")
    AB.one_mode("server_side", st, None, ["a@s.x"], ["a@t.x"], {"sample": {}}, "engine", "a-ab1@t.x")
    assert (seen["FIRST_PASS_ACCEPTANCE"], seen["MAIL_COMPLETION_CHECK"]) == (acceptance, check)


def test_an_arm_registers_as_its_own_job_so_it_is_never_judged_as_a_migration():
    import inspect

    import main
    import run_watch
    src = inspect.getsource(AB.one_mode)
    assert 'env["BITPORT_JOB_NAME"] = "ab-migrate"' in src
    assert 'os.getenv("BITPORT_JOB_NAME")' in inspect.getsource(main.cmd_migrate) or \
           'os.getenv("BITPORT_JOB_NAME")' in inspect.getsource(main)
    assert "ab-migrate" not in run_watch.MIGRATION_JOBS


def test_a_user_never_mapped_is_left_unmapped_and_its_test_accounts_are_named_after_it(monkeypatch, tmp_path):
    """Live: esc.mrc stayed mapped to R1's test account; R2 restored that and named its own
    account after it (esc.mrc-ab1-10311709-ab1-10311833)."""
    import contextlib
    import sqlite3
    import types
    st = types.SimpleNamespace(db_path=str(tmp_path / "real.db"), scratch_dir="s", target_domain="t.x",
                               target_admin="a@t.x", target_sa_key="k", account_id=3)
    sqlite3.connect(st.db_path).close()
    executed, seen = [], []
    db = types.SimpleNamespace(all_identities=lambda: [], _identity_cache=None)
    db.write = contextlib.contextmanager(lambda: (yield types.SimpleNamespace(execute=lambda *a: executed.append(a))))
    monkeypatch.setattr(AB, "Settings", lambda account_id=None: st)
    monkeypatch.setattr(AB, "AuthManager", lambda s: object())
    monkeypatch.setattr(AB, "MigrationDB", lambda p: db)
    monkeypatch.setattr(AB, "mailbox_size", lambda auth, u: 1)
    monkeypatch.setattr(AB, "PROGRESS", str(tmp_path / "p.json"))
    monkeypatch.setattr(AB, "snapshot_drive", lambda *a: {"files": 1, "folders": 0, "bytes": 1, "native": 0,
                                                          "binary": 1, "mimes": {}, "sample": {}})

    def one_mode(mode, settings, auth, us, ut, snap, mail_mode, fresh_target, ledger=None, side_by_side=False):
        seen.append(fresh_target)
        return {"mode": mode, "mailMode": mail_mode, "exit": 0, "failed": 0, "seconds": 1, "verdicts": {},
                "differences": {}, "migrated": {}, "host_bytes_in": 0, "target": {"files": 1, "native": 0},
                "fidelity": {"compared": 1, "missing_on_target": 0, "mime_changed": 0, "md5_changed": 0,
                             "mtime_changed": 0, "examples": []}}
    monkeypatch.setattr(AB, "one_mode", one_mode)
    assert AB.main(["--report", str(tmp_path / "r.md"), "--user", "esc.mrc@s.x", "--account-id", "3",
                    "--sequential", "--mode", "server_side", "--mail-mode", "engine"]) == 0
    assert seen[0].startswith("esc.mrc-ab1-") and seen[0].endswith("@t.x")
    assert any("DELETE FROM identity_map" in a[0] and a[1] == ("esc.mrc@s.x",) for a in executed)
