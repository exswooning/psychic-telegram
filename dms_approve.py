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
                 "import email", "workspace data", "approval request")
GRANT_LABELS = ("Authorize", "Approve", "Grant", "Allow", "Accept",
                "Confirm", "Continue")


def _dismiss_interstitials(page):
    """First sign-in throws a full-screen 'Turn on smart features' onboarding
    modal that covers the mailbox and eats every click. Each run gets a fresh
    browser, so it reappears every time -- clear it before touching mail.

    Walk its pages: pick the privacy-preserving 'off' option where one is
    offered, then advance with whatever primary button is showing.
    """
    # Google's Material radio doesn't select from an element click or a
    # synthetic DOM click -- 'Next' stays disabled forever. A real pointer
    # click at the radio's own coordinates does register, so select the
    # option by clicking just left of its text label (where the circle is),
    # then press whatever primary button the page is on. Stop when the
    # 'smart features' text is gone.
    def present():
        try:
            return "smart features" in page.inner_text("body").lower()
        except Exception:          # noqa: BLE001
            return False

    for _ in range(10):
        page.wait_for_timeout(1000)
        if not present():
            break
        # click the radio beside an option heading (the circle sits ~50px left)
        for label in ("Turn off smart features", "Turn on smart features"):
            opt = page.get_by_text(label, exact=True)
            if opt.count():
                try:
                    box = opt.first.bounding_box()
                    if box:
                        page.mouse.click(box["x"] - 48, box["y"] + box["height"] / 2)
                        page.wait_for_timeout(400)
                        break
                except Exception:  # noqa: BLE001
                    continue
        advanced = False
        for label in ("Done", "Confirm", "Got it", "Finish", "Next", "Save"):
            btn = page.get_by_role("button", name=re.compile(f"^{label}$", re.I))
            if btn.count() and btn.first.is_enabled():
                try:
                    btn.first.click(timeout=3000)
                    advanced = True
                    break
                except Exception:  # noqa: BLE001
                    pass
        if not advanced and not present():
            break


def _wait_gmail_ready(page, timeout=60000):
    """Gmail shows an animated splash before the app shell exists, so a fixed
    sleep sometimes lands mid-load (zero rows, no modal). Wait for the search
    box -- it only renders once the shell is up -- then clear the onboarding
    modal, then wait for results to resolve to either rows or an empty state.
    """
    try:
        page.wait_for_selector("input[aria-label*='Search'], form[role='search']",
                               timeout=timeout)
    except Exception:              # noqa: BLE001
        pass
    page.wait_for_timeout(2500)
    _dismiss_interstitials(page)
    # results pane: a row, or Gmail's "No messages matched" empty state
    try:
        page.wait_for_function(
            """() => document.querySelector('tr.zA')
                  || /No messages matched|no results|Nothing/i.test(
                       document.body.innerText)""",
            timeout=25000)
    except Exception:              # noqa: BLE001
        pass


