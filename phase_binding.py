"""Measure what limits each phase of one account's run.

The analyzer is read-only. Run it against the account ledger while the job is
live (or immediately after): ledger phase timestamps and mappings supply work
counts, the last-hour ``run_metrics`` series supplies limiter/worker readings,
the DMS history supplies Google's import counters, and ``sampler.py`` supplies
host CPU and interface byte counters. Missing evidence is UNKNOWN; it is never
turned into an idle-worker/NOTHING verdict.

Example::

    .venv/bin/python phase_binding.py --account-id 5 --run 12 \
        --sample logs/phase-binding/5.jsonl --line-bytes-per-sec 900000
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sqlite3
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
PHASES = ("drive", "mail", "dms_import", "dms_check", "verify", "tally", "seeding")
PHASE_LABELS = {
    "drive": "Drive pass", "mail": "Mail pass", "dms_import": "DMS import",
    "dms_check": "DMS check", "verify": "Verify", "tally": "Tally",
    "seeding": "Seeding",
}
GOOGLE_QUOTA, LINE, CPU, NOTHING, UNKNOWN = (
    "Google quota", "the line", "the CPU", "NOTHING", "UNKNOWN")
CPU_BUSY_PCT = 85.0
LINE_SATURATION = 0.80
MAX_IDLE_FOR_CPU = 0.15
DEFAULT_LINE_BYTES_PER_SEC = 900_000.0  # the measured 0.9 MB/s uplink baseline

_TIME_RE = re.compile(
    r"(?P<stamp>20\d\d-\d\d-\d\d[T ]\d\d:\d\d:\d\d"
    r"(?:\.\d+)?(?:Z|[+-]\d\d:?\d\d)?)")
_SEED_DONE_RE = re.compile(r"Seeded\s+[\d,]+\s+users\s+in\s+([\d.]+)s", re.I)
_TALLY_RE = re.compile(r"tally:\s*\[(\d+)/(\d+)\]\s*users counted", re.I)


def _epoch(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        stamp = value.strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(stamp)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, OverflowError):
        return None


def _iso(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?", (name,)
    ).fetchone())


def _canonical_phase(name: str) -> list[str]:
    text = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    found = []
    # More specific DMS check markers must win before generic DMS import.
    if "dms" in text and any(x in text for x in ("check", "reconcil", "proof")):
        found.append("dms_check")
    elif "dms" in text:
        found.append("dms_import")
    if "verify" in text or "one_to_one" in text or "parity" in text:
        found.append("verify")
    if "tally" in text:
        found.append("tally")
    if "seed" in text:
        found.append("seeding")
    if "drive" in text:
        found.append("drive")
    if "gmail" in text or "mail" in text:
        found.append("mail")
    return list(dict.fromkeys(found))


def _connect_readonly(path: str) -> sqlite3.Connection:
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"ledger does not exist: {resolved}")
    conn = sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _load_run(conn: sqlite3.Connection, run_id: int | None) -> dict:
    if not _table_exists(conn, "run_records"):
        return {"id": run_id, "startedAt": None, "pid": None, "phases": [], "kind": None}
    if run_id is None:
        row = conn.execute(
            "SELECT run, started_at, pid FROM run_records ORDER BY run DESC LIMIT 1").fetchone()
    else:
        row = conn.execute(
            "SELECT run, started_at, pid FROM run_records WHERE run=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError(f"run {run_id} is not present in this ledger")
    if row is None:
        return {"id": run_id, "startedAt": None, "pid": None, "phases": [], "kind": None}
    phases = []
    if _table_exists(conn, "run_phases"):
        for r in conn.execute(
                "SELECT name, kind, state, started_at, ended_at, note FROM run_phases "
                "WHERE run=? ORDER BY seq", (row["run"],)):
            phases.append({"name": r["name"], "kind": r["kind"], "state": r["state"],
                           "startedAt": r["started_at"], "endedAt": r["ended_at"], "note": r["note"]})
    return {"id": row["run"], "startedAt": row["started_at"], "pid": row["pid"],
            "phases": phases, "kind": "migrate"}


def _phase_windows(run: dict, explicit: dict[str, tuple[float, float]],
                   logs: list[dict], summaries: list[dict]) -> dict[str, dict]:
    now = time.time()
    windows: dict[str, dict] = {}
    for p in run.get("phases", []):
        start, end = _epoch(p.get("startedAt")), _epoch(p.get("endedAt"))
        if start is None:
            continue
        end = end if end is not None else now
        for phase in _canonical_phase(p.get("name", "")):
            windows.setdefault(phase, {"start": start, "end": end, "source": "run_phases",
                                       "recordedName": p.get("name"), "state": p.get("state")})

    # Seed runs have a kept summary but no run_phases row. The summary's
    # elapsed_sec is process uptime at its last sample; for an active seed,
    # updated_at is the latest trustworthy end of the observation window.
    seed = next((s for s in summaries if s.get("kind") == "seed"), None)
    if run.get("kind") == "seed" and seed and "seeding" not in windows:
        start = _epoch(seed.get("startedAt"))
        elapsed = seed.get("elapsed_sec")
        end = start + float(elapsed) if start is not None and isinstance(elapsed, (int, float)) else _epoch(seed.get("updatedAt"))
        if start is not None and end is not None and end >= start:
            windows["seeding"] = {"start": start, "end": min(end, now), "source": "run_metric_summary",
                                  "recordedName": seed.get("runKey"), "state": "observed"}

    # Run logs split the DMS pipeline's recorded `dms` follow-on into the
    # external import and its later local reconciliation when timestamps exist.
    dms_window = windows.get("dms_import")
    dms_run_end = dms_window.get("end") if dms_window else None
    for log in logs:
        import_end = log.get("dms_import_end")
        check_start, check_end = log.get("dms_check_start"), log.get("dms_check_end")
        if (dms_window and import_end and dms_window["start"] <= import_end <= dms_window["end"]):
            dms_window["end"] = min(dms_window["end"], import_end)
        if (check_start and check_end and check_end >= check_start
                and (not dms_window or check_end >= dms_window["start"])
                and (dms_run_end is None or check_start <= dms_run_end)):
            windows["dms_check"] = {"start": check_start, "end": check_end,
                                    "source": "run_log", "recordedName": log["path"],
                                    "state": "observed"}
        seed_start, seed_end = log.get("seed_start"), log.get("seed_end")
        if seed_start is not None and seed_end is not None and "seeding" not in windows:
            windows["seeding"] = {"start": seed_start, "end": seed_end, "source": "run_log",
                                  "recordedName": log["path"], "state": "observed"}

    for phase, (start, end) in explicit.items():
        windows[phase] = {"start": start, "end": end, "source": "--phase-window",
                          "recordedName": phase, "state": "observed"}
    return windows


def _read_metric_rows(conn: sqlite3.Connection, limit: int = 240) -> list[dict]:
    if not _table_exists(conn, "run_metrics"):
        return []
    rows = conn.execute(
        "SELECT recorded_at, payload FROM run_metrics ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for row in reversed(rows):
        try:
            sample = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        sample["recordedAt"] = row["recorded_at"]
        sample["_epoch"] = _epoch(row["recorded_at"])
        out.append(sample)
    return out


def _read_run_summaries(conn: sqlite3.Connection) -> list[dict]:
    if not _table_exists(conn, "run_metric_summary"):
        return []
    out = []
    for r in conn.execute(
            "SELECT run_key,kind,source_domain,target_domain,started_at,updated_at,payload "
            "FROM run_metric_summary ORDER BY started_at DESC LIMIT 30"):
        try:
            value = json.loads(r["payload"] or "{}")
        except (TypeError, ValueError):
            value = {}
        out.append({"runKey": r["run_key"], "kind": r["kind"], "sourceDomain": r["source_domain"],
                    "targetDomain": r["target_domain"], "startedAt": r["started_at"],
                    "updatedAt": r["updated_at"], **value})
    return out


def _read_ceiling_rows(conn: sqlite3.Connection) -> dict[str, float]:
    if not _table_exists(conn, "rate_limiter_ceiling"):
        return {}
    return {str(r["tenant"]): float(r["ceiling"]) for r in conn.execute(
        "SELECT tenant,ceiling FROM rate_limiter_ceiling") if r["ceiling"] is not None}


def _auto_log_paths(account_id: int) -> list[str]:
    base = os.path.join(HERE, "logs", "jobs", str(account_id))
    if not os.path.isdir(base):
        return []
    patterns = ("migrate*.log", "delta*.log", "dms*.log", "verify*.log", "one-to-one*.log",
                "one_to_one*.log", "tally*.log", "user-tally*.log", "seed*.log")
    found = set()
    for pattern in patterns:
        found.update(glob.glob(os.path.join(base, pattern)))
        found.update(glob.glob(os.path.join(base, "runs", pattern)))
    return sorted(found)


def _read_log(path: str) -> dict:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return {"path": path}
    timestamps: list[tuple[float, str]] = []
    seed_seconds = None
    seed_totals: dict[str, int] = {}
    dms_import_end = dms_check_start = dms_check_end = None
    dms_check_items = None
    tally_users = None
    tally_at = None
    verify_items = None
    verify_at = None
    seed_done_at = None
    for line in lines:
        match = _TIME_RE.search(line)
        ts = _epoch(match.group("stamp")) if match else None
        if ts is not None:
            timestamps.append((ts, line))
        lower = line.lower()
        if ts is not None and ("the import has finished" in lower or
                               re.search(r"import status:\s*complete", lower)):
            dms_import_end = ts
        if ts is not None and "reconcil" in lower and "dms" in lower:
            if "starting" in lower:
                dms_check_start = ts if dms_check_start is None else min(dms_check_start, ts)
            if "item checks finished" in lower:
                dms_check_end = ts
            if "dms delivery reconciliation:" in lower:
                dms_check_end = ts
                try:
                    data = json.loads(line.split("DMS delivery reconciliation:", 1)[1].strip())
                    if isinstance(data, dict) and isinstance(data.get("checked"), (int, float)):
                        dms_check_items = int(data["checked"])
                except (ValueError, IndexError):
                    pass
            for m in re.finditer(r"(\d[\d,]*) checked", line, re.I):
                dms_check_items = max(dms_check_items or 0, int(m.group(1).replace(",", "")))
        tm = _TALLY_RE.search(line)
        if tm:
            if int(tm.group(1)) >= (tally_users or 0):
                tally_users, tally_at = int(tm.group(1)), ts
        vm = re.search(r"(?:verify|verified).*?checked[=: ]+(\d[\d,]*)", line, re.I)
        if vm:
            count = int(vm.group(1).replace(",", ""))
            if count >= (verify_items or 0):
                verify_items, verify_at = count, ts
        sm = _SEED_DONE_RE.search(line)
        if sm:
            seed_seconds = float(sm.group(1))
            seed_done_at = ts
        for key, pattern in (
                ("files", r"OWNED files\s*:\s*([\d,]+)"),
                ("folders", r"Folders\s*:\s*([\d,]+)"),
                ("messages", r"Emails\s*:\s*([\d,]+)"),
                ("events", r"Events\s*:\s*([\d,]+)"),
                ("contacts", r"Contacts\s*:\s*([\d,]+)"),
                ("tasks", r"Tasks\s*:\s*([\d,]+)")):
            got = re.search(pattern, line, re.I)
            if got:
                seed_totals[key] = int(got.group(1).replace(",", ""))
    seed_start = seed_end = None
    if seed_done_at is not None and seed_seconds is not None:
        seed_end = seed_done_at
        seed_start = seed_end - seed_seconds
    dms_start_line = next((ts for ts, line in timestamps if "reconcil" in line.lower()
                           and "dms" in line.lower()
                           and "starting" in line.lower()), None)
    return {"path": path, "first_timestamp": timestamps[0][0] if timestamps else None,
            "last_timestamp": timestamps[-1][0] if timestamps else None,
            "dms_import_end": dms_import_end, "dms_check_start": dms_check_start or dms_start_line,
            "dms_check_end": dms_check_end, "dms_check_items": dms_check_items,
            "tally_users": tally_users, "tally_at": tally_at,
            "verify_items": verify_items, "verify_at": verify_at,
            "seed_start": seed_start, "seed_end": seed_end, "seed_seconds": seed_seconds,
            "seed_totals": seed_totals}


def _read_dms_history(path: str) -> list[dict]:
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                metric = row.get("metrics") or {}
                at = _epoch(row.get("read_at"))
                if at is not None and "Emails imported" in metric:
                    out.append({"at": at, "imported": int(metric.get("Emails imported") or 0),
                                "status": row.get("status")})
    except OSError:
        pass
    return sorted(out, key=lambda r: r["at"])


def _read_sampler(path: str | None) -> list[dict]:
    if not path:
        return []
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            at = _epoch(row.get("timestamp") or row.get("recordedAt"))
            if at is None:
                continue
            row["_epoch"] = at
            rows.append(row)
    return sorted(rows, key=lambda r: r["_epoch"])


def _phase_metrics(all_metrics: list[dict], window: dict, ceiling_rows: dict[str, float]) -> dict:
    start, end = window["start"], window["end"]
    within = [r for r in all_metrics if r.get("_epoch") is not None and start <= r["_epoch"] < end]
    before = [r for r in all_metrics if r.get("_epoch") is not None and r["_epoch"] < start]
    tenants = set(ceiling_rows)
    for sample in within:
        tenants.update((sample.get("limiters") or {}).keys())
    limiter = {}
    for tenant in sorted(tenants):
        observed = [(r, (r.get("limiters") or {}).get(tenant)) for r in within]
        observed = [(r, s) for r, s in observed if isinstance(s, dict)]
        rates = [float(s["rate"]) for _, s in observed if isinstance(s.get("rate"), (int, float))]
        ceilings = [float(s["ceiling"]) for _, s in observed if isinstance(s.get("ceiling"), (int, float))]
        series = [(r, float(s["rejections"])) for r, s in observed
                  if isinstance(s.get("rejections"), (int, float))]
        prior_series = [(r, float((r.get("limiters") or {}).get(tenant, {}).get("rejections")))
                        for r in before
                        if isinstance((r.get("limiters") or {}).get(tenant), dict)
                        and isinstance((r.get("limiters") or {}).get(tenant, {}).get("rejections"), (int, float))]
        rejection_count = None
        if series:
            base = prior_series[-1][1] if prior_series else None
            if base is not None:
                delta = series[-1][1] - base
                # A counter reset (or a process switch) makes this phase's
                # exact delta unknowable; never turn it into a clean zero.
                if delta >= 0:
                    rejection_count = int(delta)
        backoffs = 0
        saw_events = False
        for r, _state in observed:
            events = (r.get("limiter_events") or {}).get(tenant)
            if isinstance(events, list):
                saw_events = True
                backoffs += sum(1 for e in events if isinstance(e, (list, tuple))
                                and len(e) >= 3 and str(e[2]).lower() == "backoff")
        limiter[tenant] = {
            "ratePerSec": round(statistics.median(rates), 3) if rates else None,
            "ceilingPerSec": round(ceilings[-1], 3) if ceilings else ceiling_rows.get(tenant),
            "rejections": rejection_count,
            "backoffs": backoffs if saw_events else None,
            "cumulativeRejections": series[-1][1] if series else None,
            "samples": len(observed),
        }
    rejection_values = [v["rejections"] for v in limiter.values() if v["rejections"] is not None]
    all_rejection_counters_known = bool(limiter) and all(
        v["rejections"] is not None for v in limiter.values())
    backoff_values = [v["backoffs"] for v in limiter.values() if v["backoffs"] is not None]
    quota_backoffs = sum(backoff_values) if backoff_values else 0

    busy_shares = []
    for r in within:
        share = r.get("idle_workers_share")
        if isinstance(share, (int, float)):
            busy_shares.append(float(share))
        elif (isinstance(r.get("worker_slots_configured"), (int, float))
              and r["worker_slots_configured"] > 0
              and isinstance(r.get("worker_slots_busy"), (int, float))):
            busy_shares.append(max(0.0, min(1.0,
                1 - r["worker_slots_busy"] / r["worker_slots_configured"])))
    return {"metricsSamples": len(within), "limiters": limiter,
            "rejections": sum(rejection_values) if all_rejection_counters_known else None,
            "quotaBackoffs": quota_backoffs,
            "idleWorkersShare": statistics.mean(busy_shares) if busy_shares else None,
            "idleWorkerSamples": len(busy_shares)}


def _phase_counts(conn: sqlite3.Connection, phase: str, window: dict, logs: list[dict],
                  dms_history: list[dict]) -> tuple[int | None, str]:
    start, end = window["start"], window["end"]
    start_s, end_s = _iso(start), _iso(end)
    if phase in ("drive", "mail") and _table_exists(conn, "id_mapping"):
        item_types = ("file", "folder") if phase == "drive" else ("message",)
        marks = ",".join("?" * len(item_types))
        count = conn.execute(
            f"SELECT COUNT(*) FROM id_mapping WHERE type IN ({marks}) AND created_at>=? AND created_at<?",
            (*item_types, start_s, end_s)).fetchone()[0]
        return int(count), "files+folders" if phase == "drive" else "messages"
    if phase == "drive" and _table_exists(conn, "audit_log"):
        count = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE item_type IN ('file','folder') "
            "AND status='SUCCESS' AND timestamp>=? AND timestamp<?", (start_s, end_s)).fetchone()[0]
        return int(count), "successful file/folder ledger rows"
    if phase == "mail" and _table_exists(conn, "audit_log"):
        count = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE item_type='message' AND status='SUCCESS' "
            "AND timestamp>=? AND timestamp<?", (start_s, end_s)).fetchone()[0]
        return int(count), "successful message ledger rows"
    if phase == "dms_import":
        points = [r for r in dms_history if start <= r["at"] < end]
        if len(points) >= 2:
            count = sum(max(0, b["imported"] - a["imported"]) for a, b in zip(points, points[1:]))
            return count, "DMS Emails imported delta"
        return None, "DMS Emails imported delta (needs two console readings)"
    if phase == "dms_check":
        counts = [r["dms_check_items"] for r in logs if r.get("dms_check_items") is not None
                  and r.get("dms_check_end") is not None and start <= r["dms_check_end"] < end]
        return (max(counts), "messages checked") if counts else (None, "messages checked")
    if phase == "verify" and _table_exists(conn, "user_verification"):
        count = conn.execute(
            "SELECT COALESCE(SUM(checked),0) FROM user_verification "
            "WHERE verified_at>=? AND verified_at<?", (start_s, end_s)).fetchone()[0]
        if count:
            return int(count), "verification item checks"
    if phase == "tally" and _table_exists(conn, "user_tally"):
        count = conn.execute(
            "SELECT COUNT(*) FROM user_tally WHERE recorded_at>=? AND recorded_at<?",
            (start_s, end_s)).fetchone()[0]
        if count:
            return int(count), "users tallied"
    if phase == "tally":
        counts = [r["tally_users"] for r in logs if r.get("tally_users") is not None
                  and r.get("tally_at") is not None and start <= r["tally_at"] < end]
        if counts:
            return max(counts), "users tallied (run log)"
    if phase == "verify":
        counts = [r["verify_items"] for r in logs if r.get("verify_items") is not None
                  and r.get("verify_at") is not None and start <= r["verify_at"] < end]
        if counts:
            return max(counts), "items checked (run log)"
    if phase == "seeding":
        totals = [sum(r.get("seed_totals", {}).values()) for r in logs
                  if r.get("seed_totals") and r.get("seed_end") is not None
                  and start <= r["seed_end"] <= end + 2]
        if totals:
            return max(totals), "seeded files/folders/messages/events/contacts/tasks"
    return None, "items (not recorded for this phase window)"


def _sampler_stats(samples: list[dict], window: dict) -> dict:
    start, end = window["start"], window["end"]
    selected = [r for r in samples if start <= r["_epoch"] < end]
    cpu_values, rx_rates, tx_rates, total_rates = [], [], [], []
    for i, row in enumerate(selected):
        cpu = row.get("cpu") or {}
        value = cpu.get("busy_pct", row.get("cpu_pct"))
        if isinstance(value, (int, float)):
            cpu_values.append(float(value))
        network = row.get("network") or {}
        if isinstance(network.get("rx_bytes_per_sec"), (int, float)):
            rx_rates.append(float(network["rx_bytes_per_sec"]))
        if isinstance(network.get("tx_bytes_per_sec"), (int, float)):
            tx_rates.append(float(network["tx_bytes_per_sec"]))
        if isinstance(network.get("bytes_per_sec"), (int, float)):
            total_rates.append(float(network["bytes_per_sec"]))
        if isinstance(network.get("bytes_per_sec"), (int, float)):
            continue
        if i > 0 and not (isinstance(network.get("tx_bytes_per_sec"), (int, float))
                          or isinstance(network.get("rx_bytes_per_sec"), (int, float))):
            prior = selected[i - 1]
            pn = prior.get("network") or {}
            dt = row["_epoch"] - prior["_epoch"]
            if (dt > 0 and isinstance(pn.get("tx_bytes"), (int, float))
                    and isinstance(network.get("tx_bytes"), (int, float))
                    and network["tx_bytes"] >= pn["tx_bytes"]):
                tx_rates.append((network["tx_bytes"] - pn["tx_bytes"]) / dt)
            if (dt > 0 and isinstance(pn.get("rx_bytes"), (int, float))
                    and isinstance(network.get("rx_bytes"), (int, float))
                    and network["rx_bytes"] >= pn["rx_bytes"]):
                rx_rates.append((network["rx_bytes"] - pn["rx_bytes"]) / dt)
    tx = statistics.mean(tx_rates) if tx_rates else None
    rx = statistics.mean(rx_rates) if rx_rates else None
    total = statistics.mean(total_rates) if total_rates else (
        tx + rx if tx is not None and rx is not None else tx if tx is not None else rx)
    return {"cpuSharePct": statistics.mean(cpu_values) if cpu_values else None,
            "bytesPerSecond": total, "txBytesPerSecond": tx, "rxBytesPerSecond": rx,
            "samplerSamples": len(selected)}


def _verdict(stats: dict, line_bytes_per_sec: float | None) -> tuple[str, str]:
    if ((stats.get("rejections") is not None and stats["rejections"] > 0)
            or stats.get("quotaBackoffs", 0) > 0):
        return GOOGLE_QUOTA, "Google rejected limiter-paced requests during this phase."
    tx = stats.get("txBytesPerSecond")
    if line_bytes_per_sec and tx is not None and tx / line_bytes_per_sec >= LINE_SATURATION:
        return LINE, f"Transmit rate used {tx / line_bytes_per_sec:.0%} of the configured uplink."
    cpu = stats.get("cpuSharePct")
    idle = stats.get("idleWorkersShare")
    if cpu is not None and idle is not None and cpu >= CPU_BUSY_PCT and idle <= MAX_IDLE_FOR_CPU:
        return CPU, f"Host CPU averaged {cpu:.0f}% while at least {1 - idle:.0%} of worker slots were busy."
    if (stats.get("rejections") == 0 and tx is not None and line_bytes_per_sec is not None
            and line_bytes_per_sec > 0 and cpu is not None and idle is not None
            and tx / line_bytes_per_sec < LINE_SATURATION
            and cpu < CPU_BUSY_PCT and idle > MAX_IDLE_FOR_CPU):
        return NOTHING, "Workers were idle, Google sent no limiter pushback, and the measured uplink had headroom; this phase is underfed by Bitport."
    missing = []
    if stats.get("rejections") is None:
        missing.append("phase-local limiter rejection delta")
    if tx is None or line_bytes_per_sec is None:
        missing.append("sampled transmit rate/uplink capacity")
    if cpu is None:
        missing.append("sampled CPU")
    if idle is None:
        missing.append("busy/idle worker slots")
    if missing:
        return UNKNOWN, "Insufficient phase-local evidence: " + ", ".join(missing) + "."
    return UNKNOWN, "The available signals do not meet a binding-limit threshold."


def _parse_windows(values: list[str] | None) -> dict[str, tuple[float, float]]:
    out = {}
    for value in values or []:
        try:
            name, span = value.split("=", 1)
            start_s, end_s = span.split(",", 1)
            phases = _canonical_phase(name)
            if name in PHASES:
                phases = [name]
            if not phases:
                raise ValueError(f"unknown phase in {value!r}")
            start, end = _epoch(start_s), _epoch(end_s)
            if start is None or end is None or end <= start:
                raise ValueError(f"phase window needs valid increasing UTC times: {value!r}")
        except ValueError as exc:
            raise ValueError(f"expected --phase-window PHASE=START,END: {exc}") from exc
        for phase in phases:
            out[phase] = (start, end)
    return out


def analyze(account_id: int, *, run_id: int | None = None, ledger: str | None = None,
            sample_path: str | None = None, log_paths: list[str] | None = None,
            phase_windows: list[str] | None = None,
            line_bytes_per_sec: float | None = DEFAULT_LINE_BYTES_PER_SEC,
            live_metrics: dict | None = None, kind: str = "migrate",
            run_key: str | None = None) -> dict:
    """Return phase rates and binding verdicts for one account run.

    ``live_metrics`` is optional for an in-process caller that can access
    ``metrics.METRICS.snapshot()``; the standalone CLI reads persisted samples
    using the same payload shape as ``db.latest_metrics``.
    """
    db_path = ledger or os.path.join(HERE, "data", "accounts", str(account_id), "migration.db")
    conn = _connect_readonly(db_path)
    try:
        if kind not in ("migrate", "seed"):
            raise ValueError("kind must be 'migrate' or 'seed'")
        summaries = _read_run_summaries(conn)
        if kind == "seed":
            if run_id is not None:
                raise ValueError("--run selects a migration run; use --run-key for a seed")
            seed_summaries = [s for s in summaries if s.get("kind") == "seed"
                              and (run_key is None or s.get("runKey") == run_key)]
            if not seed_summaries:
                raise ValueError("no saved seed metrics match this account/run; use --kind seed after a seed heartbeat")
            seed = seed_summaries[0]
            run = {"id": seed.get("runKey"), "startedAt": seed.get("startedAt"),
                   "pid": None, "phases": [], "kind": "seed"}
            summaries = [seed]
        else:
            if run_key:
                raise ValueError("--run-key is only valid with --kind seed")
            run = _load_run(conn, run_id)
        metric_rows = _read_metric_rows(conn)
        ceilings = _read_ceiling_rows(conn)
        logs_to_read = list(dict.fromkeys([*(log_paths or []), *_auto_log_paths(account_id)]))
        logs = [_read_log(p) for p in logs_to_read]
        explicit = _parse_windows(phase_windows)
        windows = _phase_windows(run, explicit, logs, summaries)
        # Current process metrics are useful only inside the caller's process;
        # tag them with now and enrich with the live Drive limiter state when
        # that module is already available. Importing metrics in the CLI alone
        # would create an empty reservoir, so it is deliberately not done here.
        if live_metrics and isinstance(live_metrics, dict):
            current = dict(live_metrics)
            current["recordedAt"] = _iso(time.time())
            current["_epoch"] = time.time()
            metric_rows.append(current)
        sampler_rows = _read_sampler(sample_path)
        dms_path = os.path.join(HERE, "logs", "dms", str(account_id), "metrics.history.jsonl")
        dms_history = _read_dms_history(dms_path)

        phases = {}
        for phase in PHASES:
            window = windows.get(phase)
            if not window:
                phases[phase] = {"name": PHASE_LABELS[phase], "items": None,
                                 "itemsPerMinute": None, "limiter": {}, "rejections": None,
                                 "cpuSharePct": None, "bytesPerSecond": None,
                                 "idleWorkersShare": None, "verdict": UNKNOWN,
                                 "reason": "No timestamped phase window is recorded; supply --phase-window.",
                                 "window": None}
                continue
            items, unit = _phase_counts(conn, phase, window, logs, dms_history)
            elapsed = max(0.0, window["end"] - window["start"])
            perf = _phase_metrics(metric_rows, window, ceilings)
            host = _sampler_stats(sampler_rows, window)
            stats = {**perf, **host}
            verdict, reason = _verdict(stats, line_bytes_per_sec)
            phases[phase] = {
                "name": PHASE_LABELS[phase], "items": items, "itemUnit": unit,
                "elapsedSeconds": elapsed,
                "itemsPerMinute": (items / (elapsed / 60)) if items is not None and elapsed > 0 else None,
                "limiter": perf["limiters"], "rejections": perf["rejections"],
                "cpuSharePct": host["cpuSharePct"], "bytesPerSecond": host["bytesPerSecond"],
                "txBytesPerSecond": host["txBytesPerSecond"],
                "rxBytesPerSecond": host["rxBytesPerSecond"],
                "idleWorkersShare": perf["idleWorkersShare"],
                "metricSamples": perf["metricsSamples"], "samplerSamples": host["samplerSamples"],
                "window": {"startedAt": _iso(window["start"]), "endedAt": _iso(window["end"]),
                           "source": window["source"], "recordedName": window.get("recordedName"),
                           "state": window.get("state")},
                "verdict": verdict, "reason": reason,
            }
        return {"accountId": account_id, "ledger": os.path.abspath(db_path),
                "run": run, "lineBytesPerSecond": line_bytes_per_sec,
                "dataSources": {"runMetrics": len(metric_rows), "sampler": len(sampler_rows),
                                "logs": logs_to_read, "dmsHistory": dms_path if dms_history else None,
                                "rateLimiterCeilings": ceilings},
                "phases": phases}
    finally:
        conn.close()


def historical_baselines() -> dict:
    """The old 45-user plan figures, kept as rates without invented limit data."""
    data = {
        "drive": (38_513, 103 * 60, "files"),
        "mail": (None, 34 * 60, "messages; baseline did not publish a count"),
        "dms_import": (141_977, 23 * 60 * 60, "messages (23 h approximate)"),
        "dms_check": (830, 60, "messages (reported 830/min)"),
    }
    return {phase: {"items": n, "elapsedSeconds": seconds, "itemUnit": unit,
                    "itemsPerMinute": n / (seconds / 60) if n is not None else None,
                    "verdict": UNKNOWN,
                    "reason": "Historical baseline has no phase-local CPU, network, limiter, or worker-occupancy series."}
            for phase, (n, seconds, unit) in data.items()}


def render(report: dict) -> str:
    lines = [f"Account {report['accountId']} — run {report['run'].get('id') or '(no run record)'}",
             "Phase        items/min  limiter rate/ceiling/s  rejects  CPU%  TX B/s  idle%  verdict"]
    for phase in PHASES:
        row = report["phases"][phase]
        limiters = row.get("limiter") or {}
        pair = next(iter(limiters.values()), {})
        rate, ceiling = pair.get("ratePerSec"), pair.get("ceilingPerSec")
        lim = (f"{rate if rate is not None else '?'} / {ceiling if ceiling is not None else '?'}"
               if limiters else "?")
        cells = (row.get("itemsPerMinute"), row.get("rejections"), row.get("cpuSharePct"),
                 row.get("txBytesPerSecond"), row.get("idleWorkersShare"))
        n, rejected, cpu, tx, idle = cells
        fmt = lambda x, places=1: "?" if x is None else f"{x:.{places}f}"
        lines.append(f"{row['name']:<12} {fmt(n):>8}  {lim:<22} {fmt(rejected,0):>7} "
                     f"{fmt(cpu):>5} {fmt(tx):>7} {fmt(idle * 100 if idle is not None else None):>6}  {row['verdict']}")
        if row.get("reason"):
            lines.append(f"  {row['reason']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", type=int, required=True)
    parser.add_argument("--run", type=int, help="run_records.run; defaults to newest")
    parser.add_argument("--kind", choices=("migrate", "seed"), default="migrate",
                        help="analyze a migration phase record or the latest saved seed run")
    parser.add_argument("--run-key", help="select an exact seed summary key, e.g. seed:PID:STARTED_AT")
    parser.add_argument("--ledger", help="explicit read-only ledger path (defaults to this account's ledger)")
    parser.add_argument("--sample", help="sampler.py JSONL file")
    parser.add_argument("--log", action="append", default=[], help="extra run log path; repeatable")
    parser.add_argument("--phase-window", action="append", default=[], metavar="PHASE=START,END",
                        help="timestamped UTC window for a phase missing from run_phases; repeatable")
    parser.add_argument("--line-bytes-per-sec", type=float, default=DEFAULT_LINE_BYTES_PER_SEC,
                        help="measured uplink capacity; default is the plan's 0.9 MB/s baseline")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = parser.parse_args(argv)
    try:
        report = analyze(args.account_id, run_id=args.run, ledger=args.ledger,
                         sample_path=args.sample, log_paths=args.log,
                         phase_windows=args.phase_window,
                         line_bytes_per_sec=args.line_bytes_per_sec,
                         kind=args.kind, run_key=args.run_key)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"phase_binding: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
