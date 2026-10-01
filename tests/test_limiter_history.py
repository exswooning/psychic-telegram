"""The limiter timeline the Metrics page draws as a sawtooth.

Two sources are merged: each stored snapshot carries every limiter's rate at
that moment (present in history recorded before events existed), and newer
snapshots also carry each probe and backoff as it happened.
"""
import api_server


def _snap(at, limiters=None, events=None):
    s = {"recordedAt": at}
    if limiters is not None:
        s["limiters"] = limiters
    if events is not None:
        s["limiter_events"] = events
    return s


def test_old_history_still_yields_a_coarse_trace_from_the_snapshots():
    got = api_server._limiter_history([       # newest first, as the API reads them
        _snap("2026-09-25T10:00:15Z", {"target": {"rate": 60.0}}),
        _snap("2026-09-25T10:00:00Z", {"target": {"rate": 50.0}}),
    ])
    pts = got["target"]
    assert [p["rate"] for p in pts] == [50.0, 60.0]        # oldest first
    assert {p["kind"] for p in pts} == {"sample"}


def test_events_are_merged_in_time_order_with_their_kind():
    got = api_server._limiter_history([
        _snap("2026-09-25T10:00:15Z", {"target": {"rate": 40.0}},
              {"target": [[1790330408.0, 44.0, "probe"], [1790330410.0, 30.0, "backoff"]]}),
    ])
    kinds = [(p["kind"], p["rate"]) for p in got["target"]]
    # The snapshot's own instant falls after both events (10:00:15Z).
    assert kinds[:2] == [("probe", 44.0), ("backoff", 30.0)]
    assert kinds[-1] == ("sample", 40.0)


def test_each_limiter_gets_its_own_trace():
    got = api_server._limiter_history([
        _snap("2026-09-25T10:00:00Z", {"source": {"rate": 1200.0}, "target": {"rate": 55.0}})])
    assert set(got) == {"source", "target"}


def test_an_unreadable_timestamp_drops_that_sample_not_the_rest():
    got = api_server._limiter_history([
        _snap("not a time", {"target": {"rate": 1.0}},
              {"target": [[1790330408.0, 44.0, "probe"]]})])
    assert [p["kind"] for p in got["target"]] == ["probe"]


def test_a_malformed_event_is_skipped():
    got = api_server._limiter_history([
        _snap("2026-09-25T10:00:00Z", None, {"target": [[1.0, 2.0], "junk",
                                                          [3.0, 4.0, "probe"]]})])
    assert got == {"target": [{"t": 3.0, "rate": 4.0, "kind": "probe"}]}


def test_no_snapshots_is_an_empty_timeline():
    assert api_server._limiter_history([]) == {}


def test_only_the_newest_run_is_drawn():
    """Found live: a run from 29 Sept (281/s) joined to today's fresh limiter (40/s)
    drew a pushback Google never sent."""
    new = {"recordedAt": "2026-10-01T07:30:00Z", "elapsed_sec": 120.0,
           "limiters": {"source": {"rate": 40.0}}}
    old = {"recordedAt": "2026-09-29T04:48:00Z", "elapsed_sec": 9000.0,
           "limiters": {"source": {"rate": 281.0}}}
    kept = api_server._this_run([new, old])
    assert kept == [new]
    assert [p["rate"] for p in api_server._limiter_history(kept)["source"]] == [40.0]


def test_samples_without_elapsed_are_all_kept():
    rows = [_snap("2026-10-01T07:30:00Z"), _snap("2026-09-29T04:48:00Z")]
    assert api_server._this_run(rows) == rows
