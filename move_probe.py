"""
move_probe.py
=============
Does a file leave the source for the target by being MOVED, not copied?

A move keeps the file's id, revisions and comments and moves no bytes, so the 750 GB a day never
applies. Whether Google allows it between two organisations depends on the source's "Distributing
content outside of the organisation" setting AND its edition (Enterprise and Education only), and
there is no read for either -- so this tries it, on a one-byte file, and reports where it stopped.

Two routes are tried, each on its own throwaway files and drives, all deleted after:

  direct        the source admin's file, moved into a shared drive the TARGET made
                (the route a move run uses: drive_engine._sync_move)
  source-drive  the file put INTO a shared drive the SOURCE made, the target admin invited to it,
                and the TARGET admin takes it out into their own Drive

Each result names the step that was refused and Google's own words. `ok` is True, False, or None
when it could not be tried at all (a source that is not a declared sandbox is never written to).
A move run is offered only on a direct pass: that is the route the engine has.

    python move_probe.py --account-id 3
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import uuid

log = logging.getLogger(__name__)

ROUTES = ("direct", "source-drive")
NEEDS = ("Drive and Docs > Sharing settings > Distributing content outside of the organisation set to "
         "Anyone or to its own users, AND an edition that lets content move to another organisation "
         "(Google lists it for Enterprise and Education)")


class Step(Exception):
    """A step Google refused: which one, and what it said."""

    def __init__(self, step: str, exc: Exception):
        self.step = step
        self.status = getattr(getattr(exc, "resp", None), "status", None)
        self.reason = str(exc)[:240]
        super().__init__(f"{step}: {self.reason}")


def _do(step: str, fn, tries: int = 1, wait: float = 3.0):
    for attempt in range(tries):
        try:
            return fn()
        except Exception as exc:      # noqa: BLE001 - classified into a Step
            if attempt + 1 >= tries:
                raise Step(step, exc) from exc
            time.sleep(wait)


def _cleanup(label: str, fn, tries: int = 3, wait: float = 3.0) -> None:
    for attempt in range(tries):
        try:
            fn()
            return
        except Exception as exc:      # noqa: BLE001 - never fail a probe over its own tidying
            if attempt + 1 >= tries:
                log.warning("move probe: could not remove %s: %s", label, exc)
            else:
                time.sleep(wait)


def _result(route: str, ok: bool | None, step: str, detail: str, status=None) -> dict:
    return {"route": route, "ok": ok, "step": step, "detail": detail, "status": status,
            "testedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def _refused(route: str, exc: Step, who: str) -> dict:
    hint = f" -- {who} needs {NEEDS}" if exc.status == 403 else ""
    return _result(route, False, exc.step, exc.reason + hint, exc.status)


def try_direct(src, tgt, source_admin: str, target_admin: str, prefix: str) -> dict:
    """The source admin's file into a target shared drive, which the source admin is made organizer of."""
    from googleapiclient.http import MediaInMemoryUpload
    drive_id = file_id = None
    holder = src
    try:
        drive_id = _do("create the target's shared drive", lambda: tgt.drives().create(
            requestId=uuid.uuid4().hex, fields="id", body={"name": f"{prefix}-PROBE-DIRECT"}).execute()["id"])
        _do("invite the source admin to it", lambda: tgt.permissions().create(
            fileId=drive_id, supportsAllDrives=True, sendNotificationEmail=False, fields="id",
            body={"type": "user", "role": "organizer", "emailAddress": source_admin}).execute(), tries=3)
        made = _do("create a file on the source", lambda: src.files().create(
            body={"name": "Bitport move probe (deletes itself)"},
            media_body=MediaInMemoryUpload(b".", "text/plain"), fields="id,parents").execute())
        file_id = made["id"]
        _do("move it out of the source", lambda: src.files().update(
            fileId=file_id, addParents=drive_id, removeParents=",".join(made.get("parents") or []),
            supportsAllDrives=True, fields="id").execute(), tries=3)
        holder = tgt
        return _result("direct", True, "moved", "a file moved from the source into the target's shared drive")
    except Step as exc:
        return _refused("direct", exc, "the source")
    finally:
        if file_id:
            _cleanup("the probe file", lambda: holder.files().delete(fileId=file_id, supportsAllDrives=True).execute())
        if drive_id:
            _cleanup("the probe drive", lambda: tgt.drives().delete(driveId=drive_id).execute())


