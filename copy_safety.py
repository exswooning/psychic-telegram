"""Server-side copy safety test: does files.copy by a source user INTO a staging shared drive
on the target change ANYTHING on the source?

Known from Google's own audit log (dharan.gov.np, 2026-10-19): copying a Form, a Sheet that
collects a Form's responses, or an Apps Script makes Google drop a "Copy of ..." into the
source owner's My Drive. This measures it per file type, with evidence, on a SANDBOX source.

Safety, each pinned by tests/test_copy_safety.py:
  * a government source is refused outright;
  * the only call that acts on the source is files.copy into the test's own staging drive
    (the source Drive is the engine's ReadOnlyDrive, which refuses every other write);
  * cleanup touches only the TARGET: the staging drive, after removing the source user's
    membership. A change found on the source is REPORTED (id, name, time) and left alone.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone

G = "application/vnd.google-apps."
FORM_MIME = G + "form"
OFFICE = {"docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
          "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
          "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}
TYPES = ["doc", "sheet", "sheet_form", "slides", "drawing", "form", "script",
         "pdf", "image", "docx", "xlsx", "pptx", "binary"]
FIELDS = "id,name,mimeType,version,modifiedTime,md5Checksum,trashed,size"
MAX_BYTES = 50 * 1024 * 1024        # a sample is a probe, not a payload


def _stamp(t: float | None = None) -> str:
    return datetime.fromtimestamp(t if t is not None else time.time(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def type_of(f: dict, form_sheets: set) -> str | None:
    m = f.get("mimeType") or ""
    if m == G + "document": return "doc"
    if m == G + "spreadsheet": return "sheet_form" if f["id"] in form_sheets else "sheet"
    if m == G + "presentation": return "slides"
    if m == G + "drawing": return "drawing"
    if m == FORM_MIME: return "form"
    if m == G + "script": return "script"
    if m == "application/pdf": return "pdf"
    if m.startswith("image/"): return "image"
    for k, v in OFFICE.items():
        if m == v: return k
    if m.startswith(G) or m == G + "folder": return None       # shortcut, folder, site, map ...
    return "binary"


def list_owned(drive) -> list[dict]:
    """Everything the user owns, trashed or not -- the BEFORE/AFTER snapshot's raw rows."""
    out, token = [], None
    while True:
        r = drive.files().list(q="'me' in owners", pageSize=1000, pageToken=token,
                               fields=f"nextPageToken,files({FIELDS})").execute()
        out += r.get("files", [])
        token = r.get("nextPageToken")
        if not token:
            return out


def snapshot(drive) -> dict:
    return {f["id"]: [f.get("name"), f.get("mimeType"), f.get("version"), f.get("modifiedTime"),
                      f.get("md5Checksum"), bool(f.get("trashed"))] for f in list_owned(drive)}


def diff(before: dict, after: dict) -> dict:
    row = lambda i, v: {"id": i, "name": v[0], "mimeType": v[1], "modifiedTime": v[3]}      # noqa: E731
    return {"added": [row(i, v) for i, v in after.items() if i not in before],
            "removed": [row(i, v) for i, v in before.items() if i not in after],
            "changed": [{**row(i, after[i]), "was": before[i], "now": after[i]}
                        for i in after if i in before and
                        (after[i][2], after[i][3], after[i][5]) != (before[i][2], before[i][3], before[i][5])]}


def pick_samples(files: list[dict], per_type: int, form_sheets: set) -> dict:
    out: dict = {t: [] for t in TYPES}
    for f in files:
        if f.get("trashed"):
            continue
        t = type_of(f, form_sheets)
        if t and len(out[t]) < per_type and int(f.get("size") or 0) <= MAX_BYTES:
            out[t].append({"id": f["id"], "name": f["name"], "mimeType": f["mimeType"]})
    return out


def _form_sheets(auth, drive, user: str) -> set | None:
    """drive_engine's own detection (DriveMigrator._form_sheets), run for this one user."""
    import types

    from drive_engine import DriveMigrator
    stub = types.SimpleNamespace(src=drive, auth=auth, reader=user, source_user=user,
                                 _retry=lambda fn, **_k: fn())
    return DriveMigrator._form_sheets(stub)


