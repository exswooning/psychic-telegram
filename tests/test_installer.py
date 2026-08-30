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
