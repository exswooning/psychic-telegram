"""The installer must line up with the real deployment -- units it enables
have to exist, the Caddyfile it patches has to exist, and it must never bake
localhost into the SPA build."""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SH = open(os.path.join(ROOT, "install.sh"), encoding="utf-8").read()


def test_enables_only_units_that_exist():
    for unit in ("bitport-webui.service", "bitport-api.service",
                 "xvfb.service", "bitport-backup.timer"):
        assert unit in SH, f"installer never mentions {unit}"
        assert os.path.isfile(os.path.join(ROOT, "systemd", unit)), \
            f"installer enables {unit} but systemd/{unit} is missing"


def test_builds_spa_with_relative_api_base():
    # a baked-in localhost:8090 breaks every browser -- the known bug
    assert "VITE_CP_BASE=''" in SH or 'VITE_CP_BASE=""' in SH


def test_patches_hardcoded_install_path():
    # units hardcode /root/migration; the installer must rewrite it
    assert "s#/root/migration#$INSTALL_DIR#g" in SH


def test_config_is_root_only_and_outside_checkout():
    assert "/etc/bitport" in SH
    assert "install -d -m 700 /etc/bitport" in SH


def test_requires_a_strong_admin_password():
    assert "-ge 12" in SH        # refuses a weak superadmin password


def test_installer_creates_the_schema_before_use():
    # the control-plane schema must be applied, or a fresh box fails with
    # "unable to open database file" at the superadmin step
    assert "apply_migrations" in SH
    # and it must run before the superadmin account step
    assert SH.index("apply_migrations") < SH.index("Superadmin account")


def test_installer_scans_and_cleans_broken_installs():
    # a prior/broken install must be detected and cleaned, before packages
    assert "Scan for prior/broken installs" in SH
    assert "dpkg --configure -a" in SH        # repairs a wedged package system
    assert "systemctl stop" in SH             # stops an existing/broken service
    # it must not kill its own process group (the self-kill hazard)
    assert 'pg" != "$MYPGID"' in SH or 'MYPGID' in SH
    assert SH.index("Scan for prior/broken installs") < SH.index("System packages")
