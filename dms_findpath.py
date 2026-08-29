#!/usr/bin/env python3
"""Find where Data Migration actually lives, by clicking rather than guessing.

/ac/dm was assumed to be Data Migration. It is not -- it resolves to
Devices > Overview, so every run landed on the wrong page and reported the
console had changed. That is the second time a guessed URL has produced a
confident, wrong error message; the first was the Chat app configurator,
which searched an API overview for a form behind a tab.

So this does not assume a path. It opens the Admin console home, expands
the nav, and reports every menu entry it can see plus the URL each one
leads to. Whatever "Data migration" turns out to be, the answer comes from
the console rather than from documentation.
"""
from __future__ import annotations

import json
import sys

import dms_migrate
import dwd_helper

log = dwd_helper.log


def main() -> int:
    from playwright.sync_api import sync_playwright

    p = sync_playwright().start()
    # _open_dwd_console is what actually types the credentials; the launcher
    # alone just opens a browser at a sign-in form and waits forever. That
    # mistake cost one run. "Directory" is a nav entry present on the
    # console home, so it works as a readiness marker there.
    opened = dwd_helper._open_dwd_console(
        p, headful=False, timeout=200,
        url="https://admin.google.com/",
        ready_prefix="https://admin.google.com/",
        ready_text="Directory")
    if opened is None:
        print("sign-in did not complete", file=sys.stderr)
        p.stop()
        return 1
    browser, page = opened
    try:
        page.wait_for_timeout(5000)
        log(f"landed: {page.url}")

        # The console's own search, not the nav tree. The nav is several
        # collapsible levels deep and restructures between rollouts; search
        # is the one interface that has stayed put, and it is what a person
        # actually uses to find this page.
        results = []
        box = None
        for sel in ('input[aria-label*="Search"]', 'input[placeholder*="Search"]',
                    'input[type="search"]', 'input[aria-label*="earch"]',
                    'header input', '[role="search"] input'):
            loc = page.locator(sel)
            if loc.count() and loc.first.is_visible():
                box = loc.first
                break
        if box is None:
            log("no search box found; falling back to whatever links exist")
        else:
            box.click()
            box.type("data migration", delay=60)
            page.wait_for_timeout(3500)
            opts = page.locator('[role="option"], [role="listbox"] li, ul li')
            for i in range(min(opts.count(), 25)):
                el = opts.nth(i)
                try:
                    if not el.is_visible():
                        continue
                    t = (el.inner_text() or "").strip()
                    if t:
                        results.append(t[:90])
                except Exception:      # noqa: BLE001
                    continue
            log(f"search suggestions: {len(results)}")
            # Take the first suggestion that names it, and record where it goes.
            for i in range(min(opts.count(), 25)):
                el = opts.nth(i)
                try:
                    t = (el.inner_text() or "").lower()
                except Exception:      # noqa: BLE001
                    continue
                if "data migration" in t or "migrat" in t:
                    el.click()
                    page.wait_for_timeout(6000)
                    log(f"followed a suggestion -> {page.url}")
                    break

        out = {"url": page.url, "nav": items, "interesting": interesting,
               "search_results": results,
               "page_text": page.locator("body").inner_text()[:2500]}
        with open("/tmp/dms-nav.json", "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=1)
        page.screenshot(path="/tmp/dms-nav.png", full_page=True)

        print(f"  landed on        : {page.url}")
        print(f"  search results   : {results[:8]}")
        print(f"  nav entries seen : {len(items)}")
        print("  anything data/migration related:")
        for x in interesting or []:
            print(f"    {x['text']:40} {x['href']}")
        if not interesting:
            print("    NONE -- printing the whole nav instead:")
            for x in items[:40]:
                print(f"    {x['text']:40} {x['href']}")
        return 0
    finally:
        try:
            browser.close(); p.stop()
        except Exception:      # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
