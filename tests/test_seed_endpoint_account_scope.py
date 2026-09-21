"""
tests/test_seed_endpoint_account_scope.py
==========================================
/api/seed resolved the account from the caller's own session alone, so a
superadmin picking a domain from SeedDomainPicker -- which lists every
account's configured domains, by design -- got "set the source domain in
step 2 first" back: the request silently resolved to the OPERATOR's own
(empty) account instead of the one that actually owns the domain just
picked.

resolve_target_account() exists for exactly this and was already wired
into /api/repair_console_setup and /api/reset_target; /api/seed was the
one caller still resolving from the session alone. Its own docstring
names this precise failure shape, from a different endpoint that had the
same bug once.
"""
import inspect

import webui


def _block() -> str:
    src = inspect.getsource(webui.Handler._do_POST)
    i = src.index('if self.path == "/api/seed":')
    j = src.index('if self.path == "/api/repair_console_setup":', i)
    return src[i:j]


class TestSeedResolvesTheRequestedAccountNotJustTheSession:
    def test_it_calls_resolve_target_account(self):
        blk = _block()
        assert "resolve_target_account(" in blk
        assert 'self._account_id(), body.get("account_id")' in blk

    def test_a_refused_account_stops_before_the_subscription_check(self):
        """A superadmin acting on an account they cannot reach must be
        refused before anything about THAT account is even read."""
        blk = _block()
        i = blk.index("resolve_target_account(")
        j = blk.index("_subscription_ok(")
        between = blk[i:j]
        assert "scope_err" in between and "return" in between

    def test_the_refusal_is_403_not_a_silent_200(self):
        blk = _block()
        i = blk.index("if scope_err:")
        snippet = blk[i:i + 200]
        assert "403" in snippet


class TestOmittingAccountIdStillMeansMyOwnAccount:
    def test_resolve_target_account_itself_defaults_to_the_caller(self):
        """The superadmin/non-superadmin split itself is already proved in
        test_maintenance_account_scope.py with a real accounts_auth lookup;
        duplicating that fixture here would just be a second copy that can
        drift. Restated only for the property that keeps every EXISTING
        seed -- a tenant seeding itself, no account_id in the request --
        working exactly as before this fix."""
        assert webui.resolve_target_account(66, None) == (66, "")
        assert webui.resolve_target_account(66, "") == (66, "")
