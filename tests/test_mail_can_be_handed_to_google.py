"""Mail is the majority of the work and the only part we cannot speed up.

From the last real run, 593,816 items migrated:

    message   349,560   58.9%      <- capped at 3 writes/sec/account
    file      141,662   23.9%      <- already files.copy, server-side
    acl        51,506    8.7%
    event      32,234    5.4%

Google states the 3/sec ceiling is not adjustable on request, so
1,739 messages per user is ~10 minutes each no matter what hardware or how
many nodes. Google's own Data Migration Service moves mail inside Google
and spends none of this project's Gmail quota.

The engine keeps its own Gmail migration and it stays the default -- DMS
gives per-user console status, not the per-item ledger that makes a re-run
here idempotent. This is a per-run choice between two real options.

The one thing that must not happen is both moving mail: the ledger cannot
see what Google did internally, so every message would be inserted twice.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _detail():
    return open(os.path.join(ROOT, "migration-webui/src/pages/MigrationDetail.tsx"),
                encoding="utf-8").read()


class TestTheChoiceIsOffered:
    def test_both_options_exist(self):
        src = _detail()
        assert "mail-by-engine" in src
        assert "mail-by-dms" in src

    def test_the_engine_is_the_default(self):
        # The ledger is the product; giving it up must be deliberate.
        assert "useState('engine')" in _detail()

    def test_each_option_says_what_it_costs(self):
        src = _detail()
        assert "per-item ledger" in src
        assert "3 writes/sec/account" in src


class TestChoosingDmsExcludesMailFromTheRun:
    def test_the_services_list_drops_gmail(self):
        src = _detail()
        block = src.split("const services = mailBy === 'dms'")[1][:220]
        assert "'gmail'" not in block, (
            "both would move mail -- every message inserted twice, and the "
            "ledger cannot see what Google moved internally")

    def test_it_still_migrates_everything_else(self):
        src = _detail()
        block = src.split("const services = mailBy === 'dms'")[1][:220]
        for svc in ("drive", "calendar", "contacts", "tasks", "chat"):
            assert f"'{svc}'" in block, f"{svc} would be silently skipped"

    def test_the_engine_path_is_unchanged(self):
        block = _detail().split("const services = mailBy === 'dms'")[1][:220]
        assert "['all']" in block


class TestTheDriver:
    def test_it_reuses_the_existing_sign_in(self):
        """Two hundred lines of Google login handling, already written and
        already debugged against a console that changes without notice."""
        src = open(os.path.join(ROOT, "dms_migrate.py"), encoding="utf-8").read()
        assert "dwd_helper._open_dwd_console" in src
        assert "def _sign_in" not in src, "sign-in was reimplemented"

    def test_the_opener_still_defaults_to_dwd(self):
        # Parameterising it must not change any existing caller.
        import inspect

        import dwd_helper
        sig = inspect.signature(dwd_helper._open_dwd_console)
        assert sig.parameters["url"].default == dwd_helper.DWD_URL

    def test_it_does_not_move_mail_without_apply(self):
        """It stops at the setup control by default. Firing a real mail
        migration from a selector match it cannot verify is not a thing to
        do by accident."""
        src = open(os.path.join(ROOT, "dms_migrate.py"), encoding="utf-8").read()
        assert '"--apply"' in src
        assert "dry_run=not args.apply" in src

    def test_a_failure_prints_the_manual_path(self):
        # dwd_helper's rule: the console changes, so never leave the
        # operator stuck -- tell them where to click.
        import dms_migrate
        assert "Data migration" in dms_migrate.MANUAL
        assert "--services drive,calendar,contacts,tasks,chat" in dms_migrate.MANUAL

    def test_it_tries_several_selectors(self):
        """A single brittle selector is how this class of tool silently
        stops working."""
        import dms_migrate
        assert callable(dms_migrate._find_first)

    def test_a_missing_control_is_not_an_exception(self, monkeypatch):
        import dms_migrate

        class _Page:
            def locator(self, sel):
                raise RuntimeError("no such element")

        assert dms_migrate._find_first(_Page(), ["a", "b"]) is None
