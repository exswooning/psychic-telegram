"""The regression set compares each run with the last and lists only what got worse."""
import ab_transfer
import regression as R


def _rep(verdicts, kinds, failed=0):
    return {"runs": [{"verdicts": {f"u/{k}": v for k, v in verdicts.items()}, "failed": failed, "seconds": 10,
                      "differences": {"u/drive": {"kinds": [{"kind": k, "count": n} for k, n in kinds.items()]}}}]}


def test_a_summary_counts_kinds_and_verdicts():
    s = R.summarise(_rep({"drive": "IDENTICAL", "gmail": "DIFFERENCES"}, {"gmail.extras": 3}))
    assert s["ran"] and s["verdicts"] == {"drive": "IDENTICAL", "gmail": "DIFFERENCES"}
    assert s["kinds"] == {"gmail.extras": 3}
    assert R.summarise({"runs": []}) == {"ran": False}


def test_only_what_got_worse_is_listed():
    before = {"a": R.summarise(_rep({"drive": "IDENTICAL", "gmail": "DIFFERENCES"}, {"gmail.extras": 3}, 1))}
    better = {"a": R.summarise(_rep({"drive": "IDENTICAL", "gmail": "IDENTICAL"}, {}, 0))}
    assert R.compare(better, before) == []
    worse = {"a": R.summarise(_rep({"drive": "DIFFERENCES", "gmail": "DIFFERENCES"},
                                   {"gmail.extras": 5, "drive.comments": 1}, 2))}
    got = R.compare(worse, before)
    assert "a/drive: IDENTICAL -> DIFFERENCES" in got and "a: gmail.extras 3 -> 5" in got
    assert "a: drive.comments 0 -> 1" in got and "a: failed items 1 -> 2" in got
    assert R.compare({"a": {"ran": False}}, before) == ["a: did not run"]
    assert R.compare(worse, None) == []      # first run: nothing to compare


def test_an_earlier_test_account_is_not_restored_as_the_users_own():
    assert ab_transfer._is_test_account("finance-ab1t-11010140@t.example")
    assert ab_transfer._is_test_account("it.department-ab1-10311353@t.example")
    assert not ab_transfer._is_test_account("finance@t.example")
    assert not ab_transfer._is_test_account(None)


def test_a_web_ui_launch_names_the_account_through_its_ledger(monkeypatch):
    monkeypatch.setenv("MIGRATION_DB", "/opt/bitport/data/accounts/3/migration.db")
    assert R._account_from_ledger() == 3
    monkeypatch.setenv("MIGRATION_DB", "/opt/bitport/migration.db")
    assert R._account_from_ledger() is None


def test_it_is_a_runnable_action():
    import webui
    assert webui.ACTIONS["regression"]["argv"][1] == "regression.py"
    assert not webui.ACTIONS["regression"].get("destructive")


def test_progress_is_counted_in_real_steps_from_each_users_own_run():
    """The card showed nothing and no bar for a whole run: regression.py printed nothing until the
    end. Each user is three steps (migrate, Google's import, 1:1 check) read from its own file."""
    states = {
        "a@s.example": {"running": True, "current": {"stage": "migrate"}},
        "b@s.example": {"running": True, "current": {"stage": "dms import"}},
        "c@s.example": {"running": True, "current": {"stage": "verify 1:1"}},
        "d@s.example": {"running": False, "finishedAt": "2026-11-01T18:00:00Z"},
    }
    line = R.progress_line(states)
    assert line.startswith("[6/12] steps")          # 0 + 1 + 2 + 3
    assert "a: migrate" in line and "d: finished" in line
    assert R.user_steps(None) == (0, "waiting to start")


def test_the_counter_is_the_shape_webui_reads():
    import webui
    m = webui._COUNTER_RE.search(R.progress_line({"a@s.example": None, "b@s.example": None}))
    assert m and (m.group(1), m.group(2)) == ("0", "6")
