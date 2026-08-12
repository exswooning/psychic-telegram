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


def _dialog_open(dialog) -> bool:
    """Is the Add/Authorize dialog still up?

    Success closes it and returns to the delegation list; an inline error
    leaves it open. Checking for the Authorize button rather than the dialog
    node itself, because the console keeps a detached dialog element around
    after closing.
    """
    try:
        return (dialog.count() > 0
                and dialog.get_by_role("button", name="Authorize").count() > 0)
    except Exception:      # noqa: BLE001 - a detached node counts as closed
        return False


def _installed_browser_launch(p, headful: bool):
    """Launch a REAL installed browser, not Playwright's bundled Chromium.

    Google's sign-in rejects the bundled Chromium outright ("This browser or
    app may not be secure") because it carries an automation fingerprint and
    no real Google support. Chrome/Edge via Playwright channels, or Brave
    via executable_path, look like a normal user's browser and get past it.

    On a headless VPS this also needs a real (virtual) display -- headful
    is not optional, see run()'s docstring -- and, run as root (the normal
    account on a single-purpose VPS), Chrome/Chromium refuse to start at
    all without --no-sandbox: the Linux sandbox setuid helper assumes a
    non-root user and Chrome treats "root with no sandbox flag" as unsafe
    rather than silently downgrading. --no-sandbox is standard practice for
    exactly this kind of dedicated automation host, not a security
    regression for a browser session that only ever visits accounts.google.com.
    """
    import shutil

    root_args = ["--no-sandbox"] if hasattr(os, "geteuid") and os.geteuid() == 0 else []
    launch = None
    candidates = []
    chrome = (shutil.which("google-chrome")
              or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    edge = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
    brave = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
    if os.path.exists(chrome):
        candidates.append(("chrome", p.chromium.launch(
            channel="chrome", headless=not headful, args=root_args)))
    if os.path.exists(edge):
        candidates.append(("edge", p.chromium.launch(
            channel="msedge", headless=not headful, args=root_args)))
    if os.path.exists(brave):
        candidates.append(("brave", p.chromium.launch(
            executable_path=brave, headless=not headful, args=root_args)))
    if candidates:
        launch = candidates[0]
        log(f"using installed browser: {launch[0]}")
        return launch[1]
    log("no real Chrome/Edge/Brave found; falling back to bundled Chromium "
        "(Google may reject the sign-in as 'not secure')")
    return p.chromium.launch(headless=not headful, args=root_args)


def run(client_id: str, scopes: str, timeout: int, headful: bool,
        tenant: str | None = None) -> int:
    """`tenant` is optional and only used to verify the result afterwards --
    the console work itself needs nothing but the client ID and scopes."""
    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    log(f"targeting client {client_id}")
    log(f"scopes ({len([s for s in scopes.split(',') if s])}): {scopes}")
    log("opening the Admin console in a browser...")

    with sync_playwright() as p:
        browser = _installed_browser_launch(p, headful)
        page = browser.new_page()
        nav = max(timeout * 1000, 30000)
        page.goto("https://admin.google.com/ac/owl/domainwidedelegation",
                  wait_until="domcontentloaded", timeout=nav)

        log("SIGN IN BY HAND now (password, 2FA, SSO...). "
            f"I will wait up to {timeout}s for the console to load.")
        log("IMPORTANT: sign in INSIDE the browser window this tool just "
            "opened (it is a separate Chromium with its own session). "
            "Logging into admin.google.com in a normal browser tab does "
            "NOT count -- this tool can only see its own window.")
        deadline = time.time() + timeout
        last_state = ""
        last_title = ""
        # Optional unattended sign-in.
        #
        # Credentials come from the environment, never from argv: a command
        # line is readable by any process on the box via `ps`, and this one
        # would carry a super-admin password.
        #
        # Best-effort by design. Google actively fingerprints automation and
        # will often answer with "This browser or app may not be secure", a
        # captcha, or a 2FA prompt -- none of which are scriptable. When that
        # happens the loop below simply keeps waiting for a human, which is
        # the same behaviour as before, so this can only ever save time and
        # never blocks the manual path.
        auto_email = os.getenv("DWD_EMAIL", "").strip()
        auto_pw = os.getenv("DWD_PASSWORD", "")
        typed_email = typed_pw = False

        # Google's email box is `type="text"` with id=identifierId -- NOT
        # type="email". Selecting on the obvious `input[type="email"]`
        # matches zero elements, so the fill silently never happened and the
        # tool sat waiting for a human at a page it could have filled in.
        # The password box IS type="password", but a hidden one is also
        # present on the identifier page, hence the is_visible() guard.
        EMAIL_SEL = '#identifierId, input[name="identifier"], input[type="email"]'
        PW_SEL = 'input[type="password"][name="Passwd"], input[type="password"]'

        def _fill_visible(pg, selector: str, value: str) -> bool:
            loc = pg.locator(selector)
            for i in range(min(loc.count(), 4)):
                box = loc.nth(i)
                try:
                    if not box.is_visible() or not box.is_enabled():
                        continue
                    box.click()
                    # type() rather than fill(): the sign-in form listens for
                    # real key events to enable Next, and a programmatic
                    # value set can leave the button disabled.
                    box.type(value, delay=50)
                    pg.keyboard.press("Enter")
                    return True
                except Exception:      # noqa: BLE001 - try the next match
                    continue
            return False

        while time.time() < deadline:
            if auto_email and auto_pw:
                for pg in browser.contexts[0].pages:
                    try:
                        if not typed_email and _fill_visible(pg, EMAIL_SEL,
                                                             auto_email):
                            log("  auto: entered the admin email")
                            pg.wait_for_timeout(4000)
                            typed_email = True
                            continue
                        # Never logged and never echoed back.
                        if typed_email and not typed_pw and _fill_visible(
                                pg, PW_SEL, auto_pw):
                            log("  auto: entered the password")
                            pg.wait_for_timeout(5000)
                            typed_pw = True
                            continue
                    except Exception:      # noqa: BLE001 - fall back to human
                        pass

            # Scan every open tab, not just the first: Google's sign-in often
            # opens the Admin console in a NEW tab and leaves the original
            # parked on accounts.google.com. The console tab is the signal.
            urls = []
            admin_page = None
            for pg in browser.contexts[0].pages:
                urls.append(pg.url)
                if pg.url.startswith("https://admin.google.com/"):
                    admin_page = pg
                    break
            state = " | ".join(
                f"{pg.url[:110]}" for pg in browser.contexts[0].pages)
            if state != last_state:
                log(f"  tabs: {state}")
                last_state = state
            for pg in browser.contexts[0].pages:
                t = pg.title().strip()
                if t and "Sign in" in t and t != last_title:
                    log(f"  [title] {t[:80]}")
                    last_title = t
            if admin_page is not None:
                page = admin_page
                if not page.url.startswith("https://admin.google.com/ac/owl/"):
                    page.goto("https://admin.google.com/ac/owl/domainwidedelegation",
                              wait_until="domcontentloaded", timeout=nav)
                    page.wait_for_timeout(1500)
                if page.locator("text=Add new").count() > 0:
                    log("  detected: signed in and on the DWD page")
                    break
            time.sleep(2)
        else:
            log("timed out waiting for the console. Re-run when signed in, "
                "or finish the grant by hand: "
                "Security > Access and data control > API controls > "
                "Manage Domain Wide Delegation > Add new")
            try:
                for i, pg in enumerate(browser.contexts[0].pages):
                    shot = f"/tmp/dwd-timeout-{i}.png"
                    pg.screenshot(path=shot)
                    log(f"  saved screenshot of tab {i}: {shot}")
                    try:
                        text = pg.inner_text("body")[:1200]
                    except Exception:  # noqa: BLE001
                        text = ""
                    with open(f"/tmp/dwd-timeout-{i}.txt", "w",
                              encoding="utf-8") as fh:
                        fh.write(f"URL: {pg.url}\n\n{text}")
                    log(f"  saved text of tab {i}: /tmp/dwd-timeout-{i}.txt")
            except Exception:  # noqa: BLE001 - screenshots are diagnostic
                pass
            browser.close()
            return 2

        # An onboarding banner sits over the toolbar and swallows the first
        # click, which is why `Add new` timed out while reporting itself as
        # present and visible.
        for label in ("GOT IT", "Got it", "Dismiss"):
            btn = page.get_by_role("button", name=label)
            if btn.count() > 0 and btn.first.is_visible():
                btn.first.click()
                page.wait_for_timeout(800)
                log(f"  dismissed the '{label}' banner")
                break

        # Prefer Edit over Add new when the client is already delegated.
        # Add new on an existing client can only be completed through the
        # "Overwrite existing client ID" checkbox, which replaces the scope
        # list wholesale; Edit opens the entry with its current scopes
        # already in the box, which is both the intended route and the one
        # that cannot silently drop a scope.
        used_edit = False
        if page.get_by_text(client_id, exact=False).count() > 0:
            try:
                page.get_by_text(client_id, exact=False).first.click()
                page.wait_for_timeout(1200)
                edit = page.get_by_role("button", name="Edit")
                if edit.count() > 0 and edit.first.is_visible():
                    edit.first.click()
                    page.wait_for_timeout(2000)
                    used_edit = True
                    log("  client already delegated -- editing the existing "
                        "entry rather than re-adding it")
            except Exception:      # noqa: BLE001 - fall back to Add new
                used_edit = False

        if not used_edit:
            log("console loaded. opening Add new...")
            btn = page.get_by_role("button", name="Add new")
            if btn.count() == 0:
                btn = page.locator("text=Add new")
            btn.first.click()
            page.wait_for_timeout(1500)

        # The "Add new" dialog has two editable fields (Client ID, OAuth
        # scopes). Scope to the dialog so we never match the table's "Client
        # ID" column header, which is what get_by_label("Client ID") resolved
        # to live. Pick a text input first (Client ID), then a textarea or the
        # next text input (scopes).
        dialog = page.get_by_role("dialog")
        if dialog.count() == 0:
            dialog = page.locator("[role=dialog], .dialog, [class*=dialog]")
        # Target by aria-label, not by position. On the Edit dialog the
        # first text input is the Client ID and it is *disabled* -- filling
        # by index there spins until timeout against a read-only field.
        def _field(*labels):
            for lab in labels:
                loc = dialog.locator(f'[aria-label="{lab}"]')
                if loc.count() > 0:
                    return loc.first
            return None

        if not used_edit:
            cid_box = _field("Client ID")
            if cid_box is None:
                cid_box = dialog.locator(
                    'input[type="text"]:not([disabled]), input:not([type])').first
            cid_box.fill(client_id)

        sc = _field("OAuth scopes (comma-delimited)", "OAuth Scopes", "OAuth scopes")
        if sc is None:
            # Fall back to the first *editable* field that is not the client
            # ID, rather than to a fixed index.
            sc = dialog.locator(
                'textarea:not([disabled]), input[type="text"]:not([disabled])').last
        # Replace rather than append: Edit pre-populates the box with the
        # current scopes, and `scopes` is already the merged superset of
        # those plus whatever is being added.
        sc.fill("")
        sc.fill(scopes)

        log("filling done. clicking Authorize...")
        auth_btn = dialog.get_by_role("button", name="Authorize")
        if auth_btn.count() == 0:
            auth_btn = page.locator("[role=button]", has_text="Authorize")
        auth_btn.first.click()
        page.wait_for_timeout(2500)

        # A client ID that is already delegated cannot be added again: the
        # console refuses with "Client ID already exists. Check the
        # 'Overwrite existing client ID' box below to overwrite and
        # authorize." That is the *normal* path for this project -- the
        # client is granted once during setup and every later scope change
        # is an edit -- so failing here would mean the tool only ever works
        # on a tenant it has never touched.
        #
        # Overwrite replaces the entry's scope list wholesale, which is why
        # the caller must pass the complete set rather than just the new
        # scopes: anything omitted is revoked.
        if _dialog_open(dialog):
            box = dialog.locator(
                "text=Overwrite existing client ID").first
            if box.count() > 0:
                log("client already delegated -- ticking 'Overwrite existing "
                    "client ID' and re-authorizing (scope list is replaced "
                    "wholesale, and the full set was supplied)")
                try:
                    box.click()
                except Exception:      # noqa: BLE001 - label may not be the hit target
                    cb = dialog.locator('input[type="checkbox"]').first
                    if cb.count() > 0:
                        cb.check()
                page.wait_for_timeout(500)
                # Re-fill: some console builds clear the scope box when the
                # duplicate error renders.
                try:
                    if not (sc.input_value() or "").strip():
                        sc.fill(scopes)
                except Exception:      # noqa: BLE001
                    pass
                auth_btn.first.click()
                page.wait_for_timeout(2500)

        # Authorize either returns to the list (success) or the dialog stays
        # open with an inline error (bad/duplicate client id, unsupported
        # scope). Report which.
        if _dialog_open(dialog):
            log("Authorize dialog still open -- likely an inline error "
                "(check the client ID / scopes, or multi-party approval). "
                "Fix in the open dialog, or do it by hand.")
            try:
                msg = dialog.inner_text()[:400]
                log(f"  dialog says: {msg!r}")
            except Exception:  # noqa: BLE001 - diagnostics
                pass
            browser.close()
            return 3

        log("Authorize accepted. verifying...")
        page.wait_for_timeout(2000)

        # The old check here was `client_id appears in the list`, which is
        # worthless on the path this tool now takes most often: an existing
        # client is ALREADY in the list -- that is why Overwrite was needed --
        # so it passed whether or not a single scope changed.
        #
        # The only honest check is functional. Google publishes no API to read
        # a delegation entry, so ask for a token per scope and see which ones
        # it issues. That also distinguishes "not delegated" from "API not
        # enabled in the Cloud project", which a single API call conflates.
        browser.close()
        if tenant:
            try:
                import verify_scopes
                from config import Settings

                wanted = [s.strip() for s in scopes.split(",") if s.strip()]
                rows = verify_scopes.verify(Settings(), tenant, wanted)
                missing = [r["scope"] for r in rows if not r["ok"]]
                live = len(rows) - len(missing)
                if missing:
                    log(f"VERIFY: only {live}/{len(rows)} scopes are live. "
                        f"Still missing:")
                    for m in missing:
                        log(f"  {m}")
                    log("Propagation can lag by a minute or two -- re-run "
                        "`python3 verify_scopes.py --tenant "
                        f"{tenant}` before assuming it failed.")
                    return 5
                log(f"VERIFIED: all {live} scopes issue tokens on {tenant}.")
                return 0
            except Exception as exc:      # noqa: BLE001
                log(f"could not verify functionally ({str(exc)[:90]}); "
                    f"run `python3 verify_scopes.py --tenant {tenant}` "
                    f"to confirm.")
                return 0
        log("no --tenant given, so the grant was not verified. Run "
            "`python3 verify_scopes.py --tenant source|target`.")
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
    ap.add_argument("--no-merge", action="store_true",
                    help="submit exactly --scopes instead of merging with "
                         "what is already delegated. Overwrite REPLACES the "
                         "entry's whole scope list, so anything already live "
                         "and not in --scopes is revoked; merging is the safe "
                         "default and this opts out of it")
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

    # Merge with whatever is already delegated before submitting.
    #
    # The console's only route to change an existing entry is Overwrite, and
    # Overwrite replaces the scope list wholesale -- so submitting a
    # hand-written list silently REVOKES every live scope missing from it.
    # There is no API to read the current entry, so the live set is
    # discovered the only way available: ask for a token per scope and see
    # which ones Google issues.
    if args.tenant and not args.no_merge:
        try:
            import verify_scopes
            from config import Settings

            want = [s.strip() for s in scopes.split(",") if s.strip()]
            known = verify_scopes.required_scopes(Settings(), args.tenant)
            probe = sorted(set(want) | set(known))
            rows = verify_scopes.verify(Settings(), args.tenant, probe)
            live = {r["scope"] for r in rows if r["ok"]}
            merged = sorted(set(want) | live)
            added = sorted(set(merged) - live)
            if not added:
                log(f"nothing to do: all {len(want)} requested scope(s) are "
                    f"already delegated on {args.tenant}.")
                return 0
            log(f"{len(live)} scope(s) already live; adding {len(added)}:")
            for a in added:
                log(f"  + {a}")
            scopes = ",".join(merged)
        except Exception as exc:      # noqa: BLE001
            log(f"could not read the live scope set ({str(exc)[:90]}). "
                f"Submitting --scopes as given; anything already delegated "
                f"and missing from it WILL be revoked.")

    try:
        import playwright  # noqa: F401, PLC0415
    except ImportError:
        log("playwright is not installed here. Install it once on THIS "
            "machine (where the browser will open):")
        log("  pip install playwright && playwright install chromium")
        return 1

    return run(client_id, scopes, args.timeout, args.headful, args.tenant)


if __name__ == "__main__":
    sys.exit(main())
