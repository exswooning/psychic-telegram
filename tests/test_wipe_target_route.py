"""The target's provisioned accounts need a way out that isn't SSH.

reset_target empties the seeded data; the ~200 users provisioning created
stay. A rehearsal on top of them is not a rehearsal: provisioning skips
users that already exist, the copy lands on the previous one, and the
fidelity check compares a tenant against itself.

The gate is reset_target's, reused rather than restated -- one place that
knows which domain must be typed.
"""
import webui


def _cfg(monkeypatch, source="src.example", target="tgt.example"):
    import config
    real = config.Settings

    def fake(account_id=None, **kw):
        st = real.__new__(real)
        st.source_domain, st.target_domain = source, target
        st.target_admin, st.target_sa_key = "admin@" + target, "/k/t.json"
        st.db_path, st.account_id = "/tmp/m.db", account_id
        return st

    monkeypatch.setattr(config, "Settings", fake)


class TestTheDomainMustBeTyped:
    def test_no_domain_typed_is_refused(self, monkeypatch):
        _cfg(monkeypatch)
        assert "type the target domain" in webui.wipe_target_argv({}, 7)[2]

    def test_the_source_domain_is_refused(self, monkeypatch):
        # The one typo that would matter, and it names why.
        _cfg(monkeypatch)
        err = webui.wipe_target_argv({"confirm_domain": "src.example"}, 7)[2]
        assert "does not match the target domain" in err
        assert "SOURCE domain" in err

    def test_a_protected_domain_is_refused(self, monkeypatch):
        _cfg(monkeypatch)
        monkeypatch.setenv("PROTECTED_DOMAINS", "tgt.example")
        assert "PROTECTED_DOMAINS" in webui.wipe_target_argv(
            {"confirm_domain": "tgt.example"}, 7)[2]


class TestTheCommand:
    def test_it_runs_wipe_target_and_actually_applies(self, monkeypatch):
        _cfg(monkeypatch)
        monkeypatch.delenv("PROTECTED_DOMAINS", raising=False)
        argv, env, err = webui.wipe_target_argv({"confirm_domain": "tgt.example"}, 7)
        assert not err
        assert "wipe_target.py" in argv[1]
        assert "--apply" in argv, "a button that only reports is a trap"
        assert argv[argv.index("--confirm-domain") + 1] == "tgt.example"

    def test_it_never_names_the_source_domain(self, monkeypatch):
        _cfg(monkeypatch)
        monkeypatch.delenv("PROTECTED_DOMAINS", raising=False)
        argv, _, _ = webui.wipe_target_argv({"confirm_domain": "tgt.example"}, 7)
        assert "src.example" not in " ".join(argv)

    def test_it_does_not_also_pass_account_id(self, monkeypatch):
        """The env already points MIGRATION_DB at that account's ledger, and
        control_plane_db._db_path() is Settings().db_path -- so a child told
        to resolve an account follows MIGRATION_DB into the per-account
        ledger looking for tenant_configs, which only exists in the control
        plane. Live:

            sqlite3.OperationalError: no such table: tenant_configs
        """
        _cfg(monkeypatch)
        monkeypatch.delenv("PROTECTED_DOMAINS", raising=False)
        argv, env, _ = webui.wipe_target_argv({"confirm_domain": "tgt.example"}, 7)
        assert "--account-id" not in argv
        assert env["MIGRATION_DB"] == "/tmp/m.db"
        assert env["TARGET_DOMAIN"] == "tgt.example"

    def test_the_sandbox_flag_survives(self, monkeypatch):
        # wipe_target.py calls reset_target.assert_sandbox, which needs it.
        _cfg(monkeypatch)
        monkeypatch.delenv("PROTECTED_DOMAINS", raising=False)
        _, env, _ = webui.wipe_target_argv({"confirm_domain": "tgt.example"}, 7)
        assert env["SANDBOX_MODE"] == "true"


class TestTheRouteIsWired:
    def test_the_endpoint_exists_and_admits_a_job(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "webui.py"), encoding="utf-8").read()
        block = src.split('if self.path == "/api/wipe_target":')[1][:900]
        assert "wipe_target_argv" in block
        assert "job_admission.try_admit" in block, "two at once corrupt the run"
        assert "_subscription_ok" in block


