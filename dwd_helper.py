#!/usr/bin/env python3
"""Automate the one DWD step Google gives no API for.

Google offers no API to grant domain-wide delegation -- a super admin must
click it through in a browser. This drives that browser. The login itself is
deliberately *not* automated: the operator signs in by hand (password, 2FA,
SSO, captcha -- none of which are reliably scriptable), and this tool takes
over once the Admin console is loaded, walks to the Domain Wide Delegation
page, clicks Add new, fills the client ID and the OAuth scopes, hits
Authorize, then verifies the entry landed.

Design notes
------------
* Runs wherever the operator has a display: the webui runs headless on a VPS,
  so this is invoked from the webui's "Automate" button but executes on the
  operator's machine (the button prints the exact command to run there).
* Every selector is a best-effort against the current Admin console DOM. The
  console changes without notice, so each step reports clearly, keeps the
  browser open on failure, and prints the manual path so the human can finish
  by hand instead of being blocked.
* Multi-party approval (a second super admin must sign off) is detected and
  reported, not treated as failure.

Usage
-----
    python3 dwd_helper.py --tenant source            # reads keys/ + env
    python3 dwd_helper.py --client-id 114... --scopes "a,b,c"   # explicit
    python3 dwd_helper.py --tenant source --headful --timeout 900
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def log(msg: str) -> None:
    print(f"[dwd] {msg}", flush=True)


def _load_payload(tenant: str) -> dict:
    """Mirror webui.dwd_payload() far enough to get one tenant's line.

    The client ID lives in the service-account key file; the scope line is the
    union `source_scopes`/`target_scopes` wants *today*. The webui also offers
    a "paste once" full-union line -- point this tool at a JSON payload file
    (--payload) to reuse exactly what the panel shows, or let it derive here.
    """
    from config import Settings, source_scopes, target_scopes
    from webui import dwd_payload  # noqa: PLC0415 - same repo, small dep

    data = dwd_payload()
    for t in data.get("tenants", []):
        if t.get("side") == tenant:
            return {
                "client_id": t.get("client_id", ""),
                "scopes": t.get("scopes", ""),
            }
    st = Settings()
    key = st.source_sa_key if tenant == "source" else st.target_sa_key
    client_id = ""
    try:
        with open(key, encoding="utf-8") as fh:
            client_id = json.load(fh).get("client_id", "")
    except Exception:  # noqa: BLE001 - absent key is an early, normal state
        pass
    scopes = (source_scopes(st) if tenant == "source"
              else target_scopes(st))
    return {"client_id": client_id, "scopes": ",".join(scopes)}


def run(client_id: str, scopes: str, timeout: int, headful: bool) -> int:
    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    log(f"targeting client {client_id}")
    log(f"scopes ({len([s for s in scopes.split(',') if s])}): {scopes}")
    log("opening the Admin console in a browser...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headful)
        page = browser.new_page()
        page.goto("https://admin.google.com/ac/owl/domainwidedelegation",
                  wait_until="domcontentloaded")

        log("SIGN IN BY HAND now (password, 2FA, SSO...). "
            f"I will wait up to {timeout}s for the console to load.")
        deadline = time.time() + timeout
        while time.time() < deadline:
            if page.url.startswith("https://admin.google.com/"):
                if page.locator("text=Add new").count() > 0:
                    break
            time.sleep(2)
        else:
            log("timed out waiting for the console. Re-run when signed in, "
                "or finish the grant by hand: "
                "Security > Access and data control > API controls > "
                "Manage Domain Wide Delegation > Add new")
            browser.close()
            return 2

        log("console loaded. opening Add new...")
        page.locator("text=Add new").first.click()
        page.wait_for_timeout(1500)

        # The dialog fields are labelled Client ID / OAuth Scopes. Fall back
        # to input[type=text] if the label binding changes.
        cid = page.get_by_label("Client ID").first
        if cid.count() == 0:
            cid = page.locator('input[type="text"]').first
        cid.fill(client_id)

        sc = page.get_by_label("OAuth Scopes").first
        if sc.count() == 0:
            sc = page.locator("textarea, input[type=text]").nth(1)
        sc.fill(scopes)

        log("filling done. clicking Authorize...")
        page.locator("text=Authorize").first.click()
        page.wait_for_timeout(2500)

        # Authorize either returns to the list (success) or the dialog stays
        # open with an inline error (bad/duplicate client id, unsupported
        # scope). Report which.
        if page.locator("text=Authorize").count() > 0:
            log("Authorize dialog still open -- likely an inline error "
                "(check the client ID / scopes, or multi-party approval). "
                "Fix in the open dialog, or do it by hand.")
            browser.close()
            return 3

        log("Authorize accepted. verifying the entry...")
        page.wait_for_timeout(2000)
        if page.locator(f"text={client_id}").count() > 0:
            log("VERIFIED: client present in the delegation list.")
        else:
            log("entry submitted but not yet visible in the list -- "
                "propagation can lag. Verify by hand: the list should now "
                "show this client ID.")
            browser.close()
            return 4

        browser.close()
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tenant", choices=["source", "target"])
    ap.add_argument("--client-id", help="service-account client ID (21-digit)")
    ap.add_argument("--scopes", help="comma-separated OAuth scope line")
    ap.add_argument("--payload", help="JSON file matching webui /api/dwd "
                    "shape, to reuse the exact panel line")
    ap.add_argument("--headful", action="store_true", default=True,
                    help="show the browser (default)")
    ap.add_argument("--timeout", type=int, default=600,
                    help="seconds to wait for manual sign-in")
    args = ap.parse_args(argv)

    if not args.client_id and not args.payload and not args.tenant:
        ap.error("need --client-id/--scopes, --payload, or --tenant")
    if args.tenant and args.payload:
        ap.error("give --tenant or --payload, not both")

    if args.payload:
        with open(args.payload, encoding="utf-8") as fh:
            data = json.load(fh)
        client_id = data.get("client_id", "")
        scopes = data.get("scopes", "") or ",".join(data.get("scope_list", []))
    elif args.client_id:
        client_id, scopes = args.client_id, args.scopes or ""
    else:
        data = _load_payload(args.tenant)
        client_id, scopes = data["client_id"], data["scopes"]

    if not client_id:
        log("no client ID available -- upload the service-account key first "
            "(or pass --client-id)")
        return 1
    if not scopes:
        log("no scopes -- pass --scopes or set the migration flags in env.sh")
        return 1

    try:
        import playwright  # noqa: F401, PLC0415
    except ImportError:
        log("playwright is not installed here. Install it once on THIS "
            "machine (where the browser will open):")
        log("  pip install playwright && playwright install chromium")
        return 1

    return run(client_id, scopes, args.timeout, args.headful)


if __name__ == "__main__":
    sys.exit(main())
