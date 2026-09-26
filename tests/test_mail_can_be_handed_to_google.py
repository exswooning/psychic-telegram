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

The engine keeps its own Gmail migration. DMS gives per-user console status,
not the per-item ledger that makes a re-run here idempotent, so handing ALL mail
over is a deliberate per-run choice. The migration dialog now defaults to SPLIT --
the engine moves the mail that carries a Drive link (it is the only thing that can
rewrite one) and the DMS moves the rest -- while the API's own default stays the
engine, so nothing else that starts a migration changes.

The one thing that must not happen is both moving the same mail: the ledger cannot
see what Google did internally, so every message would be inserted twice. Which
services each mode runs is decided server-side (api_server._mail_plan).
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _detail():
    return open(os.path.join(ROOT, "migration-webui/src/pages/MigrationDetail.tsx"),
                encoding="utf-8").read()


class TestTheChoiceIsOffered:
    def test_all_three_options_exist(self):
        src = _detail()
        for option in ("mail-by-split", "mail-by-engine", "mail-by-dms"):
            assert option in src

    def test_the_dialog_defaults_to_split(self):
        """Deliberately: only a fortieth or so of mail carries a Drive link, and
        those are the messages that need this tool. See the dialog's own comment."""
        assert "useState<MailMode>('split')" in _detail()

    def test_but_the_apis_own_default_is_still_the_engine(self):
        """Giving up the per-item ledger for mail must be asked for, so a caller
        that names no mode -- a script, the fleet, another page -- gets what it
        always got."""
        import api_server
        assert api_server.StartMigration.model_fields["mail_mode"].default == "engine"

    def test_each_option_says_what_it_costs(self):
        src = _detail()
        assert "per-item ledger" in src
        assert "3 writes/sec/account" in src


class TestChoosingDmsExcludesMailFromTheRun:
    """Decided server-side now, so the dialog cannot drift from the API."""

    def test_the_services_list_drops_gmail(self):
        import api_server
        services = api_server._mail_plan(["all"], "dms")[0]
        assert "gmail" not in services, (
            "both would move mail -- every message inserted twice, and the "
            "ledger cannot see what Google moved internally")

    def test_it_still_migrates_everything_else(self):
        import api_server
        services = api_server._mail_plan(["all"], "dms")[0]
        for svc in ("drive", "calendar", "contacts", "tasks", "chat"):
            assert svc in services, f"{svc} would be silently skipped"

    def test_the_engine_path_is_unchanged(self):
        import api_server
        assert api_server._mail_plan(["all"], "engine") == (["all"], None, False)

    def test_the_dialog_leaves_the_service_list_to_the_server(self):
        """It used to build the list itself, which is how the modes could drift."""
        src = _detail()
        assert "startMigration(reason, ['all'], [], false," in src
        assert "const services = mailBy" not in src


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
        assert "Data Import" in dms_migrate.MANUAL
        # the wall this tool cannot climb must be spelled out
        assert "approve" in dms_migrate.MANUAL
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


class TestWatchMode:
    """The approval lands in another tenant's mailbox. The only thing this
    side can do is keep asking -- and then finish without a person."""

    def test_watch_only_loops_on_the_pending_state(self):
        src = open("dms_migrate.py", encoding="utf-8").read()
        assert 'out.get("step") == "step1-pending"' in src
        # any other outcome must fall straight through, not spin
        assert 'if out.get("step") != "step1-pending":' in src

    def test_watch_is_bounded(self):
        src = open("dms_migrate.py", encoding="utf-8").read()
        assert "deadline = time.time() + args.watch * 60" in src
        assert "gave up after" in src

    def test_each_pass_signs_in_again(self):
        """A silently expired session is how the first driver 'succeeded'
        against a login page."""
        src = open("dms_migrate.py", encoding="utf-8").read()
        body = src[src.index("def _run():"):src.index("if args.watch")]
        assert "start(" in body