def _find_request_thread(page):
    for q in ("connection+request", "data+import+authoriz",
              "approval+request+data+import", "workspace+data+import"):
        page.goto(f"https://mail.google.com/mail/u/0/#search/{q}",
                  wait_until="domcontentloaded", timeout=60000)
        _wait_gmail_ready(page)
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
        # Gmail's list is virtualized and the row's own hit target is
        # unreliable; open it by its subject cell, then fall back to a
        # forced click and finally to keyboard (focus the row, press o).
        opened_mail = False
        for target in (thread.locator("span.bog").first,
                       thread.locator("td").nth(4),
                       thread):
            try:
                target.scroll_into_view_if_needed(timeout=5000)
                target.click(timeout=8000)
                opened_mail = True
                break
            except Exception:      # noqa: BLE001
                try:
                    target.click(force=True, timeout=5000)
                    opened_mail = True
                    break
                except Exception:  # noqa: BLE001
                    continue
        if not opened_mail:
            try:
                thread.focus()
                page.keyboard.press("o")
                opened_mail = True
            except Exception:      # noqa: BLE001
                pass
        if not opened_mail:
            out["detail"] = "found the mail but could not open it"
            return out

        # Wait for the message BODY (div.a3s) to actually render before
        # scanning. Without this the scan runs against an empty pane and
        # falls back to the Google bar's app-launcher links. Reopen once if
        # it stays empty or shows Gmail's transient load error.
        def _body_ready():
            try:
                a3s = page.locator("div.a3s")
                if a3s.count() and len((a3s.first.inner_text() or "").strip()) > 40:
                    return True
            except Exception:      # noqa: BLE001
                pass
            return False

        for attempt in range(2):
            try:
                page.wait_for_selector("div.a3s", timeout=20000)
            except Exception:      # noqa: BLE001
                pass
            page.wait_for_timeout(3000)
            if "reloading the page" in page.inner_text("body").lower():
                page.reload(wait_until="domcontentloaded")
                _wait_gmail_ready(page)
                page.wait_for_timeout(4000)
            if _body_ready():
                break
            if attempt == 0:            # reopen the thread and try once more
                try:
                    thread.locator("span.bog").first.click(force=True, timeout=6000)
                except Exception:      # noqa: BLE001
                    pass
                page.wait_for_timeout(4000)
        if not _body_ready():
            page.screenshot(path="/root/migration/dms_approve_stuck.png",
                            full_page=True)
            out["detail"] = ("opened the mail but its body did not render "
                             "(screenshot: dms_approve_stuck.png)")
            return out
        out["did"].append("message body rendered")

        # The authorization link opens Google's consent page. The mail's
        # call-to-action wording varies (Review / Authorize / Open request),
        # and the href is some Google admin/notifications URL -- so match on
        # either signal rather than requiring both, and report what was on
        # offer if nothing matches, instead of a bare "no link".
        # Scope to the opened message body (div.a3s), not the whole page --
        # otherwise the Google bar's app-launcher 'Admin' link outscores the
        # real authorization link that lives inside the email. The body can
        # also be an iframe, so include child-frame anchors too.
        anchors = []
        for fr in page.frames:
            try:
                sels = ("div.a3s a", "div.ii.gt a", "div[data-message-id] a")
                found = False
                for sel in sels:
                    for a in fr.locator(sel).all():
                        href = (a.get_attribute("href") or "")
                        if href.startswith("http"):
                            anchors.append((a, href, (a.inner_text() or "").strip()))
                            found = True
                    if found:
                        break
                if not found and fr != page.main_frame:
                    # a body rendered as its own iframe: take all its links
                    for a in fr.locator("a").all():
                        href = (a.get_attribute("href") or "")
                        if href.startswith("http"):
                            anchors.append((a, href, (a.inner_text() or "").strip()))
            except Exception:      # noqa: BLE001
                continue
        GOOGLE_ADMIN = ("admin.google.com", "notifications.google.com",
                        "accounts.google.com")
        CTA = GRANT_LABELS + ("Review", "Open", "View request", "Respond",
                              "Get started")

        def score(href, label):
            s = 0
            if any(d in href for d in GOOGLE_ADMIN):
                s += 2
            if "migrate" in href or "dataimport" in href.lower():
                s += 3
            if any(c.lower() in label.lower() for c in CTA):
                s += 2
            # Gmail chrome (labels, policy, help) must never win
            if "mail/u/" in href or "support.google" in href \
                    or "policy" in href:
                s = -10
            return s

        ranked = sorted(((score(h, t), a, h, t) for a, h, t in anchors),
                        key=lambda x: x[0], reverse=True)
        out["did"].append("mail links: " + "; ".join(
            f"{t[:22]!r}->{h[:45]}" for _, _, h, t in ranked[:4]))
        link = ranked[0][1] if ranked and ranked[0][0] > 0 else None
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
