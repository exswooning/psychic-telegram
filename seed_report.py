"""
seed_report.py -- what a seed run did, read back from its transcript.

A migration leaves a ledger. A seed leaves only what it printed, so the facts
for its report are parsed from the transcript: users started and finished,
services that failed inside a "done" line, warnings by kind, and -- for a
storage fill -- how much was uploaded against how much was planned.

This is the Python twin of migration-webui/src/utils/seedLog.ts, which does the
same reading for the dashboard. They parse the same lines, so a change to what
the seeder prints has to reach both.
"""
from __future__ import annotations

import re

USER_LINE = re.compile(r"^\s*\[([^\]]+)\]\s+(starting|done|top-up)\b\s*(.*)$")
WARNING_LINE = re.compile(r"^\s*!\s+(\S+)\b(.*)$")
HTTP_CODE = re.compile(r"HTTP\s+(\d{3})\s*(?:\(([^)]+)\))?")
# print() is not atomic and the seeder ran many threads through it, so older
# transcripts hold records spliced into one line; split them back apart.
RECORD_START = re.compile(r"(?=\[[^\]]+\]\s+(?:starting|done|top-up)\b)|(?=!\s+\S+\s)")
COUNT_LABELS = ("chat messages", "secondary calendars", "filler file", "files", "folders", "comments",
                "messages", "drafts", "events", "spaces", "contacts", "tasks")
COUNT_RE = re.compile(r"(\d[\d,]*)\s+(" + "|".join(COUNT_LABELS) + ")")
STORAGE_RE = re.compile(r"([\d.]+)\s*GB\s*->\s*([\d.]+)\s*GB")
FILL_BEAT = re.compile(r"still topping up:.*?after\s+(\d+)m(\d+)s.*?--\s*([\d,.]+)\s*GB uploaded of\s*([\d,.]+)\s*GB planned")
ANY_BEAT = re.compile(r"still (?:seeding|topping up):.*?after\s+(\d+)m(\d+)s")


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def split_records(line: str) -> list[str]:
    parts = RECORD_START.split(line)
    return [line] if len(parts) < 2 else [p.strip() for p in parts if p.strip()]


def parse(lines: list[str]) -> dict:
    users: dict[str, dict] = {}
    warnings: dict[tuple, dict] = {}
    head: dict = {}
    fill: list[tuple[int, float, float]] = []
    elapsed = 0
    for physical in lines:
        for line in split_records(str(physical)):
            m = USER_LINE.match(line)
            if m:
                email, verb, rest = m.group(1), m.group(2), m.group(3).strip()
                if verb == "starting":
                    users[email] = {"status": "running", "counts": {}, "failedServices": [], "storage": None}
                else:
                    counts: dict[str, int] = {}
                    for c in COUNT_RE.finditer(rest):
                        counts.setdefault(c.group(2), int(_num(c.group(1))))
                    st = STORAGE_RE.search(rest)
                    users[email] = {
                        "status": "done" if verb == "done" else "topped-up", "counts": counts,
                        "failedServices": re.findall(r"(\w+) failed \(", rest),
                        "storage": (float(st.group(1)), float(st.group(2))) if st else None,
                    }
                continue
            w = WARNING_LINE.match(line)
            if w:
                kind = w.group(1)
                code = HTTP_CODE.search(line)
                label = f"HTTP {code.group(1)}{f' ({code.group(2)})' if code.group(2) else ''}" if code else ""
                g = warnings.setdefault((kind, label), {"kind": kind, "code": label, "count": 0,
                                                        "sample": line.strip()[:200], "users": set()})
                g["count"] += 1
                who = re.search(r"([\w.+-]+@[\w.-]+)", line)
                if who:
                    g["users"].add(who.group(1))
                continue
            b = FILL_BEAT.search(line)
            if b:
                fill.append((int(b.group(1)) * 60 + int(b.group(2)), _num(b.group(3)), _num(b.group(4))))
            beat = ANY_BEAT.search(line)
            if beat:
                elapsed = max(elapsed, int(beat.group(1)) * 60 + int(beat.group(2)))
            if (h := re.search(r"Seeding\s+(\d+)\s+users?\s+in\s+(\S+)\s+at\s+scale\s+'([^']+)'", line)):
                head.update(mode="seed", totalUsers=int(h.group(1)), domain=h.group(2), scale=h.group(3))
            elif (h := re.search(r"Topping up storage for\s+(\d+)\s+user\(s\)\s+toward\s+(.*?)\s*\.{3}", line)):
                head.update(mode="fill", totalUsers=int(h.group(1)), target=h.group(2))
            elif (h := re.search(r"Workers:\s*(\d+)", line)):
                head["workers"] = int(h.group(1))
            elif "totalUsers" not in head and (h := re.search(r"Found\s+(\d+)\s+existing\s+user", line)):
                head["totalUsers"] = int(h.group(1))
    return {"users": users, "warnings": list(warnings.values()), "head": head, "fill": fill,
            "elapsedSec": elapsed}


def facts(lines: list[str], *, ended: bool) -> tuple[dict, list[dict]]:
    """(seed facts, failure families) for the report. `ended` says whether the
    run's outcome is known: an in-flight run has users "never finished" only in
    the sense that it has not finished yet, and must not be judged for it."""
    p = parse(lines)
    users, head = p["users"], p["head"]
    finished = [u for u in users.values() if u["status"] != "running"]
    started = len(users)
    total = head.get("totalUsers")
    denom = total or started or None
    added = sum(max(0.0, u["storage"][1] - u["storage"][0]) for u in finished if u["storage"])
    planned = p["fill"][-1][2] if p["fill"] else None
    uploaded = p["fill"][-1][1] if p["fill"] else None
    with_failed = sum(1 for u in finished if u["failedServices"])
    warn_total = sum(w["count"] for w in p["warnings"])
    services: dict[str, int] = {}
    for u in finished:
        for s in u["failedServices"]:
            services[s] = services.get(s, 0) + 1
    seed = {
        "mode": head.get("mode") or ("fill" if p["fill"] else None), "domain": head.get("domain"),
        "scale": head.get("scale"), "workers": head.get("workers"), "totalUsers": total,
        "started": started, "finished": len(finished),
        "finishedShare": (len(finished) / denom) if denom else None,
        # Only for a run that has ended. Mid-run, "not finished" means "not yet".
        "neverFinished": (max(0, (denom or 0) - len(finished)) if (ended and denom) else None),
        "failedServiceUsers": with_failed, "failedServices": services,
        "failedServiceShare": (with_failed / len(finished)) if finished else None,
        "warnings": warn_total, "warningsPerUser": (warn_total / len(finished)) if finished else None,
        "fillAddedGb": round(added, 2) if (finished and any(u["storage"] for u in finished)) else None,
        "fillPlannedGb": planned, "fillUploadedGb": uploaded,
        # Reached-ness only means something once the run is over.
        "fillReached": (min(1.0, (uploaded / planned)) if (ended and planned and uploaded is not None) else None),
        "elapsedSec": p["elapsedSec"] or None,
        "items": {k: sum(u["counts"].get(k, 0) for u in finished)
                  for k in {k for u in finished for k in u["counts"]} if k != "filler file"},
        "fillerFiles": sum(u["counts"].get("filler file", 0) for u in finished),
    }
    fam = [{"itemType": w["kind"], "message": f"{w['code']} {w['sample']}".strip(),
            "count": w["count"], "users": len(w["users"])}
           for w in sorted(p["warnings"], key=lambda w: -w["count"])[:15]]
    return seed, fam
