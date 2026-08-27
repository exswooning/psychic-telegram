"""Granting delegation for a client id you are not signed in as.

The Chat delete scope has to be added to the Admin Console entry for the
seeder's client id, which belongs to account 7. full-setup is the flow that
writes that entry -- and it hardcoded op.account_id, so a superadmin
signed in as account 66 could only ever set up account 66's tenant, which
has no project, no key and no client id.

Same shape as the wipe and reset cards before they learned to take an
account, and the same fix. The rule itself was written out by hand at
three call sites before it became one helper.
"""
import pytest

import api_server


def _op(account_id=66, superadmin=True):
    return api_server.Operator(name="boss", role="admin",
                               account_id=account_id, is_superadmin=superadmin)


class _Body:
    def __init__(self, account_id=None):
        self.account_id = account_id


class TestTheResolver:
    def test_none_means_my_own(self):
        assert api_server._resolve_account(_Body(None), _op(7)) == 7

    def test_a_superadmin_may_name_another(self):
        assert api_server._resolve_account(_Body(7), _op(66, True)) == 7

    def test_a_plain_account_may_not(self):
        with pytest.raises(api_server.HTTPException) as e:
            api_server._resolve_account(_Body(7), _op(66, False))
        assert e.value.status_code == 403

    def test_a_plain_account_naming_itself_is_fine(self):
        assert api_server._resolve_account(_Body(7), _op(7, False)) == 7

    def test_a_body_without_the_field_still_works(self):
        # Every WriteAction that never grew one.
        class Bare:
            pass
        assert api_server._resolve_account(Bare(), _op(7)) == 7

    def test_there_is_only_one_copy_of_the_rule(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "api_server.py"), encoding="utf-8").read()
        assert src.count("belongs to another account") == 1, (
            "the rule is being written out by hand again")
        assert src.count("def _resolve_account") == 1
        # The path-parameter endpoints go through the same one.
        assert src.count("_require_account_access(") >= 6


class TestFullSetupUsesIt:
    def _src(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return open(os.path.join(root, "api_server.py"), encoding="utf-8").read()

    def test_the_body_can_name_an_account(self):
        assert "account_id" in api_server.StartFullSetup.model_fields

    def test_the_launcher_resolves_it(self):
        body = self._src().split("async def full_setup_start")[1][:4000]
        assert "_resolve_account(body, op)" in body

    def test_the_keys_dir_follows_the_tenant_not_the_caller(self):
        """--keys-dir keys/<caller> would write the new service-account key
        into the wrong account's directory, where its own tenant_configs row
        does not point."""
        body = self._src().split("async def full_setup_start")[1][:4000]
        assert 'os.path.join("keys", str(setup_account))' in body
        assert 'os.path.join("keys", str(op.account_id))' not in body

    def test_the_slot_is_released_under_the_tenant_it_was_taken_for(self):
        # Releasing the caller's slot leaves the real one held forever, and
        # every later job on that tenant is refused for capacity.
        body = self._src().split("async def full_setup_start")[1][:6000]
        assert 'job_admission.try_admit(setup_account' in body
        assert 'job_admission.release(setup_account' in body

    def test_status_can_watch_another_account(self):
        body = self._src().split("async def full_setup_status")[1][:9000]
        assert "watching" in body
        assert "_full_setup_state_path(side, watching)" in body

    def test_status_refuses_a_plain_caller_asking_about_another(self):
        # Through the shared guard, not a seventh copy of the check.
        body = self._src().split("async def full_setup_status")[1][:9000]
        assert "_require_account_access(account, op)" in body

    def test_a_plain_caller_is_actually_refused(self):
        with pytest.raises(api_server.HTTPException) as e:
            api_server._require_account_access(7, _op(66, superadmin=False))
        assert e.value.status_code == 403

    def test_a_superadmin_is_not(self):
        api_server._require_account_access(7, _op(66, superadmin=True))
