"""One Google Docs 503 held the Drive verification of info@s. at item 326 of 1,403 (27 min). A
document the verifier could not read is UNVERIFIED -- tracked, retried, never identical, never a
difference, never skipped -- and the rest of the queue goes on."""
import httplib2
import pytest
from googleapiclient.errors import HttpError

import drive_engine
import native_fidelity
import resilience
import verify_resilience as vr
import verify_sample as V
from tests.conftest import SRC_USER, TGT_USER
from tests.fakes import http_error


def _err(status, retry_after=None, reason=""):
    h = {"status": status}
    if retry_after is not None:
        h["retry-after"] = str(retry_after)
    return HttpError(httplib2.Response(h), ('{"error":{"code":%d,"message":"x","errors":[{"reason":"%s"}]}}'
                                            % (status, reason)).encode())


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    vr.BREAKERS.clear()
    monkeypatch.setenv("VERIFY_RETRY_WAIT_SEC", "0")
    monkeypatch.setattr(resilience, "_wait", lambda d: None)
    monkeypatch.setattr(resilience.time, "sleep", lambda d: None)
    yield
    vr.BREAKERS.clear()


# -- retry policy ----------------------------------------------------------------------------------
class TestRetryPolicy:
    def _call(self, answers, **kw):
        seen = []

        @resilience.retry_on_google_error(max_retries=3, base_delay=0, max_delay=0, **kw)
        def go():
            seen.append(1)
            a = answers[min(len(seen) - 1, len(answers) - 1)]
            if isinstance(a, Exception):
                raise a
            return a
        return go, seen

    def test_one_503_then_success(self):
        go, seen = self._call([_err(503), "doc"])
        assert go() == "doc" and len(seen) == 2

    def test_repeated_503_ends_as_a_typed_transient_failure(self):
        go, seen = self._call([_err(503)])
        with pytest.raises(resilience.RetryBudgetExceeded) as e:
            go()
        assert e.value.status == 503 and e.value.attempts == 4 and len(seen) == 4
        assert vr.classify(e.value).state == vr.UNVERIFIED_TRANSIENT

    def test_a_retry_after_inside_the_budget_is_honoured(self):
        waited = []
        resilience._wait = waited.append
        go, _ = self._call([_err(429, retry_after=5, reason="rateLimitExceeded"), "ok"], max_elapsed=60)
        assert go() == "ok" and waited and 5 <= waited[0] < 7

    def test_a_retry_after_past_the_budget_is_not_waited_out(self):
        waited = []
        resilience._wait = waited.append
        go, seen = self._call([_err(503, retry_after=1650)], max_elapsed=180)
        with pytest.raises(resilience.RetryBudgetExceeded) as e:
            go()
        assert waited == [] and len(seen) == 1         # no 27-minute sleep; the item is handed back
        assert vr.classify(e.value).transient

    @pytest.mark.parametrize("status", [403, 404])
    def test_a_permanent_4xx_is_not_retried(self, status):
        go, seen = self._call([_err(status, reason="forbidden")])
        with pytest.raises(resilience.PermanentAPIError) as e:
            go()
        assert len(seen) == 1 and vr.classify(e.value).state == vr.UNVERIFIED_PERMANENT

    def test_without_a_budget_other_callers_are_unchanged(self):
        go, seen = self._call([_err(503, retry_after=2), "ok"])
        assert go() == "ok"

    def test_a_404_on_a_tagged_side_is_missing_not_unreadable(self):
        e = _err(404); e.bitport_side = "target"
        assert vr.classify(e).state == vr.MISSING_TARGET
        e = _err(404); e.bitport_side = "source"
        assert vr.classify(e).state == vr.MISSING_SOURCE


