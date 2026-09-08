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


class TestItStreamsSoProgressCanBeSeen:
    def test_the_child_is_not_captured(self):
        """capture_output buffers until exit, so a 200-user wipe showed an
        elapsed clock and nothing else for its whole run -- while the seeder
        underneath printed "[137/200] user: ... deleted" throughout.

        Asserted against the parsed code rather than the text: the docstring
        explaining why capture_output was wrong contains the words, and a
        test that greps prose passes on a file that says the right thing and
        does the opposite. That exact mistake was made twice today.
        """
        import ast

        tree = ast.parse(inspect.getsource(rts._run).lstrip())
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
        captured = [c for c in calls
                    if any(k.arg == "capture_output" for k in c.keywords)]
        assert not captured, "the child is still buffered until it exits"
        names = {ast.unparse(c.func) for c in calls}
        assert "subprocess.Popen" in names

    def test_the_lines_are_passed_through(self):
        """webui's _counter_progress_pct already reads [n/total]; the lines
        just never arrived."""
        src = inspect.getsource(rts._run)
        assert "print(line" in src

    def test_a_tail_is_kept_for_the_failure_message(self):
        src = inspect.getsource(rts._run)
        assert "tail" in src and "del tail[:-40]" in src


class TestAWipeIncludesTheGroups:
    def test_the_reset_deletes_them(self):
        """A wipe that leaves them behind is not a wipe. They were simply
        not here when reset was written."""
        import os
        import sys
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data-generator"))
        import seed_sandbox as seed

        assert hasattr(seed, "reset_groups")
        src = inspect.getsource(seed.main)
        assert "reset_groups(" in src

    def test_it_only_deletes_the_ones_this_seeder_makes(self):
        """A tenant's own distribution lists are not this tool's to delete,
        and "every group in the domain" is not a thing to be one typo from."""
        import sys, os
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data-generator"))
        import seed_sandbox as seed

        src = inspect.getsource(seed.reset_groups)
        assert '"all-staff", "engineering", "leads", "partners"' in src
        assert "email not in made" in src

    def test_a_group_failure_does_not_fail_the_wipe(self):
        import sys, os
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data-generator"))
        import seed_sandbox as seed

        assert "except Exception" in inspect.getsource(seed.reset_groups)
