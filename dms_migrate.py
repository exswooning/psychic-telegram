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

# /ac/migrate, confirmed from the live console. NOT /ac/dm, which this
# aimed at originally and which resolves to Devices > Overview -- so every
# run drove the wrong page and blamed its selectors. Taken from a
# screenshot of the address bar, not from documentation.
#
#     Data > Data import & export > Data Import
DMS_URL = "https://admin.google.com/ac/migrate"
DMS_PREFIX = "https://admin.google.com/"

# The page offers several sources. The one that matters is "Business
# Gmail" -- Workspace to Workspace.
#
# "Gmail" sits above it, already expanded, with a live Import button and
# the caption "Import email from a personal Gmail account". A driver that
# clicks the first Import it finds sets up a PERSONAL Gmail import and
# reports success. That is the trap this constant exists to avoid.
DMS_SOURCE_LABEL = "Business Gmail"
DMS_WRONG_SOURCE = "Import email from a personal Gmail account"

# The Business Gmail card just links here, so go straight to it and skip
# the expand entirely. Confirmed from the live console's address bar.
DMS_WORKSPACE_URL = "https://admin.google.com/ac/migrate/googleworkspace"

# The four steps, by their own headings. Taken from the page, not guessed:
#
#   Step 1: Connect to source Google Workspace account
#           field  "Source super admin email address"
#           button "Request connection"
#   Step 2: Upload data import maps
#   Step 3: Configure data import settings
#   Step 4: Import data          button "Start import"  (disabled until 1-3)
#
# Step 1 sends an authorization REQUEST that a super admin on the source
# organisation has to approve out of band. Nothing here can click that for
# you -- it happens in the other tenant, deliberately.
STEP1_FIELD = "Source super admin email address"
STEP1_BUTTON = "Request connection"
STEP2_HEADING = "Upload data import maps"
STEP4_BUTTON = "Start import"
STEP1_PENDING = "Pending authorization"
STEP1_VERIFY = "Verify authorization"


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
    # Sign in on the console HOME, then navigate. Going straight to
    # /ac/migrate makes Google serve "You need to be a super admin" instead
    # of redirecting to sign-in -- and the sign-in loop only types
    # credentials while the URL contains accounts.google.com, so it exited
    # immediately having authenticated nobody. The denial then looked like
    # a permissions problem on an account that is, in fact, a super admin.
    out = dwd_helper._open_dwd_console(p, headful, timeout,
                                       url="https://admin.google.com/",
                                       ready_prefix="https://admin.google.com/",
                                       ready_text="Directory")
    if out is None:
        p.stop()
        return None
    browser, page = out
    page.goto(DMS_URL, wait_until="domcontentloaded",
              timeout=max(timeout * 1000, 30000))
    page.wait_for_timeout(6000)   # the console paints client-side
    return p, browser, page





MANUAL = """
Could not drive the console automatically. Do it by hand -- it is a short
flow and this tool has already signed you in:

  1. admin.google.com/ac/migrate
       (Data -> Data import & export -> Data Import)
  2. Expand "Business Gmail" -- NOT the "Gmail" section above it, which is
     for importing from a personal Gmail account
  3. Connection protocol:   auto / OAuth
  4. Role account:          the SOURCE super admin
  5. Migration start date:  choose how far back to bring mail
  6. Select users, or upload a CSV of source->target pairs
  7. Start

Then run the rest here WITHOUT mail, so nothing is migrated twice:

    main.py --account-id <n> migrate --services drive,calendar,contacts,tasks,chat
"""


