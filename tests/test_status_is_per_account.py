"""The Mission Control header and the Final Report describe one tenant.

Both read webui.py's /api/status, which built its answer from a bare
State() -- env.sh's single migration.db, the operator's own ledger. Live,
signed in as a superadmin while account 7 ran 201 users:

    Mission Control -> "Live -- pushed over WebSocket - 11 users tracked
                        - overall 28%"        above a websocket-fed list
                                              of account 7's 201 users
    Final Report    -> "Migration Complete. 11 of 11 users migrated
                        successfully in --."

The report is the serious one: it does not show a wrong number, it
declares a migration complete while it is still running.

The cache is the second half of the bug. One process-wide _snap entry
meant the first caller's tenant was replayed to every other caller for
STATUS_TTL seconds -- the same fault the websocket hub had when it was a
bare set of sockets, and it would have survived the scoping fix silently.
"""
import time

import pytest

import account_context
import webui
from db import MigrationDB


@pytest.fixture(autouse=True)
def _clear_cache():
    webui._snaps.clear()
    webui._snap_busy.clear()
    yield
    webui._snaps.clear()
    webui._snap_busy.clear()


def _ledger(tmp_path, name, users):
    path = str(tmp_path / f"{name}.db")
    d = MigrationDB(path)
    for i in range(users):
        d.conn.execute(
            "INSERT INTO identity_map(source_email,target_email,status) "
            "VALUES(?,?,'DONE')", (f"u{i}@{name}.src", f"u{i}@{name}.tgt"))
    d.conn.commit()
    d.close()
    return path


def _as_account(monkeypatch, mapping):
    """Make Settings(account_id=N) resolve to one of our temp ledgers.

    The ledger and the domains have to arrive together: identities_loaded
    refuses to count a map whose emails do not match the configured
    tenants, so a fake that supplies only the path reports every tenant as
    WRONG TENANTS -- zero users, the very number being fixed.
    """
    import config

    real = config.Settings

    def fake(account_id=None, **kw):
        if account_id in mapping:
            path, dom = mapping[account_id]
            cfg = real.__new__(real)
            cfg.db_path = path
            cfg.source_domain = f"{dom}.src"
            cfg.target_domain = f"{dom}.tgt"
            cfg.account_id = account_id
            return cfg
        return real(account_id=account_id, **kw)

    monkeypatch.setattr(config, "Settings", fake)
    return fake


class TestTheReaderCountsTheLedgerItIsGiven:
    def test_state_takes_an_account(self, tmp_path, monkeypatch):
        from wizard import State
        path = _ledger(tmp_path, "seven", 3)
        _as_account(monkeypatch, {7: (path, "seven")})
        assert State(account_id=7).env["MIGRATION_DB"] == path

    def test_the_domains_move_with_the_ledger(self, tmp_path, monkeypatch):
        # Left behind, env.sh's domains mark every one of that tenant's
        # mappings a mismatch and identities_loaded returns 0.
        from wizard import State
        _as_account(monkeypatch, {7: (_ledger(tmp_path, "seven", 3), "seven")})
        st = State(account_id=7)
        assert st.env["SOURCE_DOMAIN"] == "seven.src"
        assert st.env["TARGET_DOMAIN"] == "seven.tgt"

    def test_identities_are_counted_from_that_ledger(self, tmp_path, monkeypatch):
        from wizard import State
        _as_account(monkeypatch, {7: (_ledger(tmp_path, "seven", 3), "seven"),
                                  66: (_ledger(tmp_path, "other", 11), "other")})
        assert State(account_id=7).identities_loaded() == 3
        assert State(account_id=66).identities_loaded() == 11

    def test_progress_is_counted_from_that_ledger(self, tmp_path, monkeypatch):
        from wizard import State
        path = _ledger(tmp_path, "seven", 4)
        d = MigrationDB(path)
        d.log_audit("u0@seven.src", "i1", "file", "SUCCESS", "")
        d.log_audit("u1@seven.src", "i2", "file", "FAILED", "boom")
        d.close()
        _as_account(monkeypatch, {7: (path, "seven")})
        assert State(account_id=7).migration_progress() == (1, 1, 4)

    def test_an_absent_ledger_reports_nothing_not_somebody_else(self, tmp_path,
                                                                monkeypatch):
        from wizard import State
        _as_account(monkeypatch,
                    {7: (str(tmp_path / "never-created.db"), "seven")})
        st = State(account_id=7)
        assert st.identities_loaded() == 0
        assert st.migration_progress() == (0, 0, 0)

    def test_an_unconfigured_account_shows_nothing_not_env_sh(self, monkeypatch):
        """Signup day. Falling back to env.sh is the bug, not the fallback."""
        import config
        from wizard import State

        def boom(account_id=None, **kw):
            raise ValueError(f"no tenant_configs rows for account_id={account_id}")

        monkeypatch.setattr(config, "Settings", boom)
        st = State(account_id=999)
        assert st.env["MIGRATION_DB"] == ""
        assert st.identities_loaded() == 0
        assert "no configuration yet" in st.notes.get("account", "")


