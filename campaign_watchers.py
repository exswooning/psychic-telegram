"""The watchers of the campaign: each `oversight.watch_*` check and each incident kind opens and closes
(fableplan2 P2.11). Replaces the stub `watch:*` scenarios of campaign_scenarios (same ids and format).

Every watcher has an OFFLINE test, `tests/test_campaign_watchers.py`, that crafts the state in a copy of a
ledger (`ab_transfer.ledger_copy`, as the A/B arms do) or in the control-plane database, runs the real check
or `run_watch` logic, asserts the incident opens, removes the condition and asserts it closes by evidence. Where
the plan says a watcher opens inside a failure scenario (`opens_in`), the scenario reads the incident that run
caused (`how: check_only`, the incident table is the evidence); the rest are covered by their offline test
(`how: unseedable`, `unit_test` = that test). The teardown incident is the operator's: never caused.

`opens_in` differs from campaign_scenarios.WATCHER_OPENS_IN where that mapping cannot open the incident:
  source_changed, watch_source_changed   a sandbox source has no tripwire; only a protected source opens them
                                         (err:drive.source_changed lifts a.'s declaration for it)
  stalled                                a deliberate memory pause (F-memory) exits 3 and is not a stall
  traceback                              a seed's log growing a Traceback, not a migrate dying (F-kill)
"""
from __future__ import annotations

import campaign_scenarios as S

TESTS = "tests/test_campaign_watchers.py"
# name -> (opens_in, offline test function, what opens it, the evidence that closes it)
WATCHERS = {
    "watch_repair_loop": ("", "test_repair_loop", "3 repair runs, an automatic-kind finding still open",
                          "the finding's row is resolved"),
    "watch_slow_file": ("fail:F-rate", "test_slow_file", "a heartbeat 600 s past its budget while the job runs",
                        "the job is no longer running"),
    "watch_verdict_flip": ("", "test_verdict_flip", "IDENTICAL -> DIFFERENCES with no run or repair between",
                           "the user is checked again with the same answer"),
    "watch_unclassified": ("", "test_unclassified", "a FAILED row no known_issues kind names",
                           "the item later succeeds (no FAILED row left)"),
    "watch_source_changed": ("err:drive.source_changed", "test_source_changed",
                             "a SOURCE_CHANGED row inside the day", "no such row inside the day"),
    "watch_box_busy": ("fail:F-api", "test_box_busy", "no idle minute for 3 h and a job running",
                       "a gap of 60 s between jobs inside the window"),
    "watch_tenant_ready": ("fail:F-scope", "test_tenant_ready", "readiness asked of Google answers a definite no",
                           "the next hourly answer is ready"),
    "watch_google_status": ("", "test_google_status", "an open Google incident on a product Bitport uses",
                            "its end time is set"),
    "watch_isolation": ("", "test_isolation", "a job log naming another account's domain",
                        "the log no longer names it"),
    "watch_limiter_low": ("fail:F-rate", "test_limiter_low", "a Drive limiter under 25/s for 10 minutes of heartbeats",
                          "the rate is healthy again"),
    "watch_fallbacks": ("", "test_fallbacks", "8 of 100 copies in 10 minutes arrived only by a fallback route",
                        "no fallback row is left in the window"),
    "watch_recurring": ("", "test_recurring", "one warning shape on two days and again today",
                        "it appears on one day only"),
    "crashed": ("fail:F-kill", "test_crashed", "a migrate exits 137 with no Stop on record",
                "a later migrate exits 0"),
    "stalled": ("", "test_stalled", "the supervisor ends a wedged run (api_server's own open_incident call)",
                "a later run of that job exits 0"),
    "failing": ("fail:F-suspend", "test_failing", "25+ new FAILED rows between two looks while migrate runs",
                "none of that failure family is left in the ledger"),
    "traceback": ("", "test_traceback", "a seed's log grows a Traceback", "a later seed exits 0"),
    "verdict_fail": ("fail:F-tamper", "test_verdict_fail", "a clean exit whose report fails count_parity",
                     "a later report passes that check"),
    "disk_full": ("fail:F-disk", "test_disk_full", "the scratch filesystem is 90% full (housekeeping's loop)",
                  "it is under 90% again"),
    "dms_not_started": ("fail:F-dms-held", "test_dms_not_started", "the DMS declined over a failed user",
                        "the DMS is really going: finished, running 30 min, or stopped by a person"),
    "source_changed": ("err:drive.source_changed", "test_source_changed_incident",
                       "the source guard saw a change from this tool's own addresses",
                       "the guard's next check reads CLEAN"),
}
NOT_CAUSED = {"teardown": "teardown starts only on the operator's click on Approve (lifecycle.py); a campaign "
                          "never approves a migration, so its incident is never caused. Unit tests stand."}


def _scenarios() -> dict:
    out = {}
    for name, (opens_in, test, cause, evidence) in WATCHERS.items():
        for phase, what in (("opens", cause), ("closes", evidence)):
            live = bool(opens_in)
            out[f"watch:{name}:{phase}"] = S.Scenario(
                id=f"watch:{name}:{phase}", group="watchers", title=f"{name} {phase}: {what}",
                how="check_only" if live else "unseedable",
                inject={"where": f"failure:{opens_in[5:]}" if opens_in.startswith("fail:") else opens_in or "ledger",
                        "opens_in": opens_in, "offline_test": f"{TESTS}::{test}", "what": what},
                expect={"incident": name, phase: True, "closes_on": "evidence, never time"},
                unit_test="" if live else f"{TESTS}::{test}")
    for name, why in NOT_CAUSED.items():
        for phase in ("opens", "closes"):
            out[f"watch:{name}:{phase}"] = S.Scenario(
                id=f"watch:{name}:{phase}", group="watchers", title=f"{name} {phase}: not caused ({why[:60]})",
                how="unseedable", inject={"where": "never", "why": why},
                expect={"incident": name, phase: True, "closes_on": "evidence, never time"},
                unit_test="tests/test_lifecycle.py")
    return out


SCENARIOS = _scenarios()


def register() -> None:
    prior = S.all_scenarios
    if getattr(prior, "_has_watchers", False):
        return

    def all_scenarios() -> list:
        return [SCENARIOS.get(s.id, s) for s in prior()]
    all_scenarios._has_watchers = True
    S.all_scenarios = all_scenarios


register()
