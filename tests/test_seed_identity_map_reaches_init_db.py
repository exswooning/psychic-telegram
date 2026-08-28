"""The seed's identity map has to land where init-db reads it, naming the
real target tenant.

Both halves of that sentence were false, and the two faults compounded into
a migration aimed at a tenant that does not exist:

  * the seeder runs with cwd=<root>/data-generator (see the /api/seed
    handler), so its default relative --identities-out wrote
    data-generator/identities.csv, while the wizard's "Create database +
    load identities" action runs from <root> and reads <root>/identities.csv.
    Nothing joined them. After a 201-user seed, that button loaded a ten-row
    file left behind by an unrelated tenant pair -- and the wizard would then
    have reported step 4 satisfied.

  * seed_argv overlaid only SOURCE_* onto the child, but seed_sandbox.py
    writes every row as `<localpart>@{settings.target_domain}`. With no
    TARGET_DOMAIN in the child's environment that read fell through to
    env.sh's global placeholder, so seeding source.rohitrokaya.com.np
    produced 201 rows pointing at a.example.com.
"""
import os

import pytest

import webui


@pytest.fixture()
def _account(monkeypatch):
    """An account whose tenant config differs from the ambient env.sh, which
    is the only condition under which either bug is visible."""
    class _St:
        source_domain = "src.rohit.example"
        target_domain = "tgt.rohit.example"
        source_admin = "info@src.rohit.example"
        target_admin = "info@tgt.rohit.example"
        source_sa_key = "/keys/source-sa.json"
        db_path = "/root/migration/accounts/66/migration.db"

    monkeypatch.setattr("config.Settings", lambda **kw: _St())
    monkeypatch.setenv("SOURCE_DOMAIN", "stale-src.example")
    monkeypatch.setenv("TARGET_DOMAIN", "stale-tgt.example")
    monkeypatch.delenv("PROTECTED_DOMAINS", raising=False)


def _build():
    return webui.seed_argv(
        {"confirm_domain": "src.rohit.example", "scale": "small"}, account_id=66)


def test_identity_map_is_written_where_init_db_reads_it(_account):
    argv, _, err = _build()
    assert err == ""
    out = argv[argv.index("--identities-out") + 1]
    assert os.path.isabs(out), f"relative path resolves against the seeder's own cwd: {out}"
    # The exact file main.py init-db --identities identities.csv opens.
    assert out == os.path.join(webui.HERE, "identities.csv")


def test_the_child_is_told_the_real_target_domain(_account):
    _, env, err = _build()
    assert err == ""
    assert env["TARGET_DOMAIN"] == "tgt.rohit.example"
    assert env["TARGET_ADMIN"] == "info@tgt.rohit.example"


def test_the_source_overlay_still_holds(_account):
    _, env, _ = _build()
    assert env["SOURCE_DOMAIN"] == "src.rohit.example"
    assert env["MIGRATION_DB"].endswith("/66/migration.db")


def test_an_unscoped_call_is_left_alone(monkeypatch):
    """account_id=None is the pre-existing single-tenant path: it reads
    env.sh and must not gain an overlay that was never there.

    Asserted as "seed_argv changed nothing", not as "the key is absent":
    the returned env is built from os.environ, so an ambient MIGRATION_DB
    is inherited legitimately and an absence check fails on whatever the
    surrounding suite happens to have exported.
    """
    monkeypatch.setenv("SOURCE_DOMAIN", "sandbox-src.example")
    monkeypatch.setenv("TARGET_DOMAIN", "sandbox-tgt.example")
    monkeypatch.delenv("PROTECTED_DOMAINS", raising=False)
    before = os.environ.get("MIGRATION_DB")
    _, env, err = webui.seed_argv({"confirm_domain": "sandbox-src.example"})
    assert err == ""
    assert env.get("MIGRATION_DB") == before
    # The ambient domains are passed straight through, not replaced by an
    # account's tenant_configs row.
    assert env["SOURCE_DOMAIN"] == "sandbox-src.example"
    assert env["TARGET_DOMAIN"] == "sandbox-tgt.example"


def test_every_seed_writes_the_map_where_init_db_reads(monkeypatch):
    """Unscoped runs too -- the path fault was never account-specific."""
    monkeypatch.setenv("SOURCE_DOMAIN", "sandbox-src.example")
    monkeypatch.setenv("TARGET_DOMAIN", "sandbox-tgt.example")
    monkeypatch.delenv("PROTECTED_DOMAINS", raising=False)
    argv, _, err = webui.seed_argv({"confirm_domain": "sandbox-src.example"})
    assert err == ""
    assert argv[argv.index("--identities-out") + 1] == os.path.join(
        webui.HERE, "identities.csv")
