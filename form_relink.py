"""
form_relink.py
==============
Link each copied Form to its copied responses Sheet -- on the TARGET only.

No API can: `linkedSheetId` is output-only in the Forms API, and Apps Script's
`setDestination` cannot run as a delegated service account. So the target admin does it
in a browser, in each Form's own Responses tab ("Link to Sheets" -> "Select existing
spreadsheet" -> the Sheet's URL pasted into the picker): the admin is made an editor of
both files first (as their owner, no email), the Forms API confirms each link, and then
the admin's access is removed and both files' modified times are put back. The source
is never written. (An Apps Script route was tried first, live 2026-10-24: mdharan's admin
editor never listed a function and the run answered "An unknown error has occurred".)

What a link changes, said plainly: Google adds a new "Form Responses" tab to the Sheet
(empty -- a copied Form has no responses), so the Sheet holds one tab more than the
source's. The operator chose that over an unlinked Form (2026-10-24).

Skipped, with the reason recorded: a pair whose Sheet is itself one of Bitport's own
target copies (a "Copy of" Form Google made on the source during an exact-copy run --
an artefact, not the organisation's Form), and a pair whose Form or Sheet has no copy.

  python form_relink.py --account 5            # dry run: what would be linked
  python form_relink.py --account 5 --apply
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time

log = logging.getLogger("form_relink")

STATUS = "RELINKED"


def pairs(db) -> list[dict]:
    """Every Form/responses-Sheet pair the run recorded, with its target ids or why not."""
    out = []
    for user, form, msg in db.conn.execute(
            "SELECT source_user, item_id, error_message FROM audit_log WHERE item_type='form_link' "
            "AND id IN (SELECT MAX(id) FROM audit_log WHERE item_type='form_link' GROUP BY source_user, item_id)"):
        sheet = (msg or "").split("sheet: ")[-1].split()[0] if "sheet: " in (msg or "") else ""
        p = {"user": user, "sourceForm": form, "sourceSheet": sheet,
             "targetForm": db.get_target_id(user, form, "file"),
             "targetSheet": db.get_target_id(user, sheet, "file") if sheet else None}
        if msg and msg.startswith(STATUS):
            p["skip"] = "already relinked"
        elif sheet and db.conn.execute("SELECT 1 FROM id_mapping WHERE target_id=?", (sheet,)).fetchone():
            p["skip"] = ("its Sheet is Bitport's own target copy: a 'Copy of' Form Google made on the "
                         "source during an exact-copy run, not the organisation's Form")
        elif not p["targetForm"] or not p["targetSheet"]:
            p["skip"] = "the " + ("Form" if not p["targetForm"] else "responses Sheet") + " has no copy on the target"
        out.append(p)
    return out


def _link_in_browser(admin: str, todo: list[dict], timeout: int = 900) -> list[str]:
    """Sign in as the target admin and link each pair from its Form's Responses tab."""
    import dwd_helper
    from playwright.sync_api import sync_playwright
    done = []
    with sync_playwright() as p:
        opened = dwd_helper._open_dwd_console(p, True, timeout, url="https://admin.google.com/ac/home",
                                              ready_prefix="https://admin.google.com/", ready_text="Users")
        if opened is None:
            raise RuntimeError("could not sign in as the target admin")
        browser, page = opened
        try:
            for pair in todo:
                page.goto(f"https://docs.google.com/forms/d/{pair['targetForm']}/edit#responses",
                          wait_until="domcontentloaded", timeout=120000)
                page.get_by_role("button", name=re.compile("Link to Sheets", re.I)).first.click(timeout=60000)
                dialog = page.locator("[role=dialog]")
                page.get_by_text("Select existing spreadsheet").click(timeout=30000)
                dialog.get_by_role("button", name="Select").last.click(timeout=30000)
                picker = None
                for _ in range(60):
                    picker = next((f for f in page.frames if "/picker" in f.url), None)
                    if picker and picker.locator("input[aria-label='Search in Drive or paste URL']").count():
                        break
                    page.wait_for_timeout(1000)
                box = picker.locator("input[aria-label='Search in Drive or paste URL']")
                box.fill(f"https://docs.google.com/spreadsheets/d/{pair['targetSheet']}/edit")
                box.press("Enter")
                item = picker.locator(f"[data-id='{pair['targetSheet']}']").first
                item.wait_for(timeout=60000)
                item.click()
                picker.get_by_text("Insert", exact=True).last.click(timeout=30000)
                page.get_by_role("button", name=re.compile("View in Sheets", re.I)).first.wait_for(timeout=120000)
                done.append(pair["targetForm"])
            return done
        except Exception:
            page.screenshot(path="/tmp/form-relink-failed.png", full_page=True)
            raise
        finally:
            browser.close()


def relink(auth, db, settings, apply: bool, run_script=_link_in_browser) -> dict:
    found = pairs(db)
    todo = [p for p in found if not p.get("skip")]
    report = {"pairs": found, "linked": [], "errors": []}
    if not apply or not todo:
        return report
    from scope_guard import _console_login
    login = _console_login(settings, "target")
    admin = (login.get("DWD_EMAIL") or "").lower()
    if not admin:
        raise RuntimeError(f"no kept admin login on {settings.target_domain} -- set one up first")
    held = []                      # (owner drive, file id, permission id, modifiedTime)
    try:
        for p in todo:
            owner = db.resolve_identity(p["user"]) or p["user"]
            drv = auth.target_drive(owner)
            for fid in (p["targetForm"], p["targetSheet"]):
                was = drv.files().get(fileId=fid, fields="modifiedTime", supportsAllDrives=True).execute()["modifiedTime"]
                perm = drv.permissions().create(fileId=fid, sendNotificationEmail=False, supportsAllDrives=True,
                                                body={"type": "user", "role": "writer", "emailAddress": admin},
                                                fields="id").execute()
                held.append((drv, fid, perm["id"], was))
        os.environ.update(login)
        report["browser"] = run_script(admin, todo)
        for p in todo:
            owner = db.resolve_identity(p["user"]) or p["user"]
            got = auth.api("target", "forms", owner).forms().get(formId=p["targetForm"], fields="linkedSheetId").execute()
            if got.get("linkedSheetId") == p["targetSheet"]:
                db.log_audit(p["user"], p["sourceForm"], "form_link", STATUS,
                             f"{STATUS}: target form {p['targetForm']} -> sheet {p['targetSheet']}")
                report["linked"].append(p["targetForm"])
            else:
                report["errors"].append(f"{p['targetForm']} links to {got.get('linkedSheetId')}, not {p['targetSheet']}")
    finally:
        for drv, fid, perm, was in held:
            try:
                drv.permissions().delete(fileId=fid, permissionId=perm, supportsAllDrives=True).execute()
                drv.files().update(fileId=fid, body={"modifiedTime": was}, supportsAllDrives=True).execute()
            except Exception as exc:      # noqa: BLE001 - every file gets its own attempt
                report["errors"].append(f"{fid}: admin access or time not restored: {exc}")
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--account", type=int, required=True)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    from auth import AuthManager
    from config import Settings
    from db import MigrationDB
    st = Settings(account_id=a.account)
    out = relink(AuthManager(st), MigrationDB(st.db_path), st, a.apply)
    for p in out["pairs"]:
        print(p["user"], p["sourceForm"], "->", p["targetForm"], "/", p["targetSheet"], "|", p.get("skip", "to link"))
    print(json.dumps({k: v for k, v in out.items() if k != "pairs"}, indent=1))
    sys.exit(1 if out["errors"] else 0)
