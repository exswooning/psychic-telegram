"""
tests/test_dwd_helper.py
=========================
The one piece of dwd_helper.py that is pure logic rather than browser
choreography: telling a submitted dialog apart from a rejected one.

Everything else in this file drives a real Playwright page and is exercised
live (see AGENT_COORDINATION.md, 2026-08-11), not here -- a mocked Locator
would test the mock's behaviour, not the console's.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dwd_helper as dwd  # noqa: E402


class _FakeLocator:
    def __init__(self, n: int, raises: bool = False):
        self._n = n
        self._raises = raises

    def count(self):
        if self._raises:
            raise Exception("detached node")  # noqa: TRY002 - mirrors Playwright's
        return self._n

    def get_by_role(self, role, name=None):
        return self


class TestDialogOpen:
    """Success closes the dialog and returns to the delegation list; an
    inline error (bad scope, duplicate client, multi-party approval) leaves
    it open with the Authorize button still present. This is the only
    signal dwd_helper has for "did it work" before the functional
    verify_scopes check runs, so it gates whether the merge-and-retry
    (Overwrite path) fires at all."""

    def test_dialog_with_authorize_button_is_open(self):
        assert dwd._dialog_open(_FakeLocator(1)) is True

    def test_no_dialog_at_all_is_closed(self):
        assert dwd._dialog_open(_FakeLocator(0)) is False

    def test_a_detached_node_counts_as_closed(self):
        """A closed dialog's DOM node can go stale between the click and the
        check. Treating that as 'still open' would loop forever retrying an
        Authorize that already succeeded."""
        assert dwd._dialog_open(_FakeLocator(1, raises=True)) is False


class TestScopeSelectors:
    """Google's email input is type="text" with id=identifierId, NOT
    type="email" -- the bug that made auto sign-in silently type nothing.
    Pinned here so a future edit cannot reintroduce the wrong selector
    without a visibly failing assertion, even though the fix was only
    provable live."""

    def test_email_selector_targets_identifierId(self):
        import re

        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "dwd_helper.py")).read()
        m = re.search(r'EMAIL_SEL\s*=\s*[\'"]([^\'"]+)[\'"]', src)
        assert m, "EMAIL_SEL constant not found"
        assert "identifierId" in m.group(1)

    def test_credentials_are_never_read_from_argv(self):
        """A command line is visible to any process on the box via `ps`.
        Auto sign-in must only ever come from the environment."""
        import re

        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "dwd_helper.py")).read()
        assert 'os.getenv("DWD_PASSWORD"' in src
        assert not re.search(r'add_argument\(.*password', src, re.IGNORECASE)
