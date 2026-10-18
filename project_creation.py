"""
project_creation.py -- a tenant's "Allow users to create projects", switched for a setup.

Live: dharan.gov.np refused `gcloud projects create` with "Cloud Resource Manager
project creation is disabled" -- a tenant policy, not a Bitport fault, and the setup
cannot make its service account without a project. Google publishes no API for the
setting; it is one checkbox in the Admin console (Apps > Additional Google services >
Google Cloud Platform > Cloud Resource Manager API settings), at a fixed address. So
this signs in exactly as delegation does (dwd_helper's sign-in: password fill, 2-Step
watch, hand-off to the operator's device) and ticks or unticks that box.

A setup only ever turns it on for as long as it needs and puts back what it found:
full_setup records the previous state and restores it whether or not setup succeeded.

    DWD_EMAIL=admin@x DWD_PASSWORD=... python project_creation.py status|on|off
"""
from __future__ import annotations

import argparse
import os
import sys

import dwd_helper

SERVICE = "https://admin.google.com/ac/managedsettings/923893674115"
URL = SERVICE + "/cloudresourcemanager"
LABEL = "Allow users to create projects"


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
    a = ap.parse_args()
    if not (os.getenv("DWD_EMAIL") and os.getenv("DWD_PASSWORD")):
        sys.exit("set DWD_EMAIL and DWD_PASSWORD")
    was = run(None if a.action == "status" else a.action == "on")
    print(f"before: {'on' if was else 'off'}")
