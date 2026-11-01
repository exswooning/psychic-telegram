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