def audit_events(settings, user: str, since: str, reports=None) -> list[dict] | None:
    """Drive audit events in [since, now] by or about the user; None when unreadable."""
    import source_guard
    try:
        svc = reports or source_guard._reports(settings)
        rows = []
        for x, e, p in source_guard._events(svc, "drive", since):
            if (x.get("actor") or {}).get("email") == user or p.get("owner") == user:
                rows.append({"at": x["id"]["time"], "event": e.get("name"), "id": p.get("doc_id"),
                             "item": p.get("doc_title"), "copyType": p.get("copy_type"),
                             "actor": (x.get("actor") or {}).get("email"),
                             "app": p.get("originating_app_id")})
        return rows
    except Exception as exc:      # noqa: BLE001 - reported as UNKNOWN, never a pass
        print(f"audit log unreadable: {exc}", flush=True)
        return None


def attribute(changes: dict, samples: list[dict], events: list[dict] | None) -> tuple[dict, list[dict]]:
    """sample id -> [source changes it caused]; plus changes nobody can be blamed for."""
    by_sample: dict = {s["id"]: [] for s in samples}
    left = []
    ids = {s["id"] for s in samples}
    for kind in ("added", "removed", "changed"):
        for c in changes[kind]:
            hit = next((s for s in samples if c["id"] == s["id"] or (kind == "added" and (c["name"] or "") in
                        (f"Copy of {s['name']}", s["name"]))), None)
            if hit is None:     # the audit log names the copied original on the new file's events
                hit = next((s for e in events or [] if e.get("id") == c["id"] and e.get("item")
                            for s in samples if e["item"] in (s["name"], f"Copy of {s['name']}")), None)
            if hit is None:
                left.append({**c, "kind": kind})
            else:
                by_sample[hit["id"]].append({**c, "kind": kind})
    for e in events or []:       # an event that touched a sample id by anything but a read/copy-out
        if e.get("id") in ids and e["event"] in ("edit", "rename", "move", "trash", "delete", "create"):
            by_sample[e["id"]].append({"kind": "event", **e})
    return by_sample, left


def verdicts(samples: dict, copies: dict, by_sample: dict, audit_ok: bool) -> dict:
    out = {}
    for t, rows in samples.items():
        if not rows:
            out[t] = "no sample"
        elif any(by_sample.get(s["id"]) for s in rows):
            out[t] = "UNSAFE"
        elif any(not copies.get(s["id"], {}).get("ok") for s in rows) or not audit_ok:
            out[t] = "UNKNOWN"
        else:
            out[t] = "SAFE"
    return out


def refuse_reason(settings, user: str) -> str | None:
    import domain_guard
    for d in (settings.source_domain, user.rsplit("@", 1)[-1]):
        if d and domain_guard.is_government(d):
            return f"{d} is a government domain: the safety test never runs against one"
    if not settings.source_domain:
        return "this account has no source domain"
    return None


def cleanup(tgt, staging: str | None, user: str) -> dict:
    """TARGET only: drop the source user's membership, then the test's own staging drive."""
    if not staging:
        return {"ok": True, "detail": "no staging drive was made"}
    res: dict = {"ok": False, "drive": staging}
    try:
        perms = tgt.permissions().list(fileId=staging, supportsAllDrives=True, useDomainAdminAccess=True,
                                       fields="permissions(id,emailAddress)").execute().get("permissions", [])
        for p in perms:
            if (p.get("emailAddress") or "").lower() == user.lower():
                tgt.permissions().delete(fileId=staging, permissionId=p["id"], supportsAllDrives=True,
                                         useDomainAdminAccess=True).execute()
        tgt.drives().delete(driveId=staging, useDomainAdminAccess=True, allowItemDeletion=True).execute()
        res["ok"] = True
    except Exception as exc:      # noqa: BLE001 - reported; the drive name says whose it is
        res["error"] = str(exc)[:300]
    return res