def try_source_drive(src, tgt, source_admin: str, target_admin: str, prefix: str) -> dict:
    """The file in a SOURCE shared drive, the target admin a member of it, who takes it out."""
    from googleapiclient.http import MediaInMemoryUpload
    drive_id = file_id = None
    holder = src
    try:
        drive_id = _do("create the source's shared drive", lambda: src.drives().create(
            requestId=uuid.uuid4().hex, fields="id", body={"name": f"{prefix}-PROBE-SOURCE"}).execute()["id"])
        made = _do("put a file into it", lambda: src.files().create(
            body={"name": "Bitport move probe (deletes itself)", "parents": [drive_id]},
            media_body=MediaInMemoryUpload(b".", "text/plain"), fields="id", supportsAllDrives=True).execute())
        file_id = made["id"]
        _do("invite the target admin to it", lambda: src.permissions().create(
            fileId=drive_id, supportsAllDrives=True, sendNotificationEmail=False, fields="id",
            body={"type": "user", "role": "organizer", "emailAddress": target_admin}).execute(), tries=3)
        root = _do("find the target admin's own Drive", lambda: tgt.files().get(
            fileId="root", fields="id").execute()["id"])
        _do("the target admin takes it out", lambda: tgt.files().update(
            fileId=file_id, addParents=root, removeParents=drive_id,
            supportsAllDrives=True, fields="id").execute(), tries=4, wait=4.0)
        holder = tgt
        return _result("source-drive", True, "moved", "the target admin took a file out of a source shared drive")
    except Step as exc:
        return _refused("source-drive", exc, "the source")
    finally:
        if file_id:
            _cleanup("the probe file", lambda: holder.files().delete(fileId=file_id, supportsAllDrives=True).execute())
        if drive_id:
            _cleanup("the probe drive", lambda: src.drives().delete(driveId=drive_id).execute())


def run(auth, settings, routes=ROUTES) -> list[dict]:
    """Try each route; never writes to a source that is not a declared sandbox."""
    if not (settings.source_admin and settings.target_admin):
        return [_result(r, None, "setup", "SOURCE_ADMIN and TARGET_ADMIN must both be set") for r in routes]
    import domain_guard
    why = domain_guard.source_refuse_reason(settings.source_domain, settings.source_domain)
    if why:
        return [_result(r, None, "not tried",
                        f"{settings.source_domain} is a protected source, and the probe writes a one-byte file "
                        f"there. Declare it a sandbox first if it is one ({why})") for r in routes]
    src = auth.source_drive(settings.source_admin, writable=True)
    tgt = auth.target_drive(settings.target_admin)
    prefix = getattr(settings, "staging_drive_prefix", "bitport")
    tries = {"direct": try_direct, "source-drive": try_source_drive}
    return [tries[r](src, tgt, settings.source_admin, settings.target_admin, prefix) for r in routes]


def pair_key(settings) -> str:
    return f"{settings.source_domain}->{settings.target_domain}".lower()


def save(db, settings, results: list[dict]) -> None:
    with db.write() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS move_probe (
            pair TEXT NOT NULL, route TEXT NOT NULL, ok INTEGER, step TEXT, detail TEXT,
            status INTEGER, tested_at TEXT NOT NULL, PRIMARY KEY (pair, route))""")
        for r in results:
            c.execute("INSERT OR REPLACE INTO move_probe VALUES (?,?,?,?,?,?,?)",
                      (pair_key(settings), r["route"], None if r["ok"] is None else int(r["ok"]),
                       r["step"], r["detail"], r["status"], r["testedAt"]))


def latest(db, settings) -> list[dict]:
    """The last result per route for THIS pair; routes never tried are absent."""
    try:
        rows = db.conn.execute("SELECT route, ok, step, detail, status, tested_at FROM move_probe "
                               "WHERE pair=?", (pair_key(settings),)).fetchall()
    except Exception:      # noqa: BLE001 - no table yet: never tried
        return []
    return [{"route": r[0], "ok": None if r[1] is None else bool(r[1]), "step": r[2], "detail": r[3],
             "status": r[4], "testedAt": r[5]} for r in rows]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--account-id", type=int)
    ap.add_argument("--route", action="append", choices=ROUTES)
    args = ap.parse_args(argv)
    from auth import AuthManager
    from config import Settings
    from db import MigrationDB
    settings = Settings(account_id=args.account_id)
    db = MigrationDB(settings.db_path)
    results = run(AuthManager(settings), settings, tuple(args.route or ROUTES))
    save(db, settings, results)
    for r in results:
        mark = {True: "WORKS", False: "REFUSED", None: "NOT TRIED"}[r["ok"]]
        print(f"{mark:9s} {r['route']:13s} {r['step']}: {r['detail']}", flush=True)
    print(json.dumps(results), flush=True)
    return 0      # whether the probe ran, never what it found: a refusal is an answer, not a crash


if __name__ == "__main__":
    sys.exit(main())
