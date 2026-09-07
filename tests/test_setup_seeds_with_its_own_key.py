"""The seed ran with a different credential than the setup had just created.

full_setup provisions a Cloud project, delegates its client ID, verifies it
(23/23 scopes confirmed live), and for a per-account run saves the key path
to tenant_configs -- NOT to env.sh. Then it launched the seeder as a
subprocess, which resolves its key from the environment, so the child
authenticated with whatever keys/source-sa.json happened to hold.

Live, that was a service account from an unrelated project
(wsmig-src-30428) whose client ID was never delegated for this tenant, while
the run had just delegated wsmig-src-20736. Every scope set came back
`unauthorized_client` and the phase reported the seed as failing -- moments
after the verify had confirmed the OTHER key was fine.

Two credentials, one stale, and an error that named no file.
"""
import inspect

import full_setup
import verify_scopes


class TestTheChildGetsThisRunsKey:
    def test_the_seed_launch_exports_the_key_path(self):
        src = inspect.getsource(full_setup.run_full_setup)
        i = src.index('"seed source tenant"')
        window = src[i:i + 2500]
        assert '"SEED_SA_KEY": key_path' in window, (
            "the seeder resolves its key from the environment; without this "
            "it uses whatever the legacy global path holds")

    def test_it_also_exports_domain_and_admin(self):
        """Same reason: tenant_configs is not visible to the child."""
        src = inspect.getsource(full_setup.run_full_setup)
        i = src.index('"seed source tenant"')
        window = src[i:i + 2500]
        assert '_DOMAIN": domain' in window and '_ADMIN": admin_email' in window


class TestItSeedsWhatTheGrantAllows:
    def test_groups_are_seeded_when_the_scope_was_granted(self):
        src = inspect.getsource(full_setup.run_full_setup)
        assert "GROUP_WRITE_SCOPE in set(granted)" in src
        assert '"--groups"' in src

    def test_the_group_scope_is_on_the_console_line(self):
        """A scope advertised nowhere can never be granted, which is how the
        group migration came to be unrunnable by construction."""
        from config import Settings
        g = set(verify_scopes.grant_scopes(Settings(), "source"))
        assert "https://www.googleapis.com/auth/admin.directory.group" in g

    def test_it_is_optional_not_required(self):
        """Absent, group seeding and group migration are skipped and
        everything else still runs -- so it must not block a run from
        starting."""
        from config import Settings
        req = set(verify_scopes.required_scopes(Settings(), "source"))
        assert "https://www.googleapis.com/auth/admin.directory.group" not in req

    def test_both_tenants_carry_it(self):
        """The migration creates groups on the target; the seeder creates
        them on the source."""
        from config import Settings
        for side in ("source", "target"):
            assert ("https://www.googleapis.com/auth/admin.directory.group"
                    in set(verify_scopes.grant_scopes(Settings(), side)))