# -- circuit breaker -------------------------------------------------------------------------------
class TestBreaker:
    def make(self, t):
        return vr.CircuitBreaker("docs", threshold=2, window=100, cooldown=10, clock=lambda: t[0])

    def test_opens_pauses_probes_once_and_closes(self):
        t = [0.0]; b = self.make(t)
        b.failure(); b.before(); b.failure()
        with pytest.raises(vr.CircuitOpen):
            b.before()
        t[0] = 11
        b.before()                                  # the one probe
        with pytest.raises(vr.CircuitOpen):
            b.before()                              # nobody else until it answers
        b.success()
        b.before() and None
        assert b.state == "closed"

    def test_a_failed_probe_reopens_with_a_longer_cooldown(self):
        t = [0.0]; b = self.make(t)
        b.failure(); b.failure(); t[0] = 11; b.before(); b.failure()
        t[0] = 25
        with pytest.raises(vr.CircuitOpen):         # 20s now, not 10
            b.before()
        t[0] = 32
        b.before()

    def test_an_open_breaker_sends_nothing_and_never_returns_a_result(self):
        sent = []
        b = vr.breaker("docs"); b.state, b._opened_at, b._cooldown = "open", __import__("time").monotonic(), 999
        with pytest.raises(vr.CircuitOpen) as e:
            vr.guarded("docs", lambda: sent.append(1) or "looks fine")
        assert not sent and vr.classify(e.value).transient

    def test_a_404_does_not_trip_it(self):
        for _ in range(5):
            with pytest.raises(HttpError):
                vr.guarded("docs", lambda: (_ for _ in ()).throw(_err(404)))
        assert vr.breaker("docs").state == "closed"


# -- the Drive queue -------------------------------------------------------------------------------
@pytest.fixture
def docs(auth, db, settings, identity, quota):
    """Five native Docs migrated; returns their (source, target) ids."""
    settings.rewrite_drive_links = False
    sd = auth.source_drive(SRC_USER)
    folder = sd.add_folder("Projects")
    sd.add_binary("report.pdf", parent=folder, data=b"the quarterly report " * 50)
    for n in range(5):
        sd.add_native(f"Doc {n}", parent=folder, kind="document", export_bytes=b"text %d" % n)
    drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota).run()
    pairs = [(r["source_id"], r["target_id"]) for r in db.conn.execute(
        "SELECT source_id, target_id FROM id_mapping WHERE source_user=? AND type='file'", (SRC_USER,))]
    pairs = [(a, b) for a, b in pairs if db.conn.execute(
        "SELECT 1 FROM id_mapping WHERE source_id=? AND source_name LIKE 'Doc %'", (a,)).fetchone()]
    assert len(pairs) == 5
    return type("D", (), {"auth": auth, "db": db, "settings": settings, "pairs": pairs})


def _verify(d, retry=lambda f: f):
    return V.run(d.auth, d.db, d.settings, [SRC_USER], ("drive",), retry=retry, progress=lambda *_: None)


def _fake(monkeypatch, fail_ids, error, then_ok_after=None, differ=()):
    calls = {}

    def fake(auth, su, tu, sid, tid, mime, lookup, fetch=None, pace=lambda: None):
        calls[sid] = calls.get(sid, 0) + 1
        if sid in fail_ids and (then_ok_after is None or calls[sid] <= then_ok_after):
            raise error() if callable(error) else error
        return (["native document content, formatting or structure differs"] if sid in differ else []), "scope"
    monkeypatch.setattr(native_fidelity, "compare_native", fake)
    return calls


def _drive(r):
    return r["users"][SRC_USER]["drive"]


