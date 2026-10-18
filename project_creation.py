"""
project_creation.py -- a tenant's "Allow users to create projects", switched for a setup.

Live: dharan.gov.np refused `gcloud projects create` with "Cloud Resource Manager
project creation is disabled" -- a tenant policy, not a Bitport fault, and the setup
cannot make its service account without a project. Google publishes no API for the
setting; it is one checkbox in the Admin console (Apps > Additional Google services >
Google Cloud Platform > Cloud Resource Manager API settings), at a fixed address. So
this signs in exactly as delegation does (dwd_helper's sign-in: password fill, 2-Step
watch, hand-off to the operator's device) and ticks or unticks that box.

Setup turns it on when Google refuses a project for this reason, and RECORDS that it did
(remember()): it stays on while the migration runs -- a re-provision or repair mid-run
needs it again -- and is turned back off when the migration is approved as complete
(api_server, from the Final Report), or by hand with `off --account N --side S`.

    DWD_EMAIL=admin@x DWD_PASSWORD=... python project_creation.py status|on|off
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

import dwd_helper

SERVICE = "https://admin.google.com/ac/managedsettings/923893674115"
URL = SERVICE + "/cloudresourcemanager"
LABEL = "Allow users to create projects"


HERE = os.path.dirname(os.path.abspath(__file__))


def _marker(account_id: int, side: str) -> str:
    return os.path.join(HERE, "logs", str(account_id), f"project-creation-{side}.json")


def remember(account_id: int, side: str, domain: str, admin: str) -> None:
    """Setup turned project creation on for this tenant: it is owed being turned off."""
    os.makedirs(os.path.dirname(_marker(account_id, side)), exist_ok=True)
    with open(_marker(account_id, side), "w", encoding="utf-8") as fh:
        json.dump({"side": side, "domain": domain, "admin": admin,
                   "turnedOnAt": datetime.datetime.now(datetime.timezone.utc)
                   .strftime("%Y-%m-%dT%H:%M:%SZ")}, fh)


def pending(account_id: int) -> list[dict]:
    """Every side of this migration whose project creation setup turned on and nobody
    has turned off yet."""
    out = []
    for side in ("source", "target"):
        try:
            with open(_marker(account_id, side), encoding="utf-8") as fh:
                out.append(json.load(fh))
        except (OSError, ValueError):
            continue
    return out


def forget(account_id: int, side: str) -> None:
    try:
        os.remove(_marker(account_id, side))
    except OSError:
        pass


class SettingNotFound(RuntimeError):
    pass


def _box(page):
    box = page.get_by_role("checkbox", name=LABEL)
    if box.count() == 0:
        raise SettingNotFound(f"no '{LABEL}' checkbox at {page.url}")
    return box.first


def read(page) -> bool:
    page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    page.get_by_role("checkbox", name=LABEL).first.wait_for(timeout=60000)
    return _box(page).is_checked()


def switch(page, want: bool) -> bool:
    """Make the setting `want` on an Admin console page already signed in. Returns
    what it was before. Re-reads after saving: a save that did not land is an error."""
    before = read(page)
    if before == want:
        dwd_helper.log(f"  project creation already {'on' if want else 'off'}")
        return before
    _box(page).click()
    page.get_by_role("button", name="Save").click(timeout=15000)
    page.wait_for_timeout(4000)
    if read(page) != want:
        raise RuntimeError(f"saved, but '{LABEL}' still reads {before} at {page.url}")
    dwd_helper.log(f"  project creation turned {'on' if want else 'off'} (was {'on' if before else 'off'})")
    return before


def run(want: bool | None, timeout: int = 900, headful: bool = True) -> bool:
    """Sign in with DWD_EMAIL/DWD_PASSWORD, then read (want=None) or set the setting.
    Returns its state before anything changed."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        opened = dwd_helper._open_dwd_console(p, headful, timeout, url=URL,
                                              ready_prefix=SERVICE, ready_text=LABEL)
        if opened is None:
            raise RuntimeError("could not sign in to the Admin console")
        browser, page = opened
        try:
            return read(page) if want is None else switch(page, want)
        finally:
            browser.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("action", choices=("status", "on", "off"))
    ap.add_argument("--account", type=int, help="with off: the migration whose record it clears")
    ap.add_argument("--side", choices=("source", "target"))
    a = ap.parse_args()
    if not (os.getenv("DWD_EMAIL") and os.getenv("DWD_PASSWORD")):
        sys.exit("set DWD_EMAIL and DWD_PASSWORD")
    was = run(None if a.action == "status" else a.action == "on")
    print(f"before: {'on' if was else 'off'}")
    if a.action == "off" and a.account is not None and a.side:
        forget(a.account, a.side)
        print(f"project creation is off again for migration {a.account} ({a.side})")
