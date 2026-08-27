"""Every action button ran against env.sh, not the signed-in tenant.

Pressed live from the Seed Wizard, signed in as an account whose source
tenant is source.rohitrokaya.com.np with 201 users:

    Checking 5 account(s) in c.example.com as admin@c.example.com ...
      MISS alice@c.example.com  -> invalid_grant: Invalid email or User ID
      MISS bob@c.example.com    -> invalid_grant: Invalid email or User ID
      ...
    Missing accounts. Re-run the seeder with 'create the accounts if they
    do not exist' checked

c.example.com is the placeholder in env.sh and alice..erin are its five
fabricated defaults. Nothing was wrong with the tenant; the button was
looking at a different one. The same route runs discover, provision,
migrate, verify, report and every repair action.

The transcript landed in logs/jobs/_none/ rather than logs/jobs/66/,
which is what made it visible: the run had no account at all.
"""
import webui


def _cfg(monkeypatch):
    import config
    real = config.Settings

    def fake(account_id=None, **kw):
        st = real.__new__(real)
        st.account_id = account_id
        st.source_domain, st.target_domain = "src.example", "tgt.example"
        st.source_admin, st.target_admin = "a@src.example", "a@tgt.example"
        st.source_sa_key, st.target_sa_key = "/k/s.json", "/k/t.json"
        st.db_path = "/data/66/migration.db"
        return st

    monkeypatch.setattr(config, "Settings", fake)


class TestTheChildIsPointedAtTheAccount:
    def test_both_tenants_are_overlaid(self, monkeypatch):
        """Half an overlay is how you migrate one tenant's users into
        another tenant's placeholder: migrate reads source, writes target."""
        _cfg(monkeypatch)
        env = webui._account_env(66, {"PATH": "/usr/bin"})
        assert env["SOURCE_DOMAIN"] == "src.example"
        assert env["TARGET_DOMAIN"] == "tgt.example"
        assert env["SOURCE_ADMIN"] == "a@src.example"
        assert env["TARGET_ADMIN"] == "a@tgt.example"
        assert env["SOURCE_SA_KEY"] == "/k/s.json"
        assert env["TARGET_SA_KEY"] == "/k/t.json"

    def test_the_ledger_moves_with_it(self, monkeypatch):
        _cfg(monkeypatch)
        assert webui._account_env(66, {})["MIGRATION_DB"] == "/data/66/migration.db"

    def test_the_base_environment_survives(self, monkeypatch):
        # gcloud_env() puts gcloud on PATH; losing it breaks every action.
        _cfg(monkeypatch)
        env = webui._account_env(66, {"PATH": "/usr/bin", "KEEP": "yes"})
        assert env["PATH"] == "/usr/bin" and env["KEEP"] == "yes"

    def test_no_account_leaves_env_sh_alone(self, monkeypatch):
        # The SSH-tunnel operator's path, which env.sh IS the truth for.
        _cfg(monkeypatch)
        base = {"SOURCE_DOMAIN": "legacy.example"}
        assert webui._account_env(None, base) == base

    def test_it_does_not_mutate_the_base(self, monkeypatch):
        _cfg(monkeypatch)
        base = {"SOURCE_DOMAIN": "legacy.example"}
        webui._account_env(66, base)
        assert base == {"SOURCE_DOMAIN": "legacy.example"}


class TestTheRouteUsesIt:
    def _block(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "webui.py"), encoding="utf-8").read()
        return src.split('if self.path != "/api/run":')[1]

    def test_it_resolves_an_account(self):
        b = self._block()
        assert "resolve_target_account" in b

    def test_it_overlays_that_account(self):
        assert "_account_env(account_id, base)" in self._block()

    def test_it_uses_that_accounts_job_not_the_global_one(self):
        # The global JOB is why the transcript landed in logs/jobs/_none/.
        b = self._block()
        assert "get_job(account_id).start" in b
        assert "JOB.start" not in b

    def test_it_admits_the_job(self):
        # Two migrations at once on one tenant corrupt the run.
        b = self._block()
        assert "job_admission.try_admit" in b
        assert "job_admission.release" in b

    def test_it_checks_the_subscription(self):
        assert "_subscription_ok" in self._block()


class TestTheInvariantStillHolds:
    def test_no_action_spec_passes_account_id(self):
        """_account_env sets MIGRATION_DB, and control_plane_db._db_path()
        is Settings().db_path -- so a child also told --account-id follows
        it into the per-account ledger hunting for tenant_configs:

            sqlite3.OperationalError: no such table: tenant_configs
        """
        offenders = [name for name, spec in webui.ACTIONS.items()
                     if "--account-id" in spec.get("argv", [])]
        assert not offenders, offenders

    def test_the_launch_actions_do_not_either(self):
        # migrate/delta build argv dynamically rather than from spec["argv"].
        for name in ("migrate", "delta"):
            assert "--account-id" not in webui._action_argv(name)
