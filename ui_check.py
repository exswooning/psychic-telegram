#!/usr/bin/env python3
"""
ui_check.py
===========
Drive the signed-in web UI and assert it is telling the truth.

Everything here was done by hand, repeatedly, and every repetition found
something: a page that renders only its nav, two surfaces reporting the same
fact 42x apart, a feature whose entire output was invisible. Hand-driving
also produced its own bugs -- guessed URLs reported as broken pages, a login
helper that silently skipped signing in and made every later assertion look
like a product failure.

Three checks, one sign-in:

  pages    every route in the router loads, renders more than the nav
           shell, and raises no JS error. Routes are parsed out of App.tsx
           rather than listed here, because a hardcoded list goes stale the
           moment someone adds a page -- and a guessed URL falls through to
           a catch-all that looks like a working, empty page.

  metrics  the two servers are asked the same question and their answers
           reconciled against the ledger. They legitimately differ --
           audit_log outlives id_mapping, so one counts every generation and
           the other only mapped users -- and the check is that each states
           which, not that they match.

  links    if REWRITE_DRIVE_LINKS ran, no migrated message still points at a
           source file. This is invisible by design: the mail looks the same
           either way, so nothing but reading the target proves it worked.

The credential is read in-process from the env file and never printed,
logged, or passed on a command line.

    python ui_check.py                     # all three
    python ui_check.py --only pages
    python ui_check.py --json ui_check.json
"""
from __future__ import annotations

import argparse
import base64
import email
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

ENV_FILE = os.getenv("BITPORT_UI_ENV", "/etc/bitport/ui.env")
NAV_ONLY = 400          # a body this short is the shell with no page in it


# ----------------------------------------------------------------- credential
def credential(path: str = ENV_FILE) -> tuple[str, str]:
    out: dict[str, str] = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip().strip('"').strip("'")
    email_ = out.get("BITPORT_EMAIL") or out.get("BITPORT_ADMIN_EMAIL") or ""
    password = out.get("BITPORT_PASSWORD") or out.get("BITPORT_ADMIN_PASSWORD") or ""
    if not password:
        raise SystemExit(f"no BITPORT_PASSWORD in {path}")
    return email_, password


# --------------------------------------------------------------------- routes
def routes_from_router(app_tsx: str | None = None) -> list[str]:
    """Every concrete path the SPA router serves.

    Parsed, not listed. A hardcoded list silently stops covering new pages,
    and a *guessed* path is worse than no coverage: the router's catch-all
    renders the nav with an empty body, so a wrong URL reports HTTP 200 and
    looks like a page that loads but shows nothing.
    """
    path = app_tsx or os.path.join(HERE, "migration-webui", "src", "App.tsx")
    if not os.path.isfile(path):
        return []
    src = open(path, encoding="utf-8").read()
    found = re.findall(r'path="(/[^"]*)"', src)
    return sorted({
        p for p in found
        if not p.endswith("*") and ":" not in p          # no params, no globs
        and p not in ("/", "/login", "/signup")           # pre-auth surfaces
    })


# --------------------------------------------------------------------- checks
def check_pages(pg, host: str, errs: list) -> dict:
    bad, seen = [], []
    for route in routes_from_router():
        errs.clear()
        try:
            resp = pg.goto(f"{host}/app{route}", wait_until="domcontentloaded",
                           timeout=30000)
            # Wait for content rather than sleeping a fixed 2.6s. /metrics
            # runs a 3.16s cold query behind its first paint, and a fixed
            # wait reported it as "renders nothing but the nav" -- a check
            # that cries wolf on a slow page teaches you to ignore it.
            body = ""
            for _ in range(12):
                pg.wait_for_timeout(700)
                body = pg.inner_text("body")
                if len(body) > NAV_ONLY:
                    break
            status = resp.status if resp else 0
            broken = [m for m in ("Something went wrong", "Unexpected Application Error",
                                  "Cannot read", "is not a function", "TypeError")
                      if m in body]
            why = None
            if status >= 400:
                why = f"http {status}"
            elif errs:
                why = f"js: {errs[0][:120]}"
            elif broken:
                why = broken[0]
            elif len(body) <= NAV_ONLY:
                why = f"renders nothing but the nav ({len(body)} chars)"
            seen.append({"route": route, "chars": len(body), "ok": why is None})
            if why:
                bad.append({"route": route, "why": why})
        except Exception as exc:                       # noqa: BLE001
            bad.append({"route": route, "why": str(exc)[:120]})
    return {"checked": len(seen), "failures": bad}


