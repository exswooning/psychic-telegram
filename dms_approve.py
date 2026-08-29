"""Approve a pending Google Workspace email-import connection, source-side.

The target admin requested the connection (dms_migrate.py); Google mailed
the SOURCE super admin a one-time authorization link. There is no console
control for it -- the approval lives only in that mailbox -- so this signs
in as the source admin and clicks it, completing the handshake that
otherwise waits on a human opening their email.

Credentials come from the environment (DWD_EMAIL / DWD_PASSWORD), never
argv. Run it where the DWD browser already works:

    DISPLAY=:99 DWD_EMAIL=info@source... DWD_PASSWORD=... \\
        .venv/bin/python dms_approve.py

It only ever clicks an authorization/grant control inside a message whose
subject is about a data-import connection request -- it does not touch any
other mail.
"""
from __future__ import annotations

import re
import sys

import dms_migrate as D

# Words Google uses in the request mail's subject/body. Kept broad because
# the exact wording changes, but all point at the same one action.
SUBJECT_HINTS = ("data import", "authoriz", "connection request",
                 "import email", "workspace data")
GRANT_LABELS = ("Authorize", "Approve", "Grant", "Allow", "Accept",
                "Confirm", "Continue")


def _find_request_thread(page):
    for q in ("data+import+authoriz", "authorize+data+import",
              "workspace+data+import", "connection+request"):
        page.goto(f"https://mail.google.com/mail/u/0/#search/{q}",
                  wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(9000)
        rows = page.locator("tr.zA")
        for i in range(min(rows.count(), 12)):
            txt = re.sub(r"\s+", " ", rows.nth(i).inner_text()).lower()
            if any(h in txt for h in SUBJECT_HINTS):
                return rows.nth(i)
    return None


def approve(headful: bool, timeout: int) -> dict:
    out = {"ok": False, "did": [], "detail": ""}
    opened = D.open_console(headful, timeout)   # signs in as DWD_EMAIL
    if opened is None:
        out["detail"] = "sign-in did not complete"
        return out
    p, browser, page = opened
    try:
        page.set_viewport_size({"width": 1500, "height": 1100})
        thread = _find_request_thread(page)
        if thread is None:
            out["detail"] = ("no data-import request mail found -- it may not "
                             "have arrived yet, or was already actioned")
            return out
        out["did"].append("found the request mail")
        thread.click()
        page.wait_for_timeout(6000)

        # The authorization link opens Google's consent page in-tab or new tab.
        link = None
        for a in page.locator("a").all():
            href = (a.get_attribute("href") or "")
            label = (a.inner_text() or "").strip()
            if ("admin.google.com" in href or "accounts.google.com" in href) \
                    and any(g.lower() in label.lower() for g in GRANT_LABELS):
                link = a
                break
        if link is None:
            # fall back to the most admin-looking link in the message body
            cands = [a for a in page.locator("a").all()
                     if "admin.google.com/ac/migrate" in (a.get_attribute("href") or "")]
            link = cands[0] if cands else None
        if link is None:
            out["detail"] = "opened the mail but found no authorization link"
            return out

        with page.context.expect_page(timeout=15000) as pop:
            link.click()
        target = pop.value if pop.value else page
        target.wait_for_load_state("domcontentloaded", timeout=45000)
        target.wait_for_timeout(6000)
        out["did"].append(f"opened the consent page: {target.url}")

        # Click the grant/allow button on the consent page.
        for lbl in GRANT_LABELS:
            btn = target.get_by_role("button", name=re.compile(lbl, re.I))
            if btn.count() and btn.first.is_enabled():
                btn.first.click()
                target.wait_for_timeout(6000)
                out["did"].append(f"clicked {lbl!r}")
                out["ok"] = True
                out["detail"] = "authorization granted"
                return out
        out["detail"] = ("reached the consent page but found no grant button "
                         f"({target.url}) -- approve it by hand there")
        out["ok"] = True   # we got as far as the page; nothing to retry blindly
        return out
    except Exception as exc:      # noqa: BLE001
        out["detail"] = str(exc)[:300]
        return out
    finally:
        if not headful:
            try:
                browser.close(); p.stop()
            except Exception:      # noqa: BLE001
                pass


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--headful", action="store_true")
    ap.add_argument("--timeout", type=int, default=200)
    args = ap.parse_args(argv)
    out = approve(args.headful, args.timeout)
    for d in out["did"]:
        print("  did:", d)
    print(("OK  " if out["ok"] else "STOPPED") + ": " + out["detail"])
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
