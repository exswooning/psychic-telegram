#!/usr/bin/env python3
"""Back up every ledger, consistently, without stopping anything.

Why this exists
---------------
There were no backups. Not "incomplete backups" -- none. The only record
of what a migration moved lived in one SQLite file on one VPS:

    data/accounts/7/migration.db    5.7 GB
    data/accounts/66/migration.db   0.4 GB

That file is the thing that makes a re-run idempotent and lets the tool say
which items failed. Losing it does not lose "some logs", it loses the
ability to finish or explain a migration that is already half done.

VACUUM INTO, not a file copy
----------------------------
Copying a live SQLite file gives you a torn database: the WAL holds
committed pages the main file does not have yet, and a naive `cp` during a
migration captures neither consistently. VACUUM INTO takes the same
snapshot semantics as a read transaction -- it is safe while writers are
running -- and writes a defragmented copy, which on a ledger this churned
is usually a good deal smaller than the original.

The copy is then integrity-checked. A backup nobody has verified is a
belief, not a backup, and the moment you need it is the worst moment to
discover it was truncated.

Space
-----
Refuses rather than fills the disk. The box was 67% full when this was
written, with 13 GB free against 6.1 GB of ledgers, so a careless rotation
count is how backups take out the thing they exist to protect.

    python3 backup_db.py                       # all ledgers, keep 3
    python3 backup_db.py --dest /mnt/backups --keep 7
    python3 backup_db.py --list                # what is on file
    python3 backup_db.py --verify <file>       # check one
"""
from __future__ import annotations

import argparse
import gzip
import os
import shutil
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DEST = os.path.join(HERE, "backups")
# Headroom multiplier. VACUUM INTO needs room for the copy while the
# original still exists, and a disk with literally zero slack breaks the
# migration this is protecting.
SPACE_FACTOR = 1.3


def ledgers(root: str | None = None) -> list[tuple[str, str]]:
    """(label, path) for the control plane and every account ledger.

    root resolved at call time, not bound as a default: a module-level
    default captures HERE at import, so the install directory could never
    be redirected -- not for a test, and not for a second checkout on the
    same box.
    """
    root = root or HERE
    out: list[tuple[str, str]] = []
    cp = os.path.join(root, "migration.db")
    if os.path.isfile(cp):
        out.append(("control-plane", cp))
    accounts = os.path.join(root, "data", "accounts")
    if os.path.isdir(accounts):
        for name in sorted(os.listdir(accounts)):
            p = os.path.join(accounts, name, "migration.db")
            if os.path.isfile(p):
                out.append((f"account-{name}", p))
    return out


def free_bytes(path: str) -> int:
    return shutil.disk_usage(path).free


def verify(path: str) -> tuple[bool, str]:
    """integrity_check on a finished copy."""
    opener = gzip.open if path.endswith(".gz") else open
    if path.endswith(".gz"):
        # Cannot check a compressed file in place; decompress to a temp and
        # check that, so --verify means the same thing either way.
        tmp = path[:-3] + ".verify.tmp"
        try:
            with gzip.open(path, "rb") as src, open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst)
            ok, msg = verify(tmp)
            return ok, msg
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    del opener
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return False, f"unreadable: {exc}"
    return (row[0] == "ok"), str(row[0])


def backup_one(label: str, src: str, dest_dir: str, compress: bool = False,
               stamp: str | None = None) -> dict:
    """One consistent, verified copy. Never raises -- one bad ledger must
    not stop the others being saved."""
    out = {"label": label, "src": src, "ok": False, "detail": "",
           "bytes": 0, "seconds": 0.0}
    started = time.time()
    size = os.path.getsize(src)
    need = int(size * SPACE_FACTOR)
    have = free_bytes(dest_dir)
    if have < need:
        out["detail"] = (f"refusing: needs ~{need/1e9:.1f} GB free "
                         f"({size/1e9:.1f} GB ledger x{SPACE_FACTOR}), "
                         f"{have/1e9:.1f} GB available")
        return out

    stamp = stamp or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    target = os.path.join(dest_dir, f"{label}-{stamp}.db")
    try:
        conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=60)
        try:
            # Safe while writers run: same snapshot semantics as a read
            # transaction, unlike copying the file underneath them.
            conn.execute("VACUUM INTO ?", (target,))
        finally:
            conn.close()
    except sqlite3.Error as exc:
        out["detail"] = f"VACUUM INTO failed: {str(exc)[:160]}"
        if os.path.exists(target):
            os.remove(target)
        return out

    ok, detail = verify(target)
    if not ok:
        out["detail"] = f"copy failed integrity_check: {detail}"
        os.remove(target)
        return out

    if compress:
        gz = target + ".gz"
        with open(target, "rb") as s, gzip.open(gz, "wb", compresslevel=6) as d:
            shutil.copyfileobj(s, d, length=8 << 20)
        os.remove(target)
        target = gz

    out.update(ok=True, bytes=os.path.getsize(target), seconds=round(time.time() - started, 1),
               detail=f"{target} ({os.path.getsize(target)/1e9:.2f} GB, "
                      f"source {size/1e9:.2f} GB)")
    return out


def rotate(label: str, dest_dir: str, keep: int) -> list[str]:
    """Drop the oldest copies of ONE ledger past `keep`.

    Per label, not per directory: a global count would let a busy tenant's
    backups evict the control plane's, which is the one that knows the
    others exist.
    """
    if keep < 1:
        return []
    mine = sorted(f for f in os.listdir(dest_dir)
                  if f.startswith(f"{label}-") and (f.endswith(".db") or f.endswith(".db.gz")))
    dropped = []
    for f in mine[:-keep] if len(mine) > keep else []:
        os.remove(os.path.join(dest_dir, f))
        dropped.append(f)
    return dropped


def run(dest_dir: str, keep: int, compress: bool) -> dict:
    os.makedirs(dest_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    results = [backup_one(label, src, dest_dir, compress, stamp)
               for label, src in ledgers()]
    for r in results:
        if r["ok"]:
            r["rotated"] = rotate(r["label"], dest_dir, keep)
    return {"ok": all(r["ok"] for r in results) if results else False,
            "results": results, "dest": dest_dir}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dest", default=os.getenv("BACKUP_DIR", DEFAULT_DEST))
    ap.add_argument("--keep", type=int, default=int(os.getenv("BACKUP_KEEP", "3")))
    ap.add_argument("--gzip", action="store_true",
                    help="compress the copy; slower, and much smaller on a "
                         "ledger full of repeated ids")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--verify", metavar="FILE")
    args = ap.parse_args(argv)

    if args.verify:
        ok, detail = verify(args.verify)
        print(f"{'OK  ' if ok else 'BAD '} {args.verify}: {detail}")
        return 0 if ok else 1

    if args.list:
        if not os.path.isdir(args.dest):
            print(f"no backups at {args.dest}")
            return 0
        for f in sorted(os.listdir(args.dest)):
            p = os.path.join(args.dest, f)
            print(f"  {os.path.getsize(p)/1e9:8.2f} GB  {f}")
        return 0

    found = ledgers()
    if not found:
        print("no ledgers found to back up", file=sys.stderr)
        return 1
    print(f"Backing up {len(found)} ledger(s) to {args.dest}, keeping {args.keep}")
    out = run(args.dest, args.keep, args.gzip)
    for r in out["results"]:
        print(f"  {'OK  ' if r['ok'] else 'FAIL'} {r['label']:16} {r['detail']}")
        for f in r.get("rotated", []):
            print(f"       rotated out {f}")
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
