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
SPINNER = '[role="progressbar"]'


def settle(pg) -> str:
    """Wait for content, not a fixed sleep, and believe a page that says it
    is still working.

    A page mid-render has a body of nav text only. Waiting a fixed 2.6s
    called that "renders nothing but the nav" -- the description of a broken
    page rather than a slow one -- and, in the actions check, made the answer
    depend on timing: three consecutive runs reported 41, 41 and 42 of 43,
    naming a different missing action each time.

    So: wait up to 8.4s for content, then keep waiting only while a progress
    indicator is on screen. A page with no spinner and no content has nothing
    left to say.

    Do not read a page's stall here as an endpoint's cost. Measured
    server-side, /api/v2/metrics is p50 1.04s over 80 sequential fresh
    requests and never once exceeded 8s; the multi-second figures only ever
    appeared in a browser holding several requests at once.
    """
    body = ""
    for i in range(24):
        pg.wait_for_timeout(700)
        body = pg.inner_text("body")
        if len(body) > NAV_ONLY:
            return body
        if i >= 11 and not pg.locator(SPINNER).count():
            break
    return body


def check_pages(pg, host: str, errs: list) -> dict:
    bad, seen, notes = [], [], []
    # Without these, an empty page is reported as "renders nothing" with no
    # cause, and finding the cause costs a manual Playwright session -- which
    # it did, twice. A page that renders nothing did so for a reason, and the
    # reason is nearly always a request that never arrived.
    net: list = []
    pg.on("response", lambda r: net.append(f"http {r.status} {r.url.split('?')[0][-60:]}")
          if r.status >= 400 else None)
    pg.on("requestfailed", lambda r: net.append(
        f"failed {(r.failure or '?')} {r.url.split('?')[0][-60:]}"))
    # Pending is tracked separately because requestfailed does NOT fire for a
    # request that simply never finishes, and "no failed requests" on a blank
    # page is a misleading all-clear -- it was the thing that made the first
    # blank page look causeless.
    pending: dict = {}
    pg.on("request", lambda r: pending.__setitem__(r, r.url.split("?")[0][-55:]))
    for _ev in ("requestfinished", "requestfailed"):
        pg.on(_ev, lambda r: pending.pop(r, None))

    for route in routes_from_router():
        errs.clear()
        net.clear()
        pending.clear()
        try:
            resp = pg.goto(f"{host}/app{route}", wait_until="domcontentloaded",
                           timeout=30000)
            body = settle(pg)
            # One reload before calling a page broken. An empty page that
            # comes back on reload is a different defect from one that never
            # renders, and reporting them as the same thing sent me looking
            # for a bug in a page that was fine.
            flaky = None
            if len(body) <= NAV_ONLY:
                first = (f"{len(body)} chars, "
                         f"{'; '.join(net[:2]) or 'no failed requests'}, "
                         f"pending: {'; '.join(list(pending.values())[:3]) or 'none'}")
                errs.clear(); net.clear()
                pg.reload(wait_until="domcontentloaded", timeout=30000)
                body = settle(pg)
                if len(body) > NAV_ONLY:
                    # Reported, not failed. Every observed instance was in a
                    # session that was deploying repeatedly, and sync_vps
                    # restarts both services: a request in flight across a
                    # restart hangs at Caddy with no error to report, which
                    # is exactly the shape seen here, and one run caught the
                    # 502 outright. Two consecutive runs on a settled server
                    # produced none. Kept as a note because that is an
                    # explanation, not a proof, and because failing the run
                    # on it would make this check cry wolf about 40% of the
                    # time -- a check people learn to ignore is worse than
                    # no check.
                    flaky = f"empty on first load ({first}), fine after reload"
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
                spinning = ", still spinning" if pg.locator(SPINNER).count() else ""
                why = (f"renders nothing but the nav ({len(body)} chars"
                       f"{spinning}) -- "
                       f"{'; '.join(net[:3]) or 'no failed requests'}, "
                       f"pending: {'; '.join(list(pending.values())[:3]) or 'none'}")
            if flaky:
                notes.append({"route": route, "why": flaky})
            seen.append({"route": route, "chars": len(body), "ok": why is None})
            if why:
                bad.append({"route": route, "why": why})
        except Exception as exc:                       # noqa: BLE001
            bad.append({"route": route, "why": str(exc)[:120]})
    return {"checked": len(seen), "failures": bad, "notes": notes}


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
            settle(pg)
            # Then wait for the set to stop growing. Controls that depend on
            # loaded data (Job control's Migrate needs its user list) appear
            # after the first paint, so scanning once at any fixed moment
            # answers a different question each run.
            found: set = set()
            scanned = False
            for _ in range(8):
                now = set(pg.eval_on_selector_all(
                    "[data-testid^='action-']", "n=>n.map(e=>e.dataset.testid)"))
                # Two identical scans is settled, including two empty ones.
                # Requiring a non-empty match made every action-less page --
                # more than half of them -- burn the full 6.4s for nothing,
                # which is most of why a run took a quarter of an hour.
                if scanned and now == found:
                    break
                found |= now
                scanned = True
                pg.wait_for_timeout(800)
            for tid in found:
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


