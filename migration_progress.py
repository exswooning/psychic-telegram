"""Read-only progress helpers. No live Google requests or invented completion totals."""
from __future__ import annotations

import json
from datetime import datetime


def reporting_process(samples):
    """Select the busiest fresh writer, not an idle parent's alternating flush.

    Percentiles cannot be summed or averaged across processes. State this
    process scope explicitly; ledger item totals remain account/run scoped.
    """
    if not samples:
        return None
    def at(s):
        try:
            return datetime.fromisoformat(str(s["recordedAt"]).replace("Z", "+00:00")).timestamp()
        except (ValueError, KeyError, TypeError):
            return None
    newest = at(samples[0])
    if newest is None or not samples[0].get("pid"):
        return samples[0]
    writers = {}
    for s in samples:
        t = at(s)
        if t is not None and 0 <= newest-t <= 35 and s.get("pid"):
            writers.setdefault(s["pid"], s)
    return max(writers.values(), key=lambda s: s.get("calls", 0), default=samples[0])


def drive_baseline_coverage(conn, users):
    """Exact baseline ID intersection, including folders/shortcuts.

    This is explicitly historical source-baseline coverage, not current source
    census or content/permission verification. Partial/unread snapshots are unknown.
    """
    if not users:
        return None
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='source_guard_snapshot'").fetchone():
        return None
    total = done = 0
    dates = []
    for user in users:
        row = conn.execute("SELECT taken_at,state FROM source_guard_snapshot WHERE source_user=?", (user,)).fetchone()
        if not row or not isinstance(row[0], str) or not row[0]:
            return None
        try:
            state = json.loads(row[1])
        except (ValueError, TypeError):
            return None
        if not isinstance(state, dict) or "drive" in (state.get("errors") or {}) or not isinstance(state.get("files"), dict):
            return None
        ids = set(state["files"])
        mapped = {r[0] for r in conn.execute(
            "SELECT source_id FROM id_mapping WHERE source_user=? AND type IN ('file','folder','shortcut')", (user,))}
        total += len(ids)
        done += len(ids & mapped)
        dates.append(row[0])
    return {"done": done, "total": total, "takenAt": min(dates),
            "scope": "saved source baseline IDs; copy mappings only, not 1:1 verification"}
