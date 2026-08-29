#!/usr/bin/env python3
"""Hand the mail to Google, because Google is not rate-limited by us.

Why this exists
---------------
Mail was 349,560 of the 593,816 items in the last real run -- 58.9%, the
single biggest slice -- and it is the only service behind a ceiling that
cannot be raised. Google caps sustained writes at 3/sec/account and states
that particular limit is not adjustable on request, so:

    349,560 messages / 201 users = ~1,739 each
    1,739 / 3 per second         = ~10 minutes per user, floor
                                   regardless of hardware, workers or nodes

Adding machines does not move it. Drive does not have this problem -- it
already uses files.copy server-side, so no bytes cross this host at all.

Google's own Data Migration Service moves mail inside Google's
infrastructure. It does not spend our project's Gmail write quota, because
it is not calling the API on our behalf. It sidesteps the ceiling instead
of pacing against it.

What this does NOT change
-------------------------
The engine keeps its own Gmail migration, and it stays the default. This is
an alternative for the mail leg, chosen per run, not a replacement:

  * DMS has no usable API (the Email Migration API was retired), so this
    drives the Admin console in a browser -- same approach, same fragility
    and same manual fallback as dwd_helper.py, whose conventions this
    follows deliberately.
  * DMS gives per-user console status, not the per-item ledger that makes
    this tool's re-runs idempotent and lets it name exactly which items
    failed. Handing mail over means giving that up for mail.
  * It does not touch Drive, Calendar, Chat, Contacts or Tasks -- 41% of
    the items, which the engine still migrates.

So the intended shape is a hybrid: DMS moves the mail, `main.py migrate
--services drive,calendar,contacts,tasks,chat` moves everything else.

Every selector below is best-effort against the current Admin console DOM.
The console changes without notice, so each step reports what it did, and a
failure prints the manual path rather than leaving the operator stuck --
the same rule dwd_helper.py states and for the same reason.

Usage
-----
    python3 dms_migrate.py --source-domain a.example --target-admin x@b.example
    python3 dms_migrate.py --status          # read progress back
    python3 dms_migrate.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import dwd_helper

DMS_URL = "https://admin.google.com/ac/dm"
# The console lives under /ac/dm; keep the prefix loose so a redirect to a
# sub-path still counts as "we got there".
DMS_PREFIX = "https://admin.google.com/ac/"

log = dwd_helper.log


def _find_first(page, selectors: list[str], timeout_ms: int = 8000):
    """The first of several candidate selectors that actually appears.

    Written as a list rather than one selector on purpose: the Admin
    console renders the same control differently across rollouts, and a
    single brittle selector is how this class of tool silently stops
    working. Returns None rather than raising -- the caller decides whether
    a missing control is fatal or just a step already done.
    """
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=timeout_ms // len(selectors))
            return loc
        except Exception:      # noqa: BLE001 - a miss is expected, not an error
            continue
    return None


def open_console(headful: bool, timeout: int):
    """Sign in and land on Data Migration, reusing dwd_helper's login."""
    from playwright.sync_api import sync_playwright

    p = sync_playwright().start()
    # "Data migration" is the page's own heading; "Add new" is the DWD
    # page's and would never appear here. Landing on the right page and
    # then timing out on someone else's readiness string is how this
    # reported the console unreachable while sitting on it.
    out = dwd_helper._open_dwd_console(p, headful, timeout, url=DMS_URL,
                                       ready_prefix=DMS_PREFIX,
                                       ready_text="Data migration")
    if out is None:
        p.stop()
        return None
    browser, page = out
    return p, browser, page


MANUAL = """
Could not drive the console automatically. Do it by hand -- it is a short
flow and this tool has already signed you in:

  1. admin.google.com  ->  Data  ->  Data import & export  ->  Data migration
  2. Migration source:      Google Workspace
  3. Connection protocol:   auto / OAuth
  4. Role account:          the SOURCE super admin
  5. Migration start date:  choose how far back to bring mail
  6. Select users, or upload a CSV of source->target pairs
  7. Start

Then run the rest here WITHOUT mail, so nothing is migrated twice:

    main.py --account-id <n> migrate --services drive,calendar,contacts,tasks,chat
"""


def start(source_domain: str, target_admin: str, timeout: int,
          headful: bool, dry_run: bool) -> dict:
    """Walk the Data Migration setup as far as the console allows.

    Deliberately stops short of pressing the final Start unless --apply is
    given: this moves real mail for real users, and a tool that fires that
    from a selector match it cannot verify is not one to trust.
    """
    result = {"ok": False, "step": "open", "detail": "", "manual": MANUAL.strip()}
    opened = open_console(headful, timeout)
    if opened is None:
        result["detail"] = "sign-in did not complete"
        return result
    p, browser, page = opened
    try:
        result["step"] = "console"
        log(f"landed on {page.url}")
        if "/ac/dm" not in page.url:
            result["detail"] = (
                f"expected the Data Migration console, got {page.url}. The "
                "path moves between console rollouts.")
            return result

        setup = _find_first(page, [
            'button:has-text("Set up data migration")',
            'button:has-text("Set Up Data Migration")',
            'a:has-text("Set up data migration")',
            'button:has-text("Continue")',
        ])
        if setup is None:
            result["detail"] = ("no 'Set up data migration' control found -- "
                                "a migration may already be configured")
            result["step"] = "already-configured?"
            return result
        if dry_run:
            result["ok"] = True
            result["step"] = "dry-run"
            result["detail"] = ("reached the setup control and stopped: "
                                "--apply moves real mail")
            return result

        setup.click()
        page.wait_for_timeout(1500)
        result["step"] = "source"
        src = _find_first(page, ['text="Google Workspace"',
                                 'div[role="option"]:has-text("Google Workspace")'])
        if src is not None:
            src.click()
            log("selected Google Workspace as the migration source")
        result["detail"] = (
            "opened the setup flow. The remaining fields (role account, start "
            "date, user list) are per-tenant and the console asks for them in "
            "an order that changes between rollouts -- finish in the browser "
            "window, which is left open.")
        result["ok"] = True
        return result
    except Exception as exc:      # noqa: BLE001 - report, never traceback out
        result["detail"] = str(exc)[:300]
        return result
    finally:
        if not headful:
            try:
                browser.close()
                p.stop()
            except Exception:      # noqa: BLE001
                pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--source-domain", default=os.getenv("SOURCE_DOMAIN", ""))
    ap.add_argument("--target-admin", default=os.getenv("TARGET_ADMIN", ""))
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--headful", action="store_true")
    ap.add_argument("--apply", action="store_true",
                    help="actually drive the flow (default: stop at the "
                         "setup control without moving mail)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    out = start(args.source_domain, args.target_admin, args.timeout,
                args.headful, dry_run=not args.apply)
    if args.json:
        print(json.dumps(out))
    else:
        print(f"{'OK ' if out['ok'] else 'STOPPED'} at {out['step']}: "
              f"{out['detail']}")
        if not out["ok"]:
            print(out["manual"])
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
