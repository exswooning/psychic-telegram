"""The endpoint behind the Mission Control removal button.

It ends a tenant setup rather than pausing or resetting one, so what matters
is what it refuses.
"""
import inspect

import webui


def _block() -> str:
    src = inspect.getsource(webui.Handler.do_POST)
    i = src.index('if self.path == "/api/remove_tenant_setup":')
    j = src.index('if self.path == "/api/reset_target":', i)
    return src[i:j]


class TestItRefuses:
    def test_the_domain_is_compared_against_settings_not_the_body(self):
        """A confirmation checked against a value the caller also supplied
        confirms nothing."""
        blk = _block()
        assert "st.source_domain if side" in blk
        assert 'typed.lower() != configured.lower()' in blk

    def test_an_unconfigured_side_cannot_be_removed(self):
        """Empty == empty would otherwise pass the comparison and start a
        teardown of nothing."""
        assert "not configured or" in _block()

    def test_the_side_is_validated(self):
        assert 'side must be source or target' in _block()

    def test_it_resolves_the_account_before_acting(self):
        """An operator removing somebody else's tenant is the normal case;
        resolving from the session alone silently aims at their own."""
        blk = _block()
        assert "resolve_target_account" in blk
        assert blk.index("resolve_target_account") < blk.index("remove_tenant_setup.py")

    def test_the_password_is_passed_as_environment_only(self):
        """It must not reach argv, which is world-readable in ps output."""
        blk = _block()
        assert 'env["DWD_PASSWORD"]' in blk
        i = blk.index("argv = [PY")
        assert "admin_password" not in blk[i:], "password leaked into argv"


class TestItRunsTheRightThing:
    def test_it_launches_the_orchestrator(self):
        blk = _block()
        assert '"remove_tenant_setup.py"' in blk

    def test_it_passes_the_configured_domain_not_the_typed_one(self):
        """They are equal by then -- but passing the configured value means
        the child re-checks against the same source of truth."""
        blk = _block()
        assert '"--confirm-domain", configured' in blk


class TestTheModeIsExplicit:
    def test_an_unrecognised_mode_is_refused(self):
        """Two very different intentions behind one dialog. Defaulting an
        unknown value would make the destructive one the fallback."""
        blk = _block()
        assert 'mode not in ("wipe", "remove")' in blk

    def test_a_wipe_keeps_the_setup(self):
        blk = _block()
        assert '"--keep-setup"' in blk
        i = blk.index('if mode == "wipe"')
        assert "--keep-setup" in blk[i:i + 200]

    def test_the_job_is_named_for_what_it_does(self):
        """"remove tenant setup" against a run that only wiped data would
        misreport it on the Jobs page forever after."""
        blk = _block()
        assert '"wipe tenant data"' in blk and '"remove tenant setup"' in blk
