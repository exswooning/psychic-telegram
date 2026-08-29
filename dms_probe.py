#!/usr/bin/env python3
"""Dump what the Data Migration console actually renders.

dms_migrate.py's selectors were written from Google's documented flow, not
from the page. That is exactly how gcloud_browser_auth's Chat configurator
came to be broken from the day it was written -- it searched an API
overview page for a form that lives behind a tab, reported "console may
have changed", and everyone believed it for months.

So: look first. This drives the same sign-in, lands on the same page, and
writes down every control it can see -- role, accessible name, tag, id,
data-* attributes, and the visible text around it. That output is the
ground truth to write real selectors against.

It changes nothing. No clicks beyond what is needed to reach the page.

    python3 dms_probe.py --out /tmp/dms-probe.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import dms_migrate
import dwd_helper

log = dwd_helper.log

# Everything a form is made of. Deliberately broad: the point is to find
# out what is there, not to confirm a guess.
ROLES = ("button", "link", "textbox", "combobox", "radio", "checkbox",
         "tab", "menuitem", "option", "heading")


def describe(page) -> dict:
    out = {"url": page.url, "title": page.title(), "controls": [], "text": ""}
    try:
        out["text"] = page.locator("body").inner_text()[:6000]
    except Exception:      # noqa: BLE001
        pass

    seen = set()
    for role in ROLES:
        try:
            loc = page.get_by_role(role)
            n = min(loc.count(), 60)
        except Exception:      # noqa: BLE001
            continue
        for i in range(n):
            el = loc.nth(i)
            try:
                if not el.is_visible():
                    continue
                item = {
                    "role": role,
                    "name": (el.inner_text() or "").strip()[:80],
                    "tag": el.evaluate("e => e.tagName.toLowerCase()"),
                    "id": el.get_attribute("id"),
                    "testid": el.get_attribute("data-testid"),
                    "aria": el.get_attribute("aria-label"),
                    "type": el.get_attribute("type"),
                }
            except Exception:      # noqa: BLE001
                continue
            key = (item["role"], item["name"], item["id"])
            if key in seen:
                continue
            seen.add(key)
            out["controls"].append(item)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default="/tmp/dms-probe.json")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--headful", action="store_true")
    args = ap.parse_args(argv)

    opened = dms_migrate.open_console(args.headful, args.timeout)
    if opened is None:
        print("sign-in did not complete", file=sys.stderr)
        return 1
    p, browser, page = opened
    try:
        page.wait_for_timeout(5000)   # the console renders client-side
        data = describe(page)
        # And again after any "set up" entry point, since the interesting
        # form is usually one click in -- recorded separately so the two
        # states can be told apart.
        for label in ("Set up data migration", "Set Up Data Migration",
                      "Continue", "Get started"):
            btn = page.get_by_role("button", name=label)
            if btn.count() and btn.first.is_visible():
                btn.first.click()
                page.wait_for_timeout(4000)
                data["after_setup_click"] = {"clicked": label,
                                             **describe(page)}
                break
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
        page.screenshot(path=args.out.replace(".json", ".png"), full_page=True)
        print(f"url      : {data['url']}")
        print(f"title    : {data['title']}")
        print(f"controls : {len(data['controls'])}")
        if "after_setup_click" in data:
            print(f"after '{data['after_setup_click']['clicked']}': "
                  f"{len(data['after_setup_click']['controls'])} controls")
        print(f"written  : {args.out}")
        return 0
    finally:
        try:
            browser.close(); p.stop()
        except Exception:      # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
