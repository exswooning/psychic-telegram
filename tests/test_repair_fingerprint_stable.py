"""The same finding keeps the same fingerprint across checks, so a repair that cannot clear it runs once.

a. -> b., 17 Nov 2026: each check wrote a new provenance runId/startedAt and new cache counters, the
fingerprint changed every time, and the dispatcher re-verified the same five users every ~3 minutes."""
import verification_repairs as R


def row(run, started, reused):
    return {"user": "erin@a.example", "service": "drive", "verdict": "DIFFERENCES", "verifiedAt": started,
            "differences": [{"source": "s1", "target": "t1", "diffs": ["native document content differs"]}],
            "provenance": {"runId": run, "startedAt": started}, "cache": {"reusedComparisons": reused}}


def test_two_checks_of_the_same_finding_share_a_fingerprint():
    assert R.fingerprint([row("r1", "2026-11-17T15:28:11Z", 0)]) == R.fingerprint([row("r2", "2026-11-17T15:34:27Z", 7)])


def test_a_changed_finding_changes_the_fingerprint():
    a = row("r1", "t", 0)
    b = row("r1", "t", 0)
    b["differences"] = []
    assert R.fingerprint([a]) != R.fingerprint([b])