# Derived, not listed. A hardcoded set of "pages where actions live" goes
# stale exactly like a hardcoded route list -- and this file already has a
# test asserting no route literal appears in it, which the first version of
# this list promptly failed.


def check_actions(pg, session, host: str) -> dict:
    """Every action the server offers has a control somewhere in the UI.

    Found by hand: 43 offered, 27 with a button. The Python-side reachability
    test missed it because it checks STEP_ACTIONS, which drives the old webui
    wizard rather than this app -- two lists, one silently authoritative over
    what a person can actually click.

    Matched on JobRunner's own data-testid, never on the label. A first pass
    read labels out of body text and reported 43 of 43 visible while three
    were not: "Report" matched unrelated prose, and an action claimed by a
    page that never rendered it looked fine because some other word did.
    """
    offered = session.get(f"{host}/api/actions", timeout=60).json()
    seen: dict[str, str] = {}
    for route in routes_from_router():
        try:
            pg.goto(f"{host}/app{route}", wait_until="domcontentloaded", timeout=30000)
            pg.wait_for_timeout(2600)
            for tid in pg.eval_on_selector_all(
                    "[data-testid^='action-']",
                    "n=>n.map(e=>e.dataset.testid)"):
                key = tid[len("action-"):]
                # JobRunner also emits action-exit-/action-confirm-* on the
                # same card; only the trigger names the action itself.
                if key in offered:
                    seen.setdefault(key, route)
        except Exception:                              # noqa: BLE001
            continue
    missing = sorted(set(offered) - set(seen))
    return {
        "offered": len(offered),
        "visible": len(seen),
        "failures": ([f"{k} ({offered[k]['label']}) has no control on any page"
                      for k in missing]),
    }


def check_metrics(session, host: str) -> dict:
    """Both servers, same question, and whether each says what it counts."""
    m = session.get(f"{host}/api/v2/metrics", timeout=90).json()
    rep = (session.get(f"{host}/api/spa/report", timeout=90).json() or {}).get("report", {})
    vol = {r["itemType"]: r["count"] for r in (m.get("volume") or [])
           if r.get("status") == "SUCCESS"}
    mapped = {r["type"]: r["count"] for r in (m.get("mappings") or [])}
    scope = m.get("volumeScope") or {}

    problems = []
    if not scope.get("counts"):
        problems.append("/api/v2/metrics does not say what its volume counts")
    if not rep.get("scope"):
        problems.append("/api/spa/report does not say what its totals count")
    # The gap is expected; an *unexplained* gap is not.
    gap = vol.get("message", 0) - (rep.get("emailsMigrated") or 0)
    if gap and not scope.get("unmappedRows"):
        problems.append(
            f"the two surfaces differ by {gap:,} messages and nothing in the "
            f"payload explains it")
    return {
        "ledgerWide": {"messages": vol.get("message", 0),
                       "files": vol.get("file", 0)},
        "thisMigration": {"messages": rep.get("emailsMigrated"),
                          "files": rep.get("driveFilesMigrated")},
        "onTargetNow": {"messages": mapped.get("message", 0),
                        "files": mapped.get("file", 0)},
        "unmappedRows": scope.get("unmappedRows", 0),
        "failures": problems,
    }