def run(settings, auth, user: str, per_type: int = 2, settle_min: float = 10, apply: bool = False,
        reports=None, sleep=time.sleep, say=lambda m: print(m, flush=True)) -> dict:
    why = refuse_reason(settings, user)
    if why:
        raise SystemExit(f"REFUSED: {why}")
    ok, msg = auth.verify_delegation("source", user)
    if not ok:
        raise SystemExit(f"REFUSED: the source key does not mint the scopes it needs (incl. `drive`) "
                         f"for {user}: {msg}")
    src = auth.source_drive(user)
    say(f"1/7 listing {user}'s Drive")
    form_sheets = _form_sheets(auth, src, user)
    files = list_owned(src)
    samples = pick_samples(files, per_type, form_sheets or set())
    if form_sheets is None:
        samples["sheet"] = samples["sheet_form"] = []        # unknown which Sheet is linked: neither is sampled
    chosen = [s | {"type": t} for t, rows in samples.items() for s in rows]
    for t in TYPES:
        say(f"   {t}: " + (", ".join(s["name"] for s in samples[t]) or "no sample"))
    if not apply:
        return {"dryRun": True, "samples": samples}

    say("2/7 snapshot of the source Drive (before)")
    before = snapshot(src)
    started = _stamp()
    tgt = auth.target_drive(settings.target_admin)
    staging, copies, report = None, {}, {}
    try:
        say("3/7 creating the staging shared drive on the target")
        staging = tgt.drives().create(requestId=uuid.uuid4().hex, fields="id",
                                      body={"name": f"BITPORT-SAFETY-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"}
                                      ).execute()["id"]
        tgt.permissions().create(fileId=staging, supportsAllDrives=True, sendNotificationEmail=False, fields="id",
                                 body={"type": "user", "role": "organizer", "emailAddress": user}).execute()
        import auth as auth_mod
        copier = auth_mod.allow_copy_into(src, staging)
        say(f"4/7 copying {len(chosen)} files server-side as {user}")
        for s in chosen:
            try:
                new = copier.files().copy(fileId=s["id"], body={"name": s["name"], "parents": [staging]},
                                          supportsAllDrives=True, fields="id").execute()
                copies[s["id"]] = {"ok": True, "copyId": new.get("id")}
            except Exception as exc:      # noqa: BLE001 - recorded per file
                copies[s["id"]] = {"ok": False, "error": str(exc)[:300]}
        say(f"5/7 waiting {settle_min} min for Google to settle")
        end = time.time() + settle_min * 60
        while time.time() < end:
            sleep(min(60, max(0, end - time.time())))
            say(f"   {max(0, int(end - time.time()) // 60)} min left")
        after = snapshot(src)
        changes = diff(before, after)
        events = audit_events(settings, user, started, reports)
        by_sample, unattributed = attribute(changes, chosen, events)
        v = verdicts(samples, copies, by_sample, events is not None)
        overall = ("SAFE" if all(x in ("SAFE", "no sample") for x in v.values()) and not unattributed
                   else "UNSAFE" if "UNSAFE" in v.values() or unattributed else "UNKNOWN")
        report = {"user": user, "started": started, "finished": _stamp(), "samples": samples, "copies": copies,
                  "verdicts": v, "overall": overall, "changes": changes, "attributed": by_sample,
                  "unattributed": unattributed, "auditReadable": events is not None,
                  "auditEvents": events or [], "rows": [
                      {"type": s["type"], "name": s["name"], "copied": copies[s["id"]]["ok"],
                       "verdict": v[s["type"]], "evidence": by_sample[s["id"]] or copies[s["id"]].get("error", "")}
                      for s in chosen]}
        say("6/7 source compared: " + ", ".join(f"{t}={x}" for t, x in v.items()))
    finally:
        say("7/7 cleaning up the target staging drive")
        report["cleanup"] = cleanup(tgt, staging, user)
    if report.get("overall") is None:
        report = {"error": "the run stopped before it could compare", **report}
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", str(settings.account_id or "_none"))
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"copy-safety-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json")
    with open(path, "w") as fh:
        json.dump(report, fh, indent=1)
    say(f"DONE overall={report.get('overall')} report={path}")
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--account", "--account-id", dest="account", type=int, required=True)
    ap.add_argument("--user", required=True)
    ap.add_argument("--per-type", type=int, default=2)
    ap.add_argument("--settle-min", type=float, default=10)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    from auth import AuthManager
    from config import Settings
    settings = Settings(account_id=a.account)
    settings.transfer_mode = "server_side"       # the mode under test: its scope set includes `drive`
    run(settings, AuthManager(settings), a.user, a.per_type, a.settle_min, a.apply)
    return 0        # a verdict is the result, not a crash (run_watch reads non-zero as one)


if __name__ == "__main__":
    sys.exit(main())
