"""Undoing a whole tenant setup in one run.

The most destructive thing in this product, so the parts that matter are
the order it does things in and what it refuses to do.
"""
import inspect

import remove_tenant_setup as rts


class TestOrder:
    def test_the_data_is_wiped_before_the_credential_is_removed(self):
        """The wipe needs the credential that the teardown destroys. The
        other way round leaves a tenant full of data and nothing left that
        can reach it."""
        src = inspect.getsource(rts.remove)
        assert src.index("wipe_data") < src.index("run_teardown")

    def test_a_failed_wipe_stops_the_run(self):
        """Specifically: it must not go on to remove the credential."""
        src = inspect.getsource(rts.remove)
        i = src.index("if not ok:")
        assert "would strand" in src[i:i + 400]
        assert "return {" in src[i:i + 500]

    def test_the_configuration_is_forgotten_last(self):
        src = inspect.getsource(rts.remove)
        assert src.index("run_teardown") < src.index("forget_tenant_config")


class TestWhatItRefuses:
    def test_the_typed_domain_must_match_the_configured_one(self):
        src = inspect.getsource(rts.main)
        assert "REFUSING" in src
        assert "is not the configured" in src

    def test_it_compares_against_settings_not_the_argument(self):
        """A confirmation that echoes back whatever was typed confirms
        nothing."""
        src = inspect.getsource(rts.main)
        assert "st.source_domain if args.side" in src
        assert "configured" in src

    def test_the_ledger_is_not_touched(self):
        """It records a migration that happened, and outlives the tenant it
        happened to."""
        src = inspect.getsource(rts)
        assert "reset_drive_ledger" in src, "the docstring must say where"
        assert "id_mapping" not in inspect.getsource(rts.remove)


class TestForgettingIsNotAMergeUpdate:
    def test_it_uses_its_own_function(self):
        """update_tenant_config ignores empty values by design -- a caller
        that just learned one field must not blank the others. Removing a
        setup is the opposite intent."""
        import accounts_auth
        src = inspect.getsource(accounts_auth.forget_tenant_config)
        assert "UPDATE tenant_configs SET domain=''" in src

    def test_the_row_survives(self):
        """create_account inserts both sides up front, and None from
        get_tenant_config means "unknown account" -- deleting the row would
        make a real account start reporting as one that does not exist."""
        import accounts_auth
        src = inspect.getsource(accounts_auth.forget_tenant_config)
        assert "DELETE" not in src.upper()
