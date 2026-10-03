"""Seed and migrate each get their own key on the source, delegated exactly what
their own job requests. One key shared by both meant the migration's source
credential could write, and narrowing it back was undone by the next seed."""
import json

import pytest

import separate_credentials as sc
import verify_scopes
from config import Settings

AUTH = "https://www.googleapis.com/auth/"
WRITES = {AUTH + s for s in ("gmail.insert", "gmail.modify", "gmail.labels", "chat.spaces",
                             "chat.messages", "chat.delete", "calendar", "contacts", "tasks",
                             "admin.directory.user", "admin.directory.group")} | {"https://mail.google.com/"}


class TestTheTwoSets:
    def test_the_migrate_set_holds_no_seed_write_scope(self):
        assert not WRITES & set(sc.migrate_scopes(Settings()))

    def test_it_keeps_what_a_server_side_copy_and_both_transfer_modes_need(self):
        m = set(sc.migrate_scopes(Settings()))
        assert {AUTH + "drive", AUTH + "drive.readonly", AUTH + "chat.spaces.readonly",
                AUTH + "chat.messages.readonly", AUTH + "calendar.acls.readonly"} <= m

    def test_the_seed_set_holds_what_the_seeder_and_its_reset_request(self):
        s = set(sc.seed_scopes())
        assert WRITES <= s and AUTH + "admin.reports.usage.readonly" in s

    def test_what_a_run_checks_is_what_is_granted_to_the_migrate_key(self):
        """The run's own gate asks required_scopes(); if that still wanted the seed
        set, every migration would 're-grant' the write scopes straight back."""
        st = Settings()
        req = set(verify_scopes.required_scopes(st, "source"))
        assert not WRITES & req
        assert req <= set(verify_scopes.grant_scopes(st, "source"))
        assert set(verify_scopes.grant_scopes(st, "source")) == set(sc.migrate_scopes(st))

    def test_the_target_is_unchanged(self):
        assert AUTH + "admin.directory.user" in verify_scopes.required_scopes(Settings(), "target")


class TestTheSeedKeyIsFound:
    def _key(self, path, project, client):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"project_id": project, "client_id": client}))

    def test_beside_the_source_key_or_any_account_sharing_its_project(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "HERE", str(tmp_path))
        src = tmp_path / "keys" / "3" / "source-sa.json"
        self._key(src, "proj-a", "111")
        self._key(tmp_path / "keys" / "2" / "seed-sa.json", "proj-a", "222")
        self._key(tmp_path / "keys" / "9" / "seed-sa.json", "proj-b", "999")
        st = Settings(); st.source_sa_key = str(src)
        assert sc.seed_key_path(st) == str(tmp_path / "keys" / "2" / "seed-sa.json")

    def test_none_when_the_tenant_has_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "HERE", str(tmp_path))
        src = tmp_path / "keys" / "3" / "source-sa.json"
        self._key(src, "proj-a", "111")
        st = Settings(); st.source_sa_key = str(src)
        assert sc.seed_key_path(st) is None


class TestSeparate:
    @pytest.fixture
    def wired(self, tmp_path, monkeypatch):
        src = tmp_path / "keys" / "3" / "source-sa.json"
        src.parent.mkdir(parents=True)
        src.write_text(json.dumps({"project_id": "proj-a", "client_id": "111"}))
        monkeypatch.setattr(sc, "HERE", str(tmp_path))
        st = Settings(); st.source_sa_key = str(src); st.source_domain = "src.example"
        import config
        monkeypatch.setattr(config, "Settings", lambda account_id=None: st)
        import scope_guard
        monkeypatch.setattr(scope_guard, "_console_login", lambda s, t: {
            "DWD_EMAIL_SOURCE": "a@src.example", "DWD_PASSWORD_SOURCE": "x"})
        made, grants = [], []

        def create(settings, dest, login):
            open(dest, "w").write(json.dumps({"project_id": "proj-a", "client_id": "222"}))
            made.append(dest)
            return True, "seed-sa@proj-a"
        monkeypatch.setattr(sc, "_create_seed_key", create)
        monkeypatch.setattr(sc, "_grant", lambda a, c, s, k, l: grants.append((c, set(s), k)) or True)
        return st, made, grants

    def test_it_creates_the_key_then_grants_seed_first_and_migrate_exactly(self, wired, monkeypatch):
        st, made, grants = wired
        # Live only once granted: the seed key's scopes after its entry is written,
        # and the migrate key never holds a seed write scope after its own.
        monkeypatch.setattr(sc, "_probe", lambda s, key, scopes: {
            x: key.endswith("seed-sa.json") and any(g[0] == "222" for g in grants) for x in scopes})
        assert sc.separate(3, wait=0) == 0
        assert made and [g[0] for g in grants] == ["222", "111"]
        assert grants[0][1] == set(sc.seed_scopes()) and grants[1][1] == set(sc.migrate_scopes(st))

    def test_a_rerun_leaves_a_live_seed_entry_alone(self, wired, monkeypatch):
        st, made, grants = wired
        monkeypatch.setattr(sc, "_probe", lambda s, key, scopes: {
            x: key.endswith("seed-sa.json") for x in scopes})
        assert sc.separate(3, wait=0) == 0
        assert [g[0] for g in grants] == ["111"]

    def test_it_reports_a_write_scope_still_live_on_the_migrate_key(self, wired, monkeypatch):
        monkeypatch.setattr(sc, "_probe", lambda s, key, scopes: {x: True for x in scopes})
        assert sc.separate(3, wait=0) == 7

    def test_it_refuses_without_the_source_admins_login(self, wired, monkeypatch):
        import scope_guard
        monkeypatch.setattr(scope_guard, "_console_login", lambda s, t: {})
        assert sc.separate(3) == 2