# Where each server-side run setting is controlled from. The point is not
# the mapping, it is that _RUN_STATE is read live below and any key missing
# from here is a finding: a setting the server acts on that the app can
# neither show nor change. That is how dry_run got in -- it silently
# governed every one of the 43 actions and had no control anywhere.
SETTING_HOMES = {
    "dry_run":             ("/mission-control", "dry-run"),
    "mail_transport":      ("/mission-control", "transport-split"),
    "rewrite_drive_links": ("/mission-control", "rewrite-drive-links"),
    "delta_days":          ("/mission-control", "delta-days"),
    "redo_unrewritten_links": ("/mission-control", "redo-unrewritten"),
    # run-users, NOT Job control's row ticks. Those scope api_server's
    # migrate/start and never touch _RUN_STATE, so pointing this at them
    # made the check pass while the setting it names had no control at all.
    "users":               ("/mission-control", "run-users"),
    "services":            ("/services", "toggle-drive"),
}


def check_settings(pg, host: str) -> dict:
    """Every setting the server acts on can be seen and set in the app."""
    import webui

    problems = []
    unhomed = sorted(set(webui._RUN_STATE) - set(SETTING_HOMES))
    for k in unhomed:
        problems.append(f"{k} is in the server's run state with no control anywhere")

    by_route: dict[str, list] = {}
    for key, (route, testid) in SETTING_HOMES.items():
        if key in webui._RUN_STATE:
            by_route.setdefault(route, []).append((key, testid))

    found = 0
    for route, wanted in by_route.items():
        pg.goto(f"{host}/app{route}", wait_until="domcontentloaded", timeout=30000)
        settle(pg)
        for _ in range(8):
            if all(pg.locator(f'[data-testid="{t}"]').count() for _, t in wanted):
                break
            pg.wait_for_timeout(800)
        for key, testid in wanted:
            if pg.locator(f'[data-testid="{testid}"]').count():
                found += 1
            else:
                problems.append(f"{key}: nothing on {route} matches [data-testid={testid}]")

    return {"covered": found, "offered": len(webui._RUN_STATE), "failures": problems}


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