def check_links(account_id: int | None) -> dict:
    """No migrated message may still name a source file id."""
    from config import Settings
    from db import MigrationDB
    from auth import AuthManager
    from link_rewrite import DRIVE_ID

    s = Settings(account_id=account_id) if account_id else Settings()
    db = MigrationDB(s.db_path)
    users = [r[0] for r in db.conn.execute(
        "SELECT DISTINCT source_user FROM audit_log WHERE item_type='link_rewrite'"
        " LIMIT 10")]
    if not users:
        return {"skipped": "no link_rewrite rows -- the feature has not run",
                "failures": []}

    auth = AuthManager(s)
    to_target, to_source = 0, []
    for u in users:
        g = auth.target_gmail(u.replace(s.source_domain, s.target_domain))
        msgs = g.users().messages().list(userId="me", maxResults=200
                                         ).execute().get("messages", [])
        for msg_ref in msgs:
            raw = g.users().messages().get(userId="me", id=msg_ref["id"],
                                           format="raw").execute().get("raw", "")
            parsed = email.message_from_bytes(base64.urlsafe_b64decode(raw + "==="))
            for part in parsed.walk():
                if part.get_content_maintype() != "text" or part.is_multipart():
                    continue
                for fid in {x.decode() for x in
                            DRIVE_ID.findall(part.get_payload(decode=True) or b"")}:
                    if db.conn.execute("SELECT 1 FROM id_mapping WHERE target_id=?"
                                       " LIMIT 1", (fid,)).fetchone():
                        to_target += 1
                    elif db.conn.execute("SELECT 1 FROM id_mapping WHERE source_id=?"
                                         " LIMIT 1", (fid,)).fetchone():
                        to_source.append(fid)
    return {
        "usersChecked": len(users),
        "linksAtTarget": to_target,
        "linksStillAtSource": len(to_source),
        "failures": ([f"{len(to_source)} link(s) still name a source file and "
                      f"will die with the source tenant"] if to_source else []),
    }


# ----------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default=os.getenv("BITPORT_PUBLIC_ORIGIN",
                                                "http://127.0.0.1:8080"))
    ap.add_argument("--only", action="append",
                    choices=["pages", "actions", "metrics", "links"],
                    help="default: all four")
    ap.add_argument("--account-id", type=int)
    ap.add_argument("--json", metavar="PATH")
    args = ap.parse_args(argv)
    wanted = set(args.only or ["pages", "actions", "metrics", "links"])

    import requests
    from playwright.sync_api import sync_playwright

    email_, password = credential()
    print(f"signing in as {email_ or '(email from env)'} at {args.host}")
    session = requests.Session()
    r = session.post(f"{args.host}/api/v2/auth/login",
                     json={"email": email_, "password": password}, timeout=60)
    if r.status_code != 200:
        raise SystemExit(f"login failed: HTTP {r.status_code}")

    out: dict = {}
    if "pages" in wanted or "actions" in wanted:
        with sync_playwright() as p:
            b = p.chromium.launch()
            ctx = b.new_context(viewport={"width": 1500, "height": 1000})
            for c in session.cookies:
                ctx.add_cookies([{"name": c.name, "value": c.value,
                                  "domain": c.domain, "path": c.path or "/"}])
            pg = ctx.new_page()
            errs: list = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            if "pages" in wanted:
                out["pages"] = check_pages(pg, args.host, errs)
            if "actions" in wanted:
                out["actions"] = check_actions(pg, session, args.host)
            ctx.close(); b.close()
    if "metrics" in wanted:
        out["metrics"] = check_metrics(session, args.host)
    if "links" in wanted:
        out["links"] = check_links(args.account_id)

    failures = []
    for name, res in out.items():
        fails = res.get("failures") or []
        mark = "FAIL" if fails else "ok  "
        detail = ""
        if name == "pages":
            detail = f"{res['checked']} route(s)"
        elif name == "actions":
            detail = f"{res['visible']} of {res['offered']} have a control"
        elif name == "metrics":
            detail = (f"ledger {res['ledgerWide']['messages']:,} / "
                      f"migration {res['thisMigration']['messages']} / "
                      f"on target {res['onTargetNow']['messages']:,} messages")
        elif name == "links":
            detail = res.get("skipped") or (
                f"{res.get('linksAtTarget', 0)} at target, "
                f"{res.get('linksStillAtSource', 0)} still at source")
        print(f"  {mark} {name:8s} {detail}")
        for f in fails:
            print(f"        - {f if isinstance(f, str) else f['route'] + ': ' + f['why']}")
        failures += fails

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2)
        print(f"wrote {args.json}")
    print(f"\n{'FAILED' if failures else 'PASSED'} "
          f"({len(failures)} finding(s))")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
