#!/usr/bin/env python3
"""Watch every log on the box once a second and print each NEW kind of error once.

Costs no model tokens: it runs on the box, reads only what each file gained since the last
second, and prints a line only for an error whose shape (numbers, ids, addresses stripped) it
has not printed before. A repeat is counted, and the counts go out once a minute, so a burst of
4,000 identical 403s is one line plus a tally -- not 4,000 notifications. Run it over SSH under a
Monitor and every printed line reaches Claude within about a second of being written.

    python3 monitor/logwatch.py [--root /opt/bitport/logs] [--every 1] [--from-start]
"""
from __future__ import annotations

import argparse
import collections
import os
import re
import sys
import time

TERMS = re.compile(
    r"\b(40[0-9]|429|5\d\d)\b.*(error|denied|forbidden|refused|exceeded|invalid|failed|unavailable)"
    r"|HttpError|insufficient|PermanentAPIError|TransportExhausted|QuotaExhausted"
    r"|Traceback|\bERROR\b|\bCRITICAL\b|\bFAILED\b|Killed|Out of memory|MemoryError|NOT READY"
    r"|MOVE REFUSED|insufficient\w*Permissions|userRateLimitExceeded|rateLimitExceeded|quotaExceeded"
    r"|NOT ACCEPTED|failed \(exit|exited [1-9]|HASH-MISMATCH|COMPILE-FAIL|database is locked",
    re.IGNORECASE)
QUIET = re.compile(r"ignored an error|0 failed|failed': 0|Emails failed': 0|\"failed\": 0", re.IGNORECASE)
SECRET = re.compile(r"(password|secret|token|bp_session)\S*", re.IGNORECASE)
SKIP_DIRS = {"reports", "quick"}


def shape(line: str) -> str:
    s = line.lower()
    s = re.sub(r"\S+@\S+", "<addr>", s)
    s = re.sub(r"\b(?=[\w-]*\d)[\w-]{12,}\b", "<id>", s)      # Drive/Gmail ids: long, with a digit
    s = re.sub(r"\d+", "#", s)
    s = re.sub(r"^[#:\-t ,.z]+", "", s)          # a leading timestamp
    return s[:140]


def logs(root: str):
    for d, subdirs, files in os.walk(root):
        subdirs[:] = [x for x in subdirs if x not in SKIP_DIRS]
        for f in files:
            if f.endswith(".log"):
                yield os.path.join(d, f)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/opt/bitport/logs")
    ap.add_argument("--every", type=float, default=1.0)
    ap.add_argument("--from-start", action="store_true", help="read existing content too (default: new lines only)")
    args = ap.parse_args(argv)
    offsets: dict[str, int] = {}
    if not args.from_start:
        for p in logs(args.root):
            try:
                offsets[p] = os.path.getsize(p)
            except OSError:
                pass
    seen: dict[str, int] = {}
    repeats: collections.Counter = collections.Counter()
    last_tally = time.time()
    print(f"logwatch: watching {len(offsets)} logs under {args.root} every {args.every}s", flush=True)
    while True:
        for p in logs(args.root):
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            start = offsets.get(p, 0)
            if size < start:          # truncated or rotated
                start = 0
            if size == start:
                offsets[p] = size
                continue
            with open(p, "rb") as fh:
                fh.seek(start)
                chunk = fh.read(min(size - start, 4 << 20))
            offsets[p] = start + len(chunk)
            for raw in chunk.decode("utf-8", "replace").splitlines():
                if not TERMS.search(raw) or QUIET.search(raw):
                    continue
                key = shape(raw)
                if key in seen:
                    repeats[key] += 1
                    continue
                seen[key] = 1
                where = os.path.relpath(p, args.root)
                print(f"NEW {where}: {SECRET.sub('<redacted>', raw.strip())[:300]}", flush=True)
        if repeats and time.time() - last_tally >= 60:
            top = ", ".join(f"{n}x {k[:60]}" for k, n in repeats.most_common(3))
            print(f"REPEATS last minute: {sum(repeats.values())} ({top})", flush=True)
            repeats.clear()
            last_tally = time.time()
        time.sleep(args.every)


if __name__ == "__main__":
    sys.exit(main())