def check_duplicates(account_id: int | None) -> dict:
    """A link repair must leave one live message, not two.

    This exists because a person asked the question and no test did. The
    repair trashes the copy it replaces, and that copy keeps the same
    Message-ID -- which the engine's own duplicate guard searches for with
    includeSpamTrash on. Get it wrong and you have two emails of the same
    thing carrying two different links, with the ledger naming one of them
    arbitrarily. That is invisible in every count: the ledger has one row,
    verify passes because a surplus is allowed, and only the person reading
    their mail sees it.

    So it asks the target directly, per repaired message: how many copies
    carry this Message-ID, and how many of those are not in Trash?
    """
    from config import Settings
    from db import MigrationDB
    from auth import AuthManager

    s = Settings(account_id=account_id) if account_id else Settings()
    db = MigrationDB(s.db_path)
    repaired = db.conn.execute(
        "SELECT source_user, item_id FROM audit_log "
        "WHERE item_type='link_repair' ORDER BY timestamp DESC LIMIT 60"
    ).fetchall()
    if not repaired:
        return {"skipped": "no repairs recorded -- the redo pass has not run",
                "failures": []}

    auth = AuthManager(s)
    checked = live_dupes = orphaned = 0
    problems: list[str] = []
    handles: dict = {}
    for source_user, src_id in repaired:
        row = db.conn.execute(
            "SELECT target_id FROM id_mapping WHERE source_user=? AND "
            "source_id=? AND type='message'", (source_user, src_id)).fetchone()
        if not row:
            # Trashed and forgotten but never reinserted: the repair lost the
            # message rather than duplicating it. The opposite failure, and
            # just as invisible.
            orphaned += 1
            continue
        if source_user not in handles:
            handles[source_user] = auth.target_gmail(
                source_user.replace(s.source_domain, s.target_domain))
        g = handles[source_user]
        raw = g.users().messages().get(userId="me", id=row[0], format="raw"
                                       ).execute().get("raw", "")
        parsed = email.message_from_bytes(base64.urlsafe_b64decode(raw + "==="))
        msgid = parsed.get("Message-ID")
        if not msgid:
            continue
        found = g.users().messages().list(
            userId="me", q=f"rfc822msgid:{msgid.strip('<>')}", maxResults=10,
            includeSpamTrash=True).execute().get("messages", [])
        checked += 1
        live = [m["id"] for m in found
                if "TRASH" not in (g.users().messages().get(
                    userId="me", id=m["id"], format="minimal"
                ).execute().get("labelIds") or [])]
        if len(live) > 1:
            live_dupes += 1
            problems.append(
                f"{source_user} has {len(live)} live copies of {msgid} -- "
                f"the repair duplicated the message instead of replacing it")
    # Duplicates that have nothing to do with a repair. The first real one
    # found on this tenant was ten live copies of the same seeded message,
    # each with its own ledger row -- so every count agreed and only the
    # mailbox disagreed. Sampling the live mailbox for repeated Message-IDs
    # is the only view that catches it, and it costs one metadata call per
    # message rather than a full fetch.
    for source_user in list(handles) or [r[0] for r in repaired[:1]]:
        if source_user not in handles:
            handles[source_user] = auth.target_gmail(
                source_user.replace(s.source_domain, s.target_domain))
        g = handles[source_user]
        seen: dict = {}
        for ref in (g.users().messages().list(
                userId="me", maxResults=200).execute().get("messages") or []):
            md = g.users().messages().get(
                userId="me", id=ref["id"], format="metadata",
                metadataHeaders=["Message-ID"]).execute()
            hdrs = {h["name"].lower(): h["value"]
                    for h in (md.get("payload", {}).get("headers") or [])}
            mid_hdr = hdrs.get("message-id")
            if mid_hdr:
                seen.setdefault(mid_hdr, []).append(ref["id"])
        repeats = {k: v for k, v in seen.items() if len(v) > 1}
        if repeats:
            worst = max(repeats.values(), key=len)
            problems.append(
                f"{source_user}: {len(repeats)} Message-ID(s) have more than "
                f"one live copy on the target (worst: {len(worst)} copies) -- "
                f"the ledger counts each as a separate message, so only the "
                f"mailbox shows it")

    if orphaned:
        problems.append(
            f"{orphaned} repaired message(s) have no mapping: trashed and "
            f"forgotten but never reinserted")
    return {
        "repairsChecked": checked,
        "liveDuplicates": live_dupes,
        "orphaned": orphaned,
        "failures": problems,
    }


