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

# Every tenant setting a migration may need switched on for its duration: where it is,
# the checkbox, the panel to open first (None: the checkbox is on the page), and its
# record's file name. Live, dharan.gov.np had both off: Cloud projects (setup needs one)
# and the Drive SDK ("The domain administrators have disabled Drive apps" -- no app,
# Bitport included, can read Drive through the API).
SETTINGS = {
    "project_creation": {"url": URL, "ready": SERVICE, "label": LABEL, "open": None,
                         "file": "project-creation", "name": "project creation"},
    "drive_sdk": {"url": "https://admin.google.com/ac/managedsettings/55656082996/data",
                  "ready": "https://admin.google.com/ac/managedsettings/55656082996",
                  "label": "Allow users to access Google Drive with the Drive SDK API",
                  "open": "Drive SDK", "file": "drive-sdk", "name": "Drive SDK access"},
}


HERE = os.path.dirname(os.path.abspath(__file__))


def _marker(account_id: int, side: str, setting: str = "project_creation") -> str:
    return os.path.join(HERE, "logs", str(account_id), f"{SETTINGS[setting]['file']}-{side}.json")


def remember(account_id: int, side: str, domain: str, admin: str,
             setting: str = "project_creation") -> None:
    """Setup turned this setting on for the tenant: it is owed being turned off."""
    os.makedirs(os.path.dirname(_marker(account_id, side, setting)), exist_ok=True)
    with open(_marker(account_id, side, setting), "w", encoding="utf-8") as fh:
        json.dump({"side": side, "domain": domain, "admin": admin, "setting": setting,
                   "name": SETTINGS[setting]["name"],
                   "turnedOnAt": datetime.datetime.now(datetime.timezone.utc)
                   .strftime("%Y-%m-%dT%H:%M:%SZ")}, fh)


def pending(account_id: int) -> list[dict]:
    """Every setting, on every side of this migration, that setup turned on and nobody
    has turned off yet."""
    out = []
    for setting in SETTINGS:
        for side in ("source", "target"):
            try:
                with open(_marker(account_id, side, setting), encoding="utf-8") as fh:
                    out.append({"setting": setting, "name": SETTINGS[setting]["name"],
                                **json.load(fh)})
            except (OSError, ValueError):
                continue
    return out


def forget(account_id: int, side: str, setting: str = "project_creation") -> None:
    try:
        os.remove(_marker(account_id, side, setting))
    except OSError:
        pass


class SettingNotFound(RuntimeError):
    pass


def _box(page, setting: str = "project_creation"):
    label = SETTINGS[setting]["label"]
    box = page.get_by_role("checkbox", name=label)
    if box.count() == 0:
        raise SettingNotFound(f"no '{label}' checkbox at {page.url}")
    return box.first


def read(page, setting: str = "project_creation") -> bool:
    cfg = SETTINGS[setting]
    page.goto(cfg["url"], wait_until="domcontentloaded", timeout=60000)
    if cfg["open"]:
        row = page.get_by_text(cfg["open"], exact=True).locator("visible=true").first
        row.wait_for(timeout=60000)
        row.click()
        page.wait_for_timeout(2000)
    page.get_by_role("checkbox", name=cfg["label"]).first.wait_for(timeout=60000)
    return _box(page, setting).is_checked()


def switch(page, want: bool, setting: str = "project_creation") -> bool:
    """Make the setting `want` on an Admin console page already signed in. Returns
    what it was before. Re-reads after saving: a save that did not land is an error."""
    name = SETTINGS[setting]["name"]
    before = read(page, setting)
    if before == want:
        dwd_helper.log(f"  {name} already {'on' if want else 'off'}")
        return before
    _box(page, setting).click()
    page.get_by_role("button", name="Save").locator("visible=true").first.click(timeout=15000)
    page.wait_for_timeout(4000)
    if read(page, setting) != want:
        raise RuntimeError(f"saved, but '{SETTINGS[setting]['label']}' still reads {before} at {page.url}")
    dwd_helper.log(f"  {name} turned {'on' if want else 'off'} (was {'on' if before else 'off'})")
    return before


def run(want: bool | None, timeout: int = 900, headful: bool = True,
        setting: str = "project_creation") -> bool:
    """Sign in with DWD_EMAIL/DWD_PASSWORD, then read (want=None) or set the setting.
    Returns its state before anything changed."""
    from playwright.sync_api import sync_playwright
    cfg = SETTINGS[setting]
    with sync_playwright() as p:
        opened = dwd_helper._open_dwd_console(p, headful, timeout, url=cfg["url"],
                                              ready_prefix=cfg["ready"],
                                              ready_text=cfg["open"] or cfg["label"])
        if opened is None:
            raise RuntimeError("could not sign in to the Admin console")
        browser, page = opened
        try:
            return read(page, setting) if want is None else switch(page, want, setting)
        finally:
            browser.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("action", choices=("status", "on", "off"))
    ap.add_argument("--account", type=int, help="with off: the migration whose record it clears")
    ap.add_argument("--side", choices=("source", "target"))
    ap.add_argument("--setting", choices=tuple(SETTINGS), default="project_creation")
    a = ap.parse_args()
    if not (os.getenv("DWD_EMAIL") and os.getenv("DWD_PASSWORD")):
        sys.exit("set DWD_EMAIL and DWD_PASSWORD")
    was = run(None if a.action == "status" else a.action == "on", setting=a.setting)
    print(f"before: {'on' if was else 'off'}")
    if a.action == "off" and a.account is not None and a.side:
        forget(a.account, a.side, a.setting)
        print(f"{SETTINGS[a.setting]['name']} is off again for migration {a.account} ({a.side})")