def import_map_csv(identities: str, out: str) -> tuple[int, str]:
    """Turn the migration's identity map into Google's import map.

    Step 2 wants source and target addresses. identities.csv carries a
    third column (entity_type) and a header the console does not expect, so
    it is rewritten rather than uploaded as-is.
    """
    import csv

    rows = []
    with open(identities, newline="", encoding="utf-8") as fh:
        for row in csv.reader(fh):
            if len(row) < 2 or "@" not in row[0] or row[0].startswith("source_"):
                continue          # header, or a malformed line
            rows.append((row[0].strip(), row[1].strip()))
    with open(out, "w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(rows)
    return len(rows), out


def start(source_domain: str, source_admin: str, timeout: int,
          headful: bool, dry_run: bool, identities: str | None = None) -> dict:
    """Drive the Google Workspace email data import as far as it can go.

    Stops before Step 4 unless --apply. Step 1 is also a genuine wall: it
    sends an authorization request that a super admin in the SOURCE
    organisation approves in their own console. Nothing here can click that
    on their behalf, and pretending otherwise would be the same class of
    lie as the selectors that reported success against a page they never
    reached.
    """
    result = {"ok": False, "step": "open", "detail": "",
              "manual": MANUAL.strip(), "did": []}
    opened = open_console(headful, timeout)
    if opened is None:
        result["detail"] = "sign-in did not complete"
        return result
    p, browser, page = opened
    try:
        page.goto(DMS_WORKSPACE_URL, wait_until="domcontentloaded",
                  timeout=max(timeout * 1000, 30000))
        page.wait_for_timeout(6000)
        result["step"] = "console"
        log(f"on {page.url}")
        if "googleworkspace" not in page.url:
            result["detail"] = (
                f"expected the Workspace email import page, got {page.url}")
            return result
        result["did"].append("opened the Workspace email import page")

        # --- Step 1 -----------------------------------------------------
        result["step"] = "step1-connect"
        pending = _find_first(page, [f'text="{STEP1_PENDING}"'])
        if pending is not None:
            # A request is already out. The only thing that can advance it is
            # a super admin in the SOURCE tenant clicking the link Google
            # mailed them; until then "Verify authorization" just re-checks.
            result["did"].append("step 1 request is pending authorization")
            verify = _find_first(page, [f'button:has-text("{STEP1_VERIFY}")'])
            if verify is not None and not dry_run:
                verify.click()
                page.wait_for_timeout(6000)
                still = _find_first(page, [f'text="{STEP1_PENDING}"'])
                if still is not None:
                    result["ok"] = True
                    result["step"] = "step1-pending"
                    result["detail"] = (
                        "still pending: the authorization email sent to the "
                        "source super admin has not been approved yet. "
                        "Nothing on this side can approve it.")
                    return result
                result["did"].append("authorization verified")
            else:
                result["ok"] = True
                result["step"] = "step1-pending"
                result["detail"] = ("a connection request is pending the "
                                    "source admin's approval")
                return result
            box = None
        else:
            box = _find_first(page, [
                f'input[placeholder="{STEP1_FIELD}"]',
                f'input[aria-label="{STEP1_FIELD}"]',
                f'//input[contains(@placeholder,"super admin")]',
            ])
            if box is None:
                result["detail"] = (f"no {STEP1_FIELD!r} field on {page.url}")
                return result
        existing = (box.input_value() or "").strip() if box else "connected"
        if existing:
            result["did"].append(f"step 1 already connected as {existing}")
        elif dry_run:
            result["ok"] = True
            result["step"] = "dry-run"
            result["detail"] = (
                "reached Step 1 and stopped. --apply fills the source super "
                "admin and requests the connection; approving it happens in "
                "the SOURCE tenant and cannot be done from here.")
            return result
        else:
            box.click()
            box.type(source_admin, delay=30)
            page.wait_for_timeout(800)
            btn = _find_first(page, [f'button:has-text("{STEP1_BUTTON}")'])
            if btn is None or not btn.is_enabled():
                result["detail"] = (f"{STEP1_BUTTON!r} did not become "
                                    "clickable after entering the address")
                return result
            btn.click()
            page.wait_for_timeout(5000)
            result["did"].append(
                f"requested a connection to {source_admin}")

        # --- Step 2: the import map ------------------------------------
        result["step"] = "step2-map"
        if identities and os.path.isfile(identities):
            n, path = import_map_csv(identities, "/tmp/dms-import-map.csv")
            head = _find_first(page, [f'text="{STEP2_HEADING}"'])
            if head is not None:
                head.click()
                page.wait_for_timeout(3000)
            up = page.locator('input[type="file"]')
            # The input exists in the DOM even while the panel is collapsed and
            # disabled, so its presence proves nothing. An earlier version set
            # files on it and reported success against a panel that never
            # opened. Require a visible, enabled control instead.
            usable = bool(up.count()) and up.first.is_editable()
            if usable:
                before = page.inner_text("body")
                up.first.set_input_files(path)
                page.wait_for_timeout(6000)
                after = page.inner_text("body")
                got = os.path.basename(path) in after or after != before
                if got:
                    result["did"].append(
                        f"uploaded an import map of {n} users")
                else:
                    result["did"].append(
                        f"prepared {path} ({n} users) -- the console did not "
                        "acknowledge the upload")
            else:
                result["did"].append(
                    f"prepared {path} ({n} users) -- step 2 is still locked "
                    "(it unlocks once step 1 is authorized), so upload it "
                    "there once the source admin approves")
        else:
            result["did"].append("no identities file given, skipped Step 2")

        # --- Step 4 -----------------------------------------------------
        result["step"] = "step4-start"
        go = _find_first(page, [f'button:has-text("{STEP4_BUTTON}")'])
        if go is None:
            result["detail"] = f"no {STEP4_BUTTON!r} button found"
            result["ok"] = True     # everything up to here did happen
            return result
        if not go.is_enabled():
            result["ok"] = True
            result["detail"] = (
                f"{STEP4_BUTTON!r} is still disabled -- the console enables it "
                "only once Steps 1-3 are complete, and Step 1 needs a super "
                "admin in the SOURCE tenant to approve the connection "
                "request. That approval is the remaining manual step.")
            return result
        if dry_run:
            result["ok"] = True
            result["detail"] = (f"{STEP4_BUTTON!r} is ready. Re-run with "
                                "--apply to press it.")
            return result
        go.click()
        page.wait_for_timeout(5000)
        result["ok"] = True
        result["detail"] = "import started"
        result["did"].append("pressed " + STEP4_BUTTON)
        return result
    except Exception as exc:      # noqa: BLE001 - report, never traceback out
        result["detail"] = str(exc)[:300]
        return result
    finally:
        if not headful:
            try:
                browser.close(); p.stop()
            except Exception:      # noqa: BLE001
                pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--source-domain", default=os.getenv("SOURCE_DOMAIN", ""))
    ap.add_argument("--target-admin", default=os.getenv("TARGET_ADMIN", ""))
    ap.add_argument("--source-admin", default=os.getenv("SOURCE_ADMIN", ""),
                    help="the SOURCE super admin whose organisation the mail "
                         "is coming from -- this is what Step 1 asks for")
    ap.add_argument("--identities", default="",
                    help="identities.csv; rewritten into Google's two-column "
                         "import map for Step 2")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--headful", action="store_true")
    ap.add_argument("--apply", action="store_true",
                    help="actually drive the flow (default: stop at the "
                         "setup control without moving mail)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    out = start(args.source_domain, args.source_admin or args.target_admin,
                args.timeout, args.headful, dry_run=not args.apply,
                identities=args.identities or None)
    if args.json:
        print(json.dumps(out))
    else:
        for d in out.get("did", []):
            print(f"  did: {d}")
        print(f"{'OK ' if out['ok'] else 'STOPPED'} at {out['step']}: "
              f"{out['detail']}")
        if not out["ok"]:
            print(out["manual"])
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
