"""The tenant pair on screen must be the signed-in account's.

/api/config read env.sh unconditionally, so a SaaS account saw whatever
placeholder tenants the box happened to carry. Confirmed live: the header
read "Source: c.example.com" for an account whose real source tenant is
source.rohitrokaya.com.np with 200 users -- on the one line of chrome
visible from every page.

It is the same fault _account_env() already fixes for the action buttons,
where a signed-in tenant pressing a button got a report about a different
domain entirely.
"""

from __future__ import annotations

import webui


class TestReadingIt:
    def test_without_an_account_it_still_describes_the_box(self, monkeypatch):
        """A single-tenant install, and every install predating accounts, has
        no tenant_configs row and is correctly described by env.sh."""
        monkeypatch.setattr(webui, "ENV_PATH", "/nonexistent/env.sh")
        cfg = webui.read_config()
        assert set(cfg) == {"source_domain", "target_domain",
                            "source_admin", "target_admin"}

    def test_an_account_overrides_the_box_config(self, monkeypatch):
        class _S:
            source_domain = "source.real.example"
            target_domain = "target.real.example"
            source_admin = "admin@source.real.example"
            target_admin = "admin@target.real.example"

        import config as cfgmod
        monkeypatch.setattr(cfgmod, "Settings", lambda account_id=None: _S())
        got = webui.read_config(66)
        assert got["source_domain"] == "source.real.example"
        assert got["target_admin"] == "admin@target.real.example"

    def test_a_broken_tenant_config_falls_back_instead_of_500ing(self, monkeypatch):
        """This is the header. It must never take a page down."""
        import config as cfgmod

        def boom(account_id=None):
            raise RuntimeError("no such table: tenant_configs")

        monkeypatch.setattr(cfgmod, "Settings", boom)
        got = webui.read_config(66)          # must not raise
        assert set(got) == {"source_domain", "target_domain",
                            "source_admin", "target_admin"}

    def test_an_empty_tenant_value_does_not_blank_the_box_value(self, monkeypatch):
        """A half-configured account should not erase what env.sh knows."""
        class _S:
            source_domain = ""
            target_domain = "target.real.example"
            source_admin = None
            target_admin = ""

        import config as cfgmod
        monkeypatch.setattr(cfgmod, "Settings", lambda account_id=None: _S())
        monkeypatch.setattr(webui, "read_config", webui.read_config)
        got = webui.read_config(66)
        assert got["target_domain"] == "target.real.example"
        # blank ones fell through to whatever env.sh had, not to ""
        assert "source_domain" in got


class TestTheEndpointPassesTheAccount:
    def test_get_config_is_account_scoped(self):
        src = open(webui.__file__, encoding="utf-8").read()
        assert "read_config(self._on_screen())" in src

    def test_saving_writes_the_account_tenant_config_too(self):
        """Reading from tenant_configs while writing only env.sh would let an
        account correct its domain, be told "saved", and see nothing change
        anywhere -- worse than the bug it replaced."""
        src = open(webui.__file__, encoding="utf-8").read()
        block = src.split('if self.path == "/api/config":')[1][:1600]
        assert "update_tenant_config" in block
        assert '"source"' in block and '"target"' in block


class TestActionsUseTheAccountTenantToo:
    def test_setup_builds_its_command_from_the_account_config(self):
        """Worse than the header: /api/setup puts these domains on a
        setup.sh command line, so an unscoped read does not merely show the
        wrong tenant -- it runs against it."""
        src = open(webui.__file__, encoding="utf-8").read()
        block = src.split('if self.path == "/api/setup":')[1][:400]
        assert "read_config(self._on_screen())" in block

    def test_no_unscoped_read_config_calls_remain(self):
        """Each one is a place a signed-in tenant sees, or acts on, another
        tenant's domains."""
        src = open(webui.__file__, encoding="utf-8").read()
        assert "read_config()" not in src
