"""
tests/test_dwd_status_account_scope.py
=======================================
/api/v2/dwd/status only ever read the CALLER's own account -- its own
comment already names one bug this caused (every SaaS account shown the
legacy env.sh tenant's delegation) and fixed it by switching from bare
Settings() to Settings(account_id=op.account_id). That fix covers a lone
SaaS tenant reading its own delegation; it does not cover a SUPERADMIN
reading a DIFFERENT account's, which is exactly what happens the moment
SeedDomainPicker (every account's domains, by design) hands the Seed
Wizard a domain that is not the signed-in operator's own.

Live: seeding info@source.sarafgloabalexim.com (account 2) while signed in
as the superadmin (account 3) showed "no service-account key at
/root/migration/keys/3/source-sa.json" -- account 3's own path, for a
domain account 3 does not own. The seed itself worked (POST /api/seed was
fixed first); this is the sibling GET the same picker flow still hit.
"""
import inspect

import api_server


def _src() -> str:
    return inspect.getsource(api_server.dwd_status)


class TestItAcceptsAnExplicitAccount:
    def test_the_signature_takes_one(self):
        sig = inspect.signature(api_server.dwd_status)
        assert "account_id" in sig.parameters

    def test_omitting_it_still_means_the_caller_own_account(self):
        src = _src()
        assert "account_id if account_id is not None else op.account_id" in src

    def test_a_caller_without_access_is_refused(self):
        src = _src()
        assert "_require_account_access(target_account, op)" in src

    def test_the_refusal_happens_before_any_settings_are_built(self):
        """A caller refused access must never reach a real Settings() for
        the account they were refused -- that would leak whether the
        account, its domain, or its key path exist at all."""
        src = _src()
        assert src.index("_require_account_access(") < src.index("def _check")

    def test_check_reads_the_resolved_account_not_the_callers(self):
        """The exact line that was wrong: Settings(account_id=op.account_id)
        ignored a passed-in account_id entirely."""
        src = _src()
        assert "Settings(account_id=target_account)" in src
        assert "Settings(account_id=op.account_id)" not in src