class TestOneFailingDocumentAmongMany:
    def test_the_queue_goes_on_and_the_failed_one_is_named_not_passed(self, docs, monkeypatch):
        bad = docs.pairs[2][0]
        calls = _fake(monkeypatch, {bad}, lambda: _err(503))
        r = _verify(docs)
        d = _drive(r)
        assert set(calls) >= {s for s, _ in docs.pairs}                 # every document was tried
        assert d["identical"] == d["checked"] - 1 and len(d["unverified"]) == 1
        u = d["unverified"][0]
        assert u["source"] == bad and u["state"] == vr.UNVERIFIED_TRANSIENT and u["status"] == 503
        assert r["verdict"] == "INCOMPLETE"                             # not IDENTICAL, not DIFFERENCES
        assert not d["differences"] and any("not verified" in x for x in r["reasons"])
        assert not V.service_complete(d)

    def test_it_is_tracked_in_the_ledger_and_survives_a_restart(self, docs, monkeypatch):
        bad = docs.pairs[0][0]
        _fake(monkeypatch, {bad}, lambda: _err(503))
        _verify(docs)
        from db import MigrationDB
        fresh = MigrationDB(docs.settings.db_path)                      # a new process, same ledger
        rows = vr.UnverifiedStore(fresh).pending(SRC_USER, "drive")
        assert [x["source_id"] for x in rows] == [bad] and rows[0]["state"] == vr.UNVERIFIED_TRANSIENT
        assert rows[0]["http_status"] == 503 and rows[0]["attempts"] >= 1 and rows[0]["next_retry_at"]
        fresh.close()

    def test_the_retry_pass_verifies_it_once_google_recovers(self, docs, monkeypatch):
        bad = docs.pairs[3][0]
        calls = _fake(monkeypatch, {bad}, lambda: _err(503), then_ok_after=1)
        r = _verify(docs)
        d = _drive(r)
        assert r["verdict"] == "IDENTICAL", r["reasons"]
        assert d["identical"] == d["checked"] and not d["unverified"] and not d["errors"]
        assert calls[bad] == 2
        assert vr.UnverifiedStore(docs.db).pending(SRC_USER) == []      # the row goes only after a real compare

    def test_a_retry_that_finds_a_real_difference_reports_it_as_one(self, docs, monkeypatch):
        bad = docs.pairs[1][0]
        _fake(monkeypatch, {bad}, lambda: _err(503), then_ok_after=1, differ={bad})
        r = _verify(docs)
        assert r["verdict"] == "DIFFERENCES" and len(_drive(r)["differences"]) == 1
        assert not _drive(r)["unverified"] and _drive(r)["identical"] == _drive(r)["checked"] - 1

    def test_a_document_that_keeps_failing_stays_unverified_after_every_pass(self, docs, monkeypatch):
        bad = docs.pairs[4][0]
        calls = _fake(monkeypatch, {bad}, lambda: _err(503))
        r = _verify(docs)
        assert calls[bad] == 3 and r["verdict"] == "INCOMPLETE"          # first try + two retry passes
        assert vr.UnverifiedStore(docs.db).counts(SRC_USER) == {vr.UNVERIFIED_TRANSIENT: 1}

    def test_a_permanent_error_is_recorded_and_not_retried(self, docs, monkeypatch):
        bad = docs.pairs[0][0]
        e = resilience.PermanentAPIError("HTTP 403 (forbidden): no")
        calls = _fake(monkeypatch, {bad}, e)
        r = _verify(docs)
        u = _drive(r)["unverified"][0]
        assert calls[bad] == 1 and u["state"] == vr.UNVERIFIED_PERMANENT and r["verdict"] == "INCOMPLETE"

    def test_a_confirmed_difference_stays_distinct_from_an_unread_document(self, docs, monkeypatch):
        bad, differs = docs.pairs[0][0], docs.pairs[1][0]
        _fake(monkeypatch, {bad}, lambda: _err(503), differ={differs})
        r = _verify(docs)
        d = _drive(r)
        assert r["verdict"] == "DIFFERENCES"
        assert [x["source"] for x in d["differences"]] == [differs]      # the 503 is not listed as a difference
        assert [x["source"] for x in d["unverified"]] == [bad]
        assert any("not verified" in x for x in r["reasons"])

    def test_the_breaker_pauses_a_down_endpoint_and_still_never_passes_a_document(self, docs, monkeypatch):
        calls = _fake(monkeypatch, {s for s, _ in docs.pairs}, lambda: _err(503))
        r = _verify(docs)
        d = _drive(r)
        assert vr.breaker("docs").state in ("open", "half_open")
        assert d["identical"] == d["checked"] - 5 and len(d["unverified"]) == 5 and r["verdict"] == "INCOMPLETE"
        assert sum(calls.values()) < 5 * 3                               # the open breaker spared Google the rest

    def test_the_ladder_budget_hands_a_document_back_without_a_long_wait(self, docs, monkeypatch):
        bad = docs.pairs[0][0]
        _fake(monkeypatch, {bad}, lambda: _err(503, retry_after=1650))
        waited = []
        monkeypatch.setattr(resilience, "_wait", waited.append)
        retry = resilience.retry_on_google_error(max_retries=3, base_delay=0, max_delay=0, max_elapsed=180)
        r = _verify(docs, retry)
        assert max(waited or [0]) < 180 and r["verdict"] == "INCOMPLETE"

    def test_no_write_reaches_either_tenant(self, docs, monkeypatch):
        _fake(monkeypatch, {docs.pairs[0][0]}, lambda: _err(503))
        writes = ("files.update", "files.create", "files.delete", "files.copy", "permissions.create")
        before = {n: len(docs.auth.target_drive(TGT_USER).calls_to(n)) + len(docs.auth.source_drive(SRC_USER).calls_to(n))
                  for n in writes}
        _verify(docs)
        after = {n: len(docs.auth.target_drive(TGT_USER).calls_to(n)) + len(docs.auth.source_drive(SRC_USER).calls_to(n))
                 for n in writes}
        assert before == after

    def test_a_later_run_resolves_what_an_earlier_one_could_not_read(self, docs, monkeypatch):
        bad = docs.pairs[2][0]
        _fake(monkeypatch, {bad}, lambda: _err(503))
        _verify(docs)
        assert vr.UnverifiedStore(docs.db).pending(SRC_USER)
        # A later run starts after the breaker's cooldown. Files compare side by side, so the failing one can
        # finish last and leave the breaker open when this instant "later run" begins.
        vr.BREAKERS.clear()
        _fake(monkeypatch, set(), None)
        r = _verify(docs)
        assert r["verdict"] == "IDENTICAL" and vr.UnverifiedStore(docs.db).pending(SRC_USER) == [], (
            r["reasons"], {k: v.state for k, v in vr.BREAKERS.items()})


