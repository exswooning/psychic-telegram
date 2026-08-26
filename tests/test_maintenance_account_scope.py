"""A maintenance action must apply to the migration being worked on.

The account came from the caller's session alone, so a superadmin looking
at somebody else's migration could only ever act on their own. Live, a full
ledger reset aimed at account 7 came back "set the source domain in step 2
first" -- it had silently resolved to the operator's own empty tenant, and
the error said nothing about the account being wrong.
"""
import accounts_auth
import webui


class TestResolveTargetAccount:
    def test_no_request_means_my_own_account(self):
        assert webui.resolve_target_account(7, None) == (7, "")
        assert webui.resolve_target_account(7, "") == (7, "")

    def test_my_own_account_needs_no_privilege(self, monkeypatch):
        monkeypatch.setattr(accounts_auth, "get_account",
                            lambda i: {"is_superadmin": 0})
        assert webui.resolve_target_account(7, 7) == (7, "")
        assert webui.resolve_target_account(7, "7") == (7, "")

    def test_a_superadmin_may_target_another_account(self, monkeypatch):
        monkeypatch.setattr(accounts_auth, "get_account",
                            lambda i: {"is_superadmin": 1})
        assert webui.resolve_target_account(66, 7) == (7, "")

    def test_an_ordinary_tenant_may_not(self, monkeypatch):
        monkeypatch.setattr(accounts_auth, "get_account",
                            lambda i: {"is_superadmin": 0})
        account, err = webui.resolve_target_account(66, 7)
        assert account is None
        assert "another account" in err

    def test_a_signed_out_caller_may_not(self, monkeypatch):
        monkeypatch.setattr(accounts_auth, "get_account", lambda i: None)
        account, err = webui.resolve_target_account(None, 7)
        assert account is None and err

    def test_rubbish_is_refused_rather_than_coerced(self, monkeypatch):
        # int("7; DROP") raises; silently falling back to the caller's own
        # account would run a destructive action on the wrong tenant.
        monkeypatch.setattr(accounts_auth, "get_account",
                            lambda i: {"is_superadmin": 1})
        account, err = webui.resolve_target_account(66, "seven")
        assert account is None
        assert "not an account id" in err


class TestTheResetItselfStaysAccountScoped:
    def test_the_argv_builder_reads_the_named_account(self, monkeypatch):
        """It already took an account_id; nothing passed it a real one."""
        import config
        seen = {}

        class _S:
            def __init__(self, account_id=None):
                seen["account_id"] = account_id
                self.source_domain = "src.example.com"
                self.db_path = "/tmp/x.db"

        monkeypatch.setattr(config, "Settings", _S)
        argv, env, err = webui.reset_drive_ledger_argv(
            {"confirm_domain": "src.example.com",
             "services": "drive,gmail"}, 7)
        assert err == ""
        assert seen["account_id"] == 7
        assert "--services" in argv
        assert argv[argv.index("--services") + 1] == "drive,gmail"

    def test_every_service_can_be_reset_not_just_drive(self, monkeypatch):
        import config

        class _S:
            def __init__(self, account_id=None):
                self.source_domain = "src.example.com"
                self.db_path = "/tmp/x.db"

        monkeypatch.setattr(config, "Settings", _S)
        argv, _env, err = webui.reset_drive_ledger_argv(
            {"confirm_domain": "src.example.com",
             "services": ["drive", "gmail", "calendar", "chat", "contacts",
                          "tasks"]}, 7)
        assert err == ""
        assert argv[argv.index("--services") + 1] == \
            "drive,gmail,calendar,chat,contacts,tasks"
