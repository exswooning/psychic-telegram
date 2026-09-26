"""
benchmarks.py -- what "good" means for a run, as data, and the rule that a
check nobody could make is not a pass.

Why this exists
---------------
benchmark_run.judge() gates a Drive rehearsal, and its own docstring records
why it starts where it does: B5's first attempt died after 17 seconds having
copied nothing, and the judge returned PASS, because every gate below it was a
statement about what landed on the target and an empty target satisfies all of
them vacuously. A verdict that cannot fail an empty run is not a safety net.

So every benchmark here can come back UNKNOWN, and the verdict treats a
required UNKNOWN as UNVERIFIED -- never as PASS. That matters most for
fidelity (do the tenants agree?), which cannot be read from the ledger at all:
the ledger says what the engine believes it did, only the tenants can say what
is there.

Thresholds are the tool's own numbers where it has measured them. Anything
else is a starting point to be tuned per tenant, and is overridable per
account (see evaluate's `overrides`) rather than edited here.
"""
from __future__ import annotations

from dataclasses import dataclass

PASS, WARN, FAIL, UNKNOWN = "pass", "warn", "fail", "unknown"


@dataclass(frozen=True)
class Benchmark:
    id: str
    label: str
    category: str            # health | reliability | performance | fidelity
    metric: str              # dotted path into the run's facts
    better: str              # "lower" or "higher"
    ok: float                # at or better than this: pass
    bad: float | None        # worse than this: fail; between ok and bad: warn.
                             # None means it can only ever warn.
    unit: str = ""           # "", "pct" (0-1 shown as a percentage), "s", "per_min"
    required: bool = True    # unknown + required => the verdict is UNVERIFIED
    why: str = ""


DEFAULTS: tuple[Benchmark, ...] = (
    # -- health: did the run happen at all --------------------------------
    # nonzeroExit, not the exit code itself: a process killed by a signal has a
    # NEGATIVE code (-6 for an abort), and "lower is better" reads -6 as fine.
    Benchmark("run_completed", "Run exited cleanly", "health", "run.nonzeroExit",
              "lower", 0, 0, unit="exit", required=True,
              why="A crashed run satisfies every other gate vacuously; this is "
                  "the one that cannot."),
    Benchmark("migrated_something", "Items were migrated", "health",
              "ledger.succeeded", "higher", 1, 1, required=True,
              why="Zero items copied is a failed run, however clean it looks."),
    # -- reliability -------------------------------------------------------
    Benchmark("item_failure_rate", "Item failure rate", "reliability",
              "ledger.failureRate", "lower", 0.01, 0.05, unit="pct",
              why="FAILED plus BLOCKED, over attempts that were not deliberate skips."),
    Benchmark("users_failed", "Users that failed", "reliability",
              "users.failedShare", "lower", 0.0, 0.02, unit="pct",
              why="A failed user is a whole mailbox and drive not moved."),
    Benchmark("blocked_items", "Items blocked (licences)", "reliability",
              "ledger.blocked", "lower", 0, None, required=False,
              why="BLOCKED means Google refused because of licence or user "
                  "limits; a re-run collects them once that is fixed."),
    Benchmark("retry_rate", "Calls retried", "reliability",
              "metrics.retryRate", "lower", 0.02, 0.10, unit="pct", required=False,
              why="Retries are how quota pressure shows up before failures do."),
    Benchmark("quota_pushbacks", "Quota pushbacks", "reliability",
              "metrics.pushbacks", "lower", 0, None, required=False,
              why="Not a failure -- the engine retries -- but the write ceiling is "
                  "being pushed."),
    # -- performance -------------------------------------------------------
    Benchmark("p95_latency", "p95 call latency", "performance", "metrics.p95",
              "lower", 3.0, 10.0, unit="s", required=False,
              why="Google queues before it rejects: latency climbs first."),
    Benchmark("items_per_min_per_worker", "Items per minute, per worker",
              "performance", "perf.itemsPerMinPerWorker", "higher", 30, 10,
              unit="per_min", required=False,
              why="Measured at about 62 on this project's live 16-32 worker "
                  "runs; the floor is half of that."),
    # -- fidelity: do the two tenants agree (needs the tally / verify) -----
    Benchmark("count_parity", "Source and target counts agree (worst service)",
              "fidelity", "fidelity.countParity", "higher", 0.999, 0.99, unit="pct",
              why="Target count over source count, after items the engine "
                  "deliberately skipped are accounted for."),
    Benchmark("checksum_failures", "Byte-level mismatches (sampled files)", "fidelity",
              "fidelity.checksumFailures", "lower", 0, 0,
              why="Corruption is never acceptable at any speed. Checked on a random sample of files, "
                  "so zero here means none found, not none exist."),
    Benchmark("acl_fidelity", "Share grants preserved (sampled users)", "fidelity",
              "fidelity.aclFidelity", "higher", 0.99, 0.99, unit="pct",
              why="Sharing that did not survive is access silently lost. Compared for every "
                  "file of the users the tally sampled."),
    Benchmark("extra_grants", "Grants that should not exist (sampled users)", "fidelity",
              "fidelity.extraGrants", "lower", 0, 0,
              why="Any grant the source did not have is a security regression."),
    Benchmark("timestamps_preserved", "Modified times preserved (sampled files)", "fidelity",
              "fidelity.timestampsPreserved", "higher", 0.99, 0.90, unit="pct",
              required=False,
              why="A migration that resets every file to 'today' is complete and "
                  "useless for sorting by last modified."),
)