class TestItCanTargetAnotherAccount:
    """Cleaning up somebody else's tenant is the normal case for this button.

    Resolving from the session alone aims a superadmin at their own empty
    account, which is how a full ledger reset once came back "set the source
    domain in step 2 first" while pointed at a tenant that had one.
    """

    def _block(self, path):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "webui.py"), encoding="utf-8").read()
        return src.split(f'if self.path == "{path}":')[1][:700]

    def test_wipe_resolves_the_requested_account(self):
        b = self._block("/api/wipe_target")
        assert "resolve_target_account" in b
        assert 'body.get("account_id")' in b

    def test_reset_target_does_too(self):
        # Same button row, same expectation -- one of them targeting and the
        # other not is worse than neither.
        b = self._block("/api/reset_target")
        assert "resolve_target_account" in b

    def test_someone_elses_account_is_refused_for_a_plain_caller(self, monkeypatch):
        # resolve_target_account owns that rule; assert it still holds.
        # get_account reads the control-plane db, which a unit test has no
        # business opening -- stub the one fact the rule turns on.
        monkeypatch.setattr(webui.accounts_auth, "get_account",
                            lambda aid: {"is_superadmin": 0})
        assert webui.resolve_target_account(7, "66")[1] == \
            "that migration belongs to another account"

    def test_a_superadmin_may_target_another_account(self, monkeypatch):
        monkeypatch.setattr(webui.accounts_auth, "get_account",
                            lambda aid: {"is_superadmin": 1})
        assert webui.resolve_target_account(66, "7") == (7, "")

    def test_blank_means_my_own(self):
        assert webui.resolve_target_account(7, None) == (7, "")


class TestAJobStartedElsewhereCanBeWatched:
    """The wipe card can aim at another account; /api/job could not follow.

    Live: the wipe started under account 7 and /api/job -- scoped to the
    caller -- answered {"running": false, "lines": []}. The UI had launched
    work it could not then show, which leaves the box as the only way to
    see whether a destructive job did anything.
    """

    def test_the_route_resolves_the_account_being_watched(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "webui.py"), encoding="utf-8").read()
        block = src.split('elif path == "/api/job":')[1][:1100]
        assert "resolve_target_account" in block
        assert '_job_snapshot(watching' in block

    def test_it_refuses_another_tenants_job_for_a_plain_caller(self, monkeypatch):
        monkeypatch.setattr(webui.accounts_auth, "get_account",
                            lambda aid: {"is_superadmin": 0})
        assert webui.resolve_target_account(7, "66")[0] is None


class TestTheTwoAccountStrategiesAreNeverMixed:
    """webui.py overlays the account's config onto the child's environment.
    api_server.py instead passes --account-id and lets the child resolve it
    from tenant_configs. Either is fine on its own.

    Mixing them is not: control_plane_db._db_path() is Settings().db_path,
    so a child handed MIGRATION_DB=<that account's ledger> AND told to look
    up an account follows the env var into the per-account ledger hunting
    for tenant_configs -- a table only the control-plane database has.

        sqlite3.OperationalError: no such table: tenant_configs

    That is what the wipe button did on its first real press.
    """

    BUILDERS = ("seed_argv", "reset_target_argv", "reset_drive_ledger_argv",
                "wipe_target_argv")

    def test_no_builder_sets_the_ledger_and_also_resolves_an_account(
            self, monkeypatch):
        _cfg(monkeypatch)
        monkeypatch.delenv("PROTECTED_DOMAINS", raising=False)
        body = {"confirm_domain": "tgt.example", "scale": "small"}
        offenders = []
        for name in self.BUILDERS:
            argv, env, err = getattr(webui, name)(dict(body), 7)
            if err:      # a builder that refuses this body proves nothing
                continue
            if env.get("MIGRATION_DB") and "--account-id" in argv:
                offenders.append(name)
        assert not offenders, (
            "these hand the child a ledger path and then tell it to resolve "
            f"an account through that same path: {offenders}")

    def test_the_builders_still_scope_the_child_somehow(self, monkeypatch):
        # The invariant above is satisfied trivially by a builder that
        # scopes nothing at all, which would silently act on env.sh.
        _cfg(monkeypatch)
        monkeypatch.delenv("PROTECTED_DOMAINS", raising=False)
        argv, env, err = webui.wipe_target_argv({"confirm_domain": "tgt.example"}, 7)
        assert not err and env.get("MIGRATION_DB")
