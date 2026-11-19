"""Shape statistics of a real organisation's migration ledger (fableplan2 P4.1): numbers and file types only.

Reads one account's ledger read-only (`discovery`, `id_mapping`, `mail_link_messages`, `label_map`) and writes a JSON
profile the seeder can draw a synthetic organisation from. Nothing that names a person or an item leaves the ledger: no
address, subject, file name, label name or id. A user is a row of numbers with no key; rows are ordered by file count, so
the file says how the organisation was shaped, not who was in it.

    python data-generator/profile_extract.py --db data/accounts/3/migration.db --out data-generator/profiles/org45.json
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import statistics
import sys
from pathlib import Path

DISCOVERY = ("file_count", "folder_count", "native_count", "shortcut_count", "max_depth", "total_bytes",
             "largest_bytes", "messages_total", "threads_total", "user_label_count")
# id_mapping types that are counts of something a user holds (never an id)
MAPPED = ("event", "contact", "contact_group", "task", "task_list", "calendar", "draft", "comment", "thread")


def _pct(values, q):
    s = sorted(values)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))] if s else 0


def _summary(values):
    return {"p50": _pct(values, 0.5), "p90": _pct(values, 0.9), "max": max(values) if values else 0, "total": sum(values)}


def extract(db_path: str) -> dict:
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    users = [r[0] for r in c.execute("SELECT source_email FROM identity_map WHERE entity_type='user'")] or \
            [r[0] for r in c.execute("SELECT DISTINCT source_user FROM discovery")]
    latest = {}
    for r in c.execute("SELECT * FROM discovery ORDER BY scanned_at"):
        latest[r["source_user"]] = r
    link_counts = {u: n for u, n in c.execute("SELECT source_user, COUNT(*) FROM mail_link_messages GROUP BY source_user")}
    link_files = []
    for (ids,) in c.execute("SELECT file_ids FROM mail_link_messages"):
        link_files.append(len(re.findall(r"[A-Za-z0-9_-]{25,}", ids or "")))      # a delimited list of Drive ids
    labels = {u: n for u, n in c.execute("SELECT source_user, COUNT(*) FROM label_map GROUP BY source_user")}
    mapped = {}
    for u, t, n in c.execute("SELECT source_user, type, COUNT(*) FROM id_mapping GROUP BY source_user, type"):
        mapped.setdefault(u, {})[t] = n
    mimes: dict = {}
    rows = []
    for u in users:
        d = latest.get(u)
        if d is None:
            continue
        row = {k: int(d[k] or 0) for k in DISCOVERY}
        row["link_mail"] = link_counts.get(u, 0)
        row["labels_mapped"] = labels.get(u, 0)
        for t in MAPPED:
            row[t] = mapped.get(u, {}).get(t, 0)
        try:
            hist = json.loads(d["mime_histogram"] or "{}")
        except ValueError:
            hist = {}
        row["mime"] = {m: int(n) for m, n in hist.items() if isinstance(n, (int, float))}
        for m, n in row["mime"].items():
            mimes[m] = mimes.get(m, 0) + n
        rows.append(row)
    rows.sort(key=lambda r: -r["file_count"])
    metrics = [k for k in rows[0] if k != "mime"] if rows else []
    return {
        "users": len(rows),
        "per_user": rows,            # one row of numbers per user, largest Drive first; no key, no name
        "summary": {k: _summary([r[k] for r in rows]) for k in metrics},
        "mime_totals": dict(sorted(mimes.items(), key=lambda kv: -kv[1])),
        "link_mail": {"messages": len(link_files), "files_per_message": {"p50": _pct(link_files, 0.5), "p90": _pct(link_files, 0.9),
                                                                          "max": max(link_files or [0]),
                                                                          "mean": round(statistics.fmean(link_files), 2) if link_files else 0}},
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    profile = extract(a.db)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(profile, indent=1, sort_keys=True))
    s = profile["summary"]
    print(f"{profile['users']} users; files p50 {s['file_count']['p50']} p90 {s['file_count']['p90']} max {s['file_count']['max']}; "
          f"messages total {s['messages_total']['total']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
