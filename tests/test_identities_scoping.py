"""
tests/test_identities_scoping.py
================================
Reads on this page belong to the signed-in account.

/api/identities was called with no account id, so it opened the box's
default ledger. Live, a superadmin looking at account 66's console saw
eleven rows belonging to c.anupam-poudel.com.np -- a different customer's
source and target addresses, rendered as though they were this
migration's.

That is the failure _account_env's docstring already describes for the
action buttons ("everything the buttons did was aimed at the same wrong
place"), reappearing in the one payload whose rows are literally other
people's email addresses. It was invisible until the identity map was put
on screen; nothing else read it.
"""

from __future__ import annotations

import inspect
import re

import webui


class TestTheRouteIsScoped:
    def test_it_passes_the_account_on_screen(self):
        src = inspect.getsource(webui.Handler.do_GET)
        # Generous window: a comment above the call must not push it out of
        # view and turn a passing check into a failing one.
        seg = src.split('path == "/api/identities"', 1)[1][:900]
        assert "_on_screen()" in seg, "identities is read without an account"

    def test_the_payload_still_accepts_no_account(self):
        """The signature keeps its default so CLI callers and the legacy
        single-operator path are unaffected."""
        sig = inspect.signature(webui.identities_payload)
        assert sig.parameters["account_id"].default is None

    def test_every_ledger_read_route_is_scoped(self):
        """A read that opens a ledger without an account shows one tenant's
        data to another. Named individually so a new one has to be added
        here deliberately."""
        src = inspect.getsource(webui.Handler.do_GET)
        for route in ("/api/identities", "/api/licences", "/api/snapshot",
                      "/api/spa/users", "/api/spa/report"):
            seg = src.split(f'path == "{route}"', 1)
            if len(seg) < 2:
                continue
            window = seg[1][:900]
            assert "_on_screen()" in window, f"{route} reads without an account"


class TestMergesAreVisible:
    def test_the_payload_marks_rows_that_share_a_target(self):
        """A typo in a target address and a deliberate merge look identical
        until something says which targets have more than one source."""
        src = inspect.getsource(webui.identities_payload)
        assert "mergedInto" in src and "merges" in src

    def test_the_console_styles_them_differently(self):
        page = open("console.html", encoding="utf-8").read()
        assert "tr.merged" in page and "mergetag" in page
