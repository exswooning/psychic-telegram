"""A node was handed every customer's password hash.

node_setup.sh copied the coordinator's own migration.db to each node, and
install_node.sh/.ps1 printed the same instruction at the end of a
successful join -- which is where it was noticed, on a real laptop.

On this deployment that file is 70 MB and contains the accounts table
(password_hash for every customer), live session tokens, the operator audit
log, and every other tenant's configuration. What a node actually reads, in
config.py's _load_account_tenant_config, is:

    SELECT side, domain, admin_email, sa_key_path, db_path
      FROM tenant_configs WHERE account_id = ?

Two rows, five columns. export_node_config.py writes that and nothing else,
measured at 4 KB against a 70 MB original, with the real password hash and
the other tenant's domain both verifiably absent from the output.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

import accounts_auth as aa
import control_plane_db as cpdb
import export_node_config
from db import MigrationDB

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(name: str) -> str:
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture
def two_tenants(monkeypatch):
    path = tempfile.mktemp(suffix=".db")
    monkeypatch.setenv("MIGRATION_DB", path)
    MigrationDB(path)
    cpdb.apply_migrations()
    mine = aa.create_account("victim@x.test", "SuperSecret12345", "Victim")
    other = aa.create_account("other@y.test", "AnotherSecret999", "Other")
    aa.update_tenant_config(mine, "source", domain="src.acme",
                            admin_email="admin@src.acme")
    aa.update_tenant_config(mine, "target", domain="tgt.acme",
                            admin_email="admin@tgt.acme")
    aa.update_tenant_config(other, "source", domain="other.co",
                            admin_email="admin@other.co")
    yield mine, other, path
    for p in (path,):
        try:
            os.unlink(p)
        except OSError:
            pass


class TestTheExportCarriesNoSecrets:
    def test_the_real_password_hash_is_not_in_it(self, two_tenants):
        mine, _, src = two_tenants
        with cpdb.ro() as c:
            real = c.execute("SELECT password_hash FROM accounts WHERE id=?",
                             (mine,)).fetchone()["password_hash"]
        out = tempfile.mktemp(suffix=".db")
        export_node_config.export(mine, out)
        assert real.encode() not in open(out, "rb").read()
        os.unlink(out)

    def test_no_other_tenant_appears(self, two_tenants):
        mine, _, _ = two_tenants
        out = tempfile.mktemp(suffix=".db")
        export_node_config.export(mine, out)
        assert b"other.co" not in open(out, "rb").read()
        os.unlink(out)

    def test_no_sessions_travel(self, two_tenants):
        mine, _, _ = two_tenants
        aa.create_session(mine)
        out = tempfile.mktemp(suffix=".db")
        export_node_config.export(mine, out)
        conn = sqlite3.connect(out)
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        conn.close(); os.unlink(out)

    def test_it_does_carry_what_the_node_reads(self, two_tenants):
        mine, _, _ = two_tenants
        out = tempfile.mktemp(suffix=".db")
        export_node_config.export(mine, out)
        conn = sqlite3.connect(out); conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT side, domain, admin_email, sa_key_path, db_path "
            "FROM tenant_configs WHERE account_id=?", (mine,)).fetchall()
        assert {r["side"] for r in rows} == {"source", "target"}
        assert {r["domain"] for r in rows} == {"src.acme", "tgt.acme"}
        conn.close(); os.unlink(out)

    def test_it_will_not_overwrite(self, two_tenants):
        mine, _, _ = two_tenants
        out = tempfile.mktemp(suffix=".db")
        export_node_config.export(mine, out)
        with pytest.raises(SystemExit, match="refusing to overwrite"):
            export_node_config.export(mine, out)
        os.unlink(out)

    def test_an_unknown_account_fails_loudly(self, two_tenants):
        out = tempfile.mktemp(suffix=".db")
        with pytest.raises(SystemExit, match="no tenant_configs rows"):
            export_node_config.export(9999, out)


class TestNothingStillShipsTheWholeDatabase:
    def test_node_setup_uses_the_exporter(self):
        sh = _read("node_setup.sh")
        assert "export_node_config.py" in sh
        assert "/migration.db\" \"$TMP/migration.db\"" not in sh

    def test_node_setup_no_longer_ships_env_sh(self):
        """It is the legacy single-tenant config, unread by a node running
        with --account-id, and on this deployment it carries a live
        GROQ_API_KEY."""
        sh = _read("node_setup.sh")
        assert "$TMP/env.sh" not in sh

    def test_the_installers_tell_you_to_export(self):
        for name in ("install_node.sh", "install_node.ps1"):
            body = _read(name)
            assert "export_node_config.py" in body, name
            assert "node-config.db" in body, name

    def test_they_no_longer_hardcode_one_coordinators_path(self):
        """/opt/bitport was printed to a node that had just joined a
        coordinator installed at /root/migration."""
        for name in ("install_node.sh", "install_node.ps1"):
            body = _read(name)
            assert "<bitport-dir>" in body, name

    def test_multinode_says_which_file(self):
        md = _read("MULTINODE.md")
        assert "export_node_config.py" in md
        assert "password_hash" in md
