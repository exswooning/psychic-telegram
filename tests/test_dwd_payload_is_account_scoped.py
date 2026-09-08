"""The panel described one tenant while the account was set up against another.

env.sh is sourced by systemd into the server process, and on a long-lived
deployment it holds whatever the first setup put there. Live, that was
c.example.com and a.example.com -- placeholders from August -- while every
real account's tenant_configs row said source.rohitrokaya.com.np.

So three answers existed on one box at once: the process environment,
config.py's own defaults (tenanta.com), and the account's real row. The
Working domains card read the first and offered to wipe a domain nobody had
set up, while the endpoint behind its button compared against the third and
would have refused the very domain the card displayed.
"""
import inspect

import webui


class TestItReadsTheAccountsTenant:
    def test_the_payload_takes_an_account(self):
        sig = inspect.signature(webui.dwd_payload)
        assert "account_id" in sig.parameters

    def test_it_builds_settings_from_that_account(self):
        src = inspect.getsource(webui.dwd_payload)
        assert "Settings(account_id=account_id) if account_id else Settings()" in src

    def test_the_route_passes_the_on_screen_account(self):
        src = inspect.getsource(webui.Handler.do_GET)
        i = src.index('path == "/api/dwd"')
        assert "dwd_payload(self._on_screen())" in src[i:i + 200]

    def test_the_cli_caller_still_works_without_one(self):
        """dwd_helper runs from a shell with env.sh sourced and no account
        context at all -- the default has to keep that path working."""
        assert inspect.signature(webui.dwd_payload).parameters[
            "account_id"].default is None
        import dwd_helper
        assert "dwd_payload()" in inspect.getsource(dwd_helper._load_payload)