# -- verdicts, workers and what the heartbeat says -------------------------------------------------
def test_an_unverified_object_alone_makes_a_service_incomplete():
    r = {"checked": 5, "identical": 4, "differences": [], "missing": [], "extras": [], "duplicates": [],
         "notCopied": [], "errors": [], "unverified": [{"state": vr.UNVERIFIED_TRANSIENT}]}
    assert not V.service_complete(r)
    assert V.verdict_of({"u": {"drive": r}}, ("drive",))[0] == "INCOMPLETE"
    assert V.verdict_of({"u": {"drive": {**r, "unverified": []}}}, ("drive",))[0] == "IDENTICAL"


def test_a_requests_wait_is_bounded_by_a_configurable_http_timeout(monkeypatch):
    import auth
    monkeypatch.setenv("GOOGLE_HTTP_TIMEOUT_SEC", "7")
    assert auth._http().timeout == 7
    monkeypatch.delenv("GOOGLE_HTTP_TIMEOUT_SEC")
    assert auth._http().timeout == 300             # the engines are untouched


def test_the_heartbeat_gets_the_documents_state(auth, db, settings, identity):
    seen = []
    v = V.Verifier(auth, db, settings, SRC_USER, TGT_USER, on_progress=lambda **d: seen.append(d))
    v._publish_stats(currentDocument="d1", phase="request")
    st = seen[-1]["stats"]
    assert st["currentDocument"] == "d1" and st["phase"] == "request" and st["pendingRetry"] == 0
    assert {"verified", "identical", "different", "unverified", "lastSuccessfulDocument", "breakers"} <= set(st)


def test_one_api_can_have_a_longer_wait_than_the_rest(monkeypatch):
    """A large Doc takes over 90 s to read: under the verifier's 90 s bound it timed out on every pass and stayed
    unverified for ever (info@ and ward15@, 8 Nov 2026). Docs, Slides and Sheets get their own bound; Drive and
    Gmail keep the short one."""
    import auth
    monkeypatch.setenv("GOOGLE_HTTP_TIMEOUT_SEC", "90")
    monkeypatch.setenv("GOOGLE_HTTP_TIMEOUT_SEC_DOCS", "200")
    assert auth._http(service="docs").timeout == 200
    assert auth._http(service="drive").timeout == 90 and auth._http().timeout == 90
    monkeypatch.delenv("GOOGLE_HTTP_TIMEOUT_SEC_DOCS")
    assert auth._http(service="docs").timeout == 90               # nothing set: the general bound applies