# ----------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default=os.getenv("BITPORT_PUBLIC_ORIGIN",
                                                "http://127.0.0.1:8080"))
    ap.add_argument("--only", action="append",
                    choices=["pages", "actions", "settings", "metrics", "links",
                             "duplicates"],
                    help="default: all six")
    ap.add_argument("--account-id", type=int)
    ap.add_argument("--json", metavar="PATH")
    args = ap.parse_args(argv)
    wanted = set(args.only or ["pages", "actions", "settings", "metrics",
                               "links", "duplicates"])

    import requests
    from playwright.sync_api import sync_playwright

    email_, password = credential()
    print(f"signing in as {email_ or '(email from env)'} at {args.host}")
    session = requests.Session()
    r = session.post(f"{args.host}/api/v2/auth/login",
                     json={"email": email_, "password": password}, timeout=60)
    if r.status_code != 200:
        raise SystemExit(f"login failed: HTTP {r.status_code}")

    # Always, and never skippable with --only: a result is worthless if you
    # cannot say which code produced it. This reads the commit from the
    # running process, not from disk, so a deploy that copied files but did
    # not restart reports the OLD commit and the mismatch is visible.
    commit = "unknown"
    try:
        commit = (session.get(f"{args.host}/api/version", timeout=30)
                  .json().get("commit", "unknown"))
    except Exception as exc:                       # noqa: BLE001
        commit = f"unreadable ({exc.__class__.__name__})"
    print(f"checking deployed commit {commit}")
    version_fails = []
    if commit.endswith("-dirty"):
        version_fails.append(
            f"deployed commit {commit} is dirty -- the box is running code "
            f"that matches no commit, so nothing found here is reproducible")
    elif commit in ("unknown", "") or commit.startswith("unreadable"):
        version_fails.append(f"cannot tell what is deployed ({commit})")

    out: dict = {"version": {"commit": commit, "failures": version_fails}}
    if wanted & {"pages", "actions", "settings"}:
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
            if "settings" in wanted:
                out["settings"] = check_settings(pg, args.host)
            ctx.close(); b.close()
    if "metrics" in wanted:
        out["metrics"] = check_metrics(session, args.host)
    if "links" in wanted:
        out["links"] = check_links(args.account_id)
    if "duplicates" in wanted:
        out["duplicates"] = check_duplicates(args.account_id)

    failures = []
    for name, res in out.items():
        fails = res.get("failures") or []
        mark = "FAIL" if fails else "ok  "
        detail = ""
        if name == "version":
            detail = commit
        elif name == "pages":
            detail = f"{res['checked']} route(s)"
        elif name == "actions":
            detail = f"{res['visible']} of {res['offered']} have a control"
        elif name == "settings":
            detail = f"{res['covered']} of {res['offered']} have a control"
        elif name == "metrics":
            detail = (f"ledger {res['ledgerWide']['messages']:,} / "
                      f"migration {res['thisMigration']['messages']} / "
                      f"on target {res['onTargetNow']['messages']:,} messages")
        elif name == "links":
            detail = res.get("skipped") or (
                f"{res.get('linksAtTarget', 0)} at target, "
                f"{res.get('linksStillAtSource', 0)} still at source")
        elif name == "duplicates":
            detail = res.get("skipped") or (
                f"{res.get('repairsChecked', 0)} repair(s), "
                f"{res.get('liveDuplicates', 0)} duplicated, "
                f"{res.get('orphaned', 0)} lost")
        print(f"  {mark} {name:8s} {detail}")
        for f in fails:
            print(f"        - {f if isinstance(f, str) else f['route'] + ': ' + f['why']}")
        for n in res.get("notes") or []:
            print(f"        ~ {n['route']}: {n['why']}")
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
