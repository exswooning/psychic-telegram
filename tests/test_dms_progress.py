"""A bar per user while the DMS imports: each mailbox's target count against its source count."""
import dms_progress as D


class _Gmail:
    def __init__(self, n, fail=False):
        self.n, self.fail = n, fail

    def users(self):
        return self

    def getProfile(self, userId):
        outer = self

        class _R:
            def execute(self):
                if outer.fail:
                    raise RuntimeError("401")
                return {"messagesTotal": outer.n}
        return _R()


class _Auth:
    def __init__(self, src, tgt):
        self.src, self.tgt = src, tgt

    def source_gmail(self, u):
        return self.src[u]

    def target_gmail(self, u):
        return self.tgt[u]


def test_each_user_gets_its_share_and_an_unreadable_mailbox_is_unknown_not_zero():
    auth = _Auth({"a@s": _Gmail(100), "b@s": _Gmail(40), "c@s": _Gmail(0), "d@s": _Gmail(5)},
                 {"a@t": _Gmail(25), "b@t": _Gmail(44), "c@t": _Gmail(2), "d@t": _Gmail(0, fail=True)})
    rows = {r["source"]: r for r in D.count(auth, [("a@s", "a@t", "AWAITING_DMS"), ("b@s", "b@t", "DONE"),
                                                   ("c@s", "c@t", "DONE"), ("d@s", "d@t", "AWAITING_DMS")])}
    assert rows["a@s"]["share"] == 0.25 and rows["a@s"]["status"] == "AWAITING_DMS"
    assert rows["b@s"]["share"] == 1.0              # welcome mail can put the target over: capped at 100%
    assert rows["c@s"]["share"] == 1.0              # an empty source mailbox is complete
    assert rows["d@s"]["targetCount"] is None and rows["d@s"]["share"] is None


def test_counts_are_cached_for_a_minute_so_polling_does_not_hammer_google(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "HERE", str(tmp_path))
    calls = []
    auth = _Auth({"a@s": _Gmail(10)}, {"a@t": _Gmail(5)})
    pairs = lambda: calls.append(1) or [("a@s", "a@t", "AWAITING_DMS")]        # noqa: E731
    first = D.view(5, lambda: auth, pairs, now=lambda: 1000.0)
    again = D.view(5, lambda: auth, pairs, now=lambda: 1030.0)
    later = D.view(5, lambda: auth, pairs, now=lambda: 1061.0)
    assert first["users"][0]["share"] == 0.5 and again == first and len(calls) == 2 and later["at"] == 1061.0
