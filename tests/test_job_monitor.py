"""The job monitor spends tokens only on news: repeats and known kinds cost nothing, a lane is serial,
lanes run side by side, caps hold, and nothing it remembers lives in a context window."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from monitor import watch as W

L = ("2026-11-02T01:00:00.000Z INCIDENT #{id} error crashed account={acct} job={job} :: {msg} "
     "-> brief: /opt/bitport/logs/incidents/{id}.md")


def line(id=1, acct=3, job="verify", msg="exited with signal 9"):
    return L.format(id=id, acct=acct, job=job, msg=msg)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "INBOX", str(tmp_path / "INBOX.md"))
    monkeypatch.setattr(W, "STATE", str(tmp_path / "state.json"))
    calls = []

    def run(model, prompt, cwd=None, tools="", budget=0.05, timeout=0, extra=None):
        calls.append({"model": model, "prompt": prompt, "tools": tools, "extra": extra})
        if model == W.TRIAGE_MODEL:
            return json.dumps({"action": run.triage, "reason": "r", "hypothesis": "h"}), 0.01
        return json.dumps({"status": run.fix, "summary": "did it", "files": [], "tests": "ok"}), 0.50
    run.triage, run.fix = "escalate", "fixed"
    return type("E", (), {"run": staticmethod(run), "calls": calls, "state": W.load(str(tmp_path / "state.json")),
                          "git": staticmethod(lambda *a, **k: None), "fetch": staticmethod(lambda p: "BRIEF TEXT")})


def go(env, text, **kw):
    ev = W.parse(text)
    return W.handle(env.state, ev, env.run, env.fetch, env.git)


def test_a_feed_line_is_parsed_into_a_lane():
    ev = W.parse(line(job="Regression set: four users, server-side + split"))
    assert ev["id"] == "1" and ev["kind"] == "crashed" and W.lane_of(ev) == "3:regression set"
    assert W.parse("2026-11-02T08:08:01.000Z FINISHED account=3 job=verify rc=0 verdict=None report=None") is None


def test_the_same_failure_with_other_numbers_is_one_fingerprint():
    a, b = W.parse(line(id=1, msg="stalled for 2897s pid 4001")), W.parse(line(id=2, msg="stalled for 960s pid 7733"))
    assert W.fingerprint(a) == W.fingerprint(b)
    assert W.fingerprint(a) != W.fingerprint(W.parse(line(id=3, acct=7, msg="stalled for 960s pid 7733")))


def test_a_new_incident_goes_to_the_cheap_model_first_and_only_then_the_strong_one(env):
    assert go(env, line()) == "fixed"
    assert [c["model"] for c in env.calls] == [W.TRIAGE_MODEL, W.FIX_MODEL]
    assert env.calls[0]["tools"] == ""                     # triage reads, never acts
    assert "git push" in " ".join(env.calls[1]["extra"])    # and the fixer cannot push or deploy
    assert "BRIEF TEXT" in env.calls[0]["prompt"]
    assert "FIXED on monitor/incident-1" in open(W.INBOX).read()


def test_a_repeat_costs_nothing(env):
    go(env, line(id=1)); n = len(env.calls)
    assert go(env, line(id=2)).startswith("skip") and len(env.calls) == n


def test_a_cheap_ignore_never_wakes_the_strong_model(env):
    env.run.triage = "ignore"
    assert go(env, line()) == "ignored" and [c["model"] for c in env.calls] == [W.TRIAGE_MODEL]


def test_a_kind_bitport_fixes_itself_is_not_sent_to_any_model(env):
    msg = "Google refused or failed a read during the check HTTP 503"
    assert go(env, line(msg=msg)) == "auto" and env.calls == []


def test_a_failed_fix_stays_open_and_is_retried_only_up_to_the_cap(env, monkeypatch):
    env.run.fix = "cannot"
    assert go(env, line(id=1, msg="boom one")) == "open"
    ev2 = line(id=2, msg="boom one")
    assert go(env, ev2) == "open"                           # second and last attempt
    assert go(env, line(id=3, msg="boom one")).startswith("skip")


def test_the_daily_dollar_cap_stops_every_model_call(env, monkeypatch):
    monkeypatch.setattr(W, "MAX_USD_DAY", 0.0)
    assert go(env, line()) == "skip:budget" and env.calls == []


def test_the_daily_escalation_cap_holds(env, monkeypatch):
    monkeypatch.setattr(W, "MAX_ESCALATIONS_DAY", 1)
    go(env, line(id=1, msg="first problem"))
    assert go(env, line(id=2, msg="a different second problem")) == "queued-over-cap"
    assert [c["model"] for c in env.calls].count(W.FIX_MODEL) == 1


def test_a_resolved_line_closes_its_incident(env):
    env.run.triage = "note"
    go(env, line(id=5, msg="odd thing"))
    pool = ThreadPoolExecutor(1)
    W.poll(env.state, ["2026-11-02T02:00:00.000Z RESOLVED #5 automatically :: a later run of the same job exited 0"], pool, env.run, env.fetch, env.git)
    assert {r["status"] for r in env.state["fps"].values()} == {"closed"}


def test_two_jobs_are_two_feeds_one_job_is_serial(env):
    """Different lanes overlap in time; the same lane never does."""
    active, peak, by_lane = [], {"n": 0}, {}

    def slow(model, prompt, **kw):
        lane = "A" if "lane=3:verify" in prompt else "B"
        with threading.Lock():
            active.append(lane); by_lane.setdefault(lane, 0)
            by_lane[lane] = max(by_lane[lane], active.count(lane)); peak["n"] = max(peak["n"], len(active))
        time.sleep(0.15)
        active.remove(lane)
        return json.dumps({"action": "ignore", "reason": "", "hypothesis": ""}), 0.0
    pool = ThreadPoolExecutor(4)
    futs = W.poll(env.state, [line(id=1, job="verify", msg="a1"), line(id=2, job="verify", msg="a2 different"),
                              line(id=3, job="dms", msg="b1")], pool, slow, env.fetch, env.git)
    [f.result() for f in futs]
    assert peak["n"] == 2 and by_lane["A"] == 1             # verify and dms together; verify never twice at once


def test_state_survives_a_restart_and_the_brief_names_what_is_open(env, tmp_path):
    env.run.triage = "note"
    go(env, line(id=9, msg="unusual thing"))
    W.save({k: v for k, v in env.state.items() if k != "_locks"}, str(tmp_path / "state.json"))
    again = W.load(str(tmp_path / "state.json"))             # a new process, no context of the old one
    text = W.brief(again)
    assert "CHARTER.md" in text and "3:verify crashed noted" in text and "open: 1" in text


def test_the_charter_is_what_every_call_starts_from(env):
    go(env, line())
    assert all(W.charter()[:60] in c["prompt"] for c in env.calls)


def test_a_first_start_skips_the_feeds_history(monkeypatch, tmp_path):
    monkeypatch.setattr(W, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(W, "fetch_feed", lambda off: ([line(id=1)] if off == 0 else [], 4242))
    polled = []
    monkeypatch.setattr(W, "poll", lambda state, lines, pool, *a, **k: polled.append(lines) or [])
    monkeypatch.setattr(W, "save", lambda s, p=None: None)
    assert W.main(["--once"]) == 0
    assert polled == [[]]                                   # history was not handed to any model