class TestTheCacheIsKeyedByAccount:
    """A shared entry is how the wrong tenant gets served after the reader
    is already correct."""

    def test_two_accounts_do_not_share_one_entry(self, tmp_path, monkeypatch):
        _as_account(monkeypatch, {7: (_ledger(tmp_path, "seven", 3), "seven"),
                                  66: (_ledger(tmp_path, "other", 11), "other")})
        assert webui.status_payload(7)["users_total"] == 3
        # Second caller, inside the TTL: the entry above must not answer it.
        assert webui.status_payload(66)["users_total"] == 11
        assert set(webui._snaps) == {7, 66}

    def test_the_first_caller_is_still_served_from_cache(self, tmp_path,
                                                         monkeypatch):
        _as_account(monkeypatch, {7: (_ledger(tmp_path, "seven", 3), "seven")})
        webui.status_payload(7)
        at = webui._snaps[7]["at"]
        assert webui.status_payload(7)["users_total"] == 3
        assert webui._snaps[7]["at"] == at, "recomputed inside the TTL"

    def test_invalidation_drops_every_tenant(self, tmp_path, monkeypatch):
        # env.sh and the run mode are properties of the box, so a change to
        # either has to expire all of them, not only the caller's.
        _as_account(monkeypatch, {7: (_ledger(tmp_path, "seven", 3), "seven"),
                                  66: (_ledger(tmp_path, "other", 11), "other")})
        webui.status_payload(7); webui.status_payload(66)
        webui.invalidate_status()
        assert all(e["at"] == 0.0 for e in webui._snaps.values())

    def test_the_operator_path_keeps_its_own_entry(self, monkeypatch):
        # No account: the SSH-tunnel operator reading env.sh's ledger, which
        # is what this endpoint has always served and must keep serving.
        called = {}
        monkeypatch.setattr(webui, "_compute_status",
                            lambda account_id=None: called.setdefault(
                                "account", account_id) or {"users_total": 0})
        webui.status_payload(None)
        assert called["account"] is None
        assert None in webui._snaps


class TestTheEndpointResolvesTheMigrationOnScreen:
    def test_the_route_asks_account_context(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "webui.py"), encoding="utf-8").read()
        route = src.split('elif path == "/api/status":')[1][:220]
        assert "account_context.in_context" in route
        assert "status_payload()" not in route, "unscoped, serves env.sh"

    def test_a_tenant_gets_their_own_account(self):
        assert account_context.in_context(7, is_superadmin=False) == 7

    def test_a_superadmin_gets_the_running_migration(self, monkeypatch):
        monkeypatch.setattr(account_context.job_admission, "list_active",
                            lambda: [{"job_name": "migrate", "account_id": 7,
                                      "pid": 1}])
        monkeypatch.setattr(account_context.job_admission, "is_live",
                            lambda j: True)
        assert account_context.in_context(66, is_superadmin=True) == 7

    def test_a_superadmin_with_nothing_running_gets_their_own(self, monkeypatch):
        monkeypatch.setattr(account_context.job_admission, "list_active",
                            lambda: [])
        assert account_context.in_context(66, is_superadmin=True) == 66

    def test_a_seed_job_is_not_a_migration_on_screen(self, monkeypatch):
        # Seeding a test tenant is not the run a sidebar click is asking
        # about, and answering with it would show the wrong domain entirely.
        monkeypatch.setattr(account_context.job_admission, "list_active",
                            lambda: [{"job_name": "seed", "account_id": 9,
                                      "pid": 1}])
        monkeypatch.setattr(account_context.job_admission, "is_live",
                            lambda j: True)
        assert account_context.in_context(66, is_superadmin=True) == 66


class TestBothServersAnswerWithTheSameRule:
    """Mission Control renders api_server's user list under webui's header.

    When the two disagree the page shows two tenants at once, which is how
    "11 users tracked" came to sit above 201 running users.
    """

    def test_api_server_delegates_rather_than_keeping_a_copy(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "api_server.py"), encoding="utf-8").read()
        body = src.split("def _account_in_context")[1].split("\ndef ")[0]
        assert "account_context.in_context" in body
        assert "job_admission.list_active" not in body, "a second copy"

    def test_they_agree_for_the_superadmin_case_that_broke(self, monkeypatch):
        import api_server
        monkeypatch.setattr(account_context.job_admission, "list_active",
                            lambda: [{"job_name": "delta", "account_id": 7,
                                      "pid": 1}])
        monkeypatch.setattr(account_context.job_admission, "is_live",
                            lambda j: True)
        op = api_server.Operator(name="boss", role="admin", account_id=66,
                                 is_superadmin=True)
        assert api_server._account_in_context(op) == \
            account_context.in_context(66, True) == 7