# A seed run has no ledger, so it has its own yardsticks. They read the same
# way -- unknown is never a pass, and a run that did nothing fails.
SEED_DEFAULTS: tuple[Benchmark, ...] = (
    Benchmark("run_completed", "Run exited cleanly", "health", "run.nonzeroExit", "lower", 0, 0,
              unit="exit", required=True,
              why="A run that crashed part-way has not seeded what it says it did."),
    Benchmark("users_finished", "Users that finished", "health", "seed.finishedShare", "higher", 1.0, 0.95,
              unit="pct", required=True,
              why="A user who started and never reported is a mailbox and drive seeded only in part."),
    Benchmark("never_finished", "Users that never reported", "reliability", "seed.neverFinished", "lower", 0, 0,
              required=True,
              why="Judged once the run has ended; mid-run 'not finished' only means 'not yet'."),
    Benchmark("service_failures", "Users with a failed service", "reliability", "seed.failedServiceShare",
              "lower", 0.0, 0.10, unit="pct", required=False,
              why="A user can read as done while chat or contacts produced nothing at all."),
    Benchmark("warnings_per_user", "Warnings per user", "reliability", "seed.warningsPerUser", "lower", 1.0, None,
              required=False, why="A noisy run is usually one cause repeated; the families say which."),
    Benchmark("fill_reached", "Storage fill reached its target", "performance", "seed.fillReached", "higher",
              0.98, 0.90, unit="pct", required=False,
              why="Uploaded over planned, for a fill run once it has ended."),
)


def for_kind(kind: str) -> tuple[Benchmark, ...]:
    return SEED_DEFAULTS if kind == "seed" else DEFAULTS


def _get(facts: dict, path: str):
    cur = facts
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def fmt(value, unit: str) -> str:
    if value is None:
        return "not measured"
    if unit == "pct":
        return f"{value * 100:.2f}%"
    if unit == "s":
        return f"{value:.2f}s"
    if unit == "per_min":
        return f"{value:.1f}/min"
    if unit == "exit":
        return "clean exit" if not value else "non-zero exit"
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"


def _judge(b: Benchmark, value) -> str:
    if value is None or isinstance(value, bool):
        return UNKNOWN
    if b.better == "lower":
        if value <= b.ok:
            return PASS
        return WARN if b.bad is None or value <= b.bad else FAIL
    if value >= b.ok:
        return PASS
    return WARN if b.bad is None or value >= b.bad else FAIL


def _threshold_text(b: Benchmark) -> str:
    cmp_ok = "<=" if b.better == "lower" else ">="
    text = f"pass {cmp_ok} {fmt(b.ok, b.unit)}"
    if b.bad is not None and b.bad != b.ok:
        text += f", fail {'>' if b.better == 'lower' else '<'} {fmt(b.bad, b.unit)}"
    elif b.bad is not None:
        text += " (no grace)"
    return text


def evaluate(facts: dict, overrides: dict | None = None,
             benchmarks: tuple[Benchmark, ...] = DEFAULTS) -> dict:
    """Judge a run's facts. `overrides` maps a benchmark id to {"ok": .., "bad": ..}."""
    overrides = overrides or {}
    results = []
    for b in benchmarks:
        o = overrides.get(b.id) or {}
        if "ok" in o or "bad" in o:
            b = Benchmark(**{**b.__dict__, "ok": o.get("ok", b.ok), "bad": o.get("bad", b.bad)})
        value = _get(facts, b.metric)
        status = _judge(b, value)
        results.append({
            "id": b.id, "label": b.label, "category": b.category, "status": status,
            "value": value, "display": fmt(value, b.unit), "threshold": _threshold_text(b),
            "required": b.required, "why": b.why, "metric": b.metric,
        })
    counts = {s: sum(1 for r in results if r["status"] == s) for s in (PASS, WARN, FAIL, UNKNOWN)}
    unverified = [r["id"] for r in results if r["status"] == UNKNOWN and r["required"]]
    if counts[FAIL]:
        verdict = "FAIL"
    elif unverified:
        verdict = "UNVERIFIED"
    else:
        verdict = "PASS"
    return {"results": results, "counts": counts, "verdict": verdict, "unverified": unverified}
