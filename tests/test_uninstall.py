"""Taking Bitport off a machine must not take the tenant with it.

Two things in an install directory cannot be recreated by reinstalling.
keys/ holds service-account credentials for real Google tenants -- new ones
mean re-doing domain-wide delegation on both tenants by hand. migration.db
and data/ hold the record of what was already copied, and Drive's duplicate
check reads it: lose the ledger and a resumed migration re-copies every file
it already moved (MULTINODE.md spells out why Gmail survives this and Drive
does not).

So the default removes the software and keeps both. Verified by running it
against a fabricated install tree: code and .venv gone, keys/, migration.db,
data/accounts/7/migration.db and identities.csv all byte-identical
afterwards; --purge refused a wrong confirmation and left all four standing.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(name: str) -> str:
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


SH = _read("uninstall.sh")
PS1 = _read("uninstall_node.ps1")
INSTALL = _read("install.sh")


class TestTheDefaultKeepsWhatCannotBeRemade:
    def test_both_name_the_same_keepable_set(self):
        for body in (SH, PS1):
            for item in ("keys", "migration.db", "data", "identities.csv"):
                assert item in body, item

    def test_deleting_them_needs_a_separate_flag(self):
        assert "--purge" in SH
        assert "BITPORT_UNINSTALL_PURGE" in PS1

    def test_and_a_typed_confirmation_on_top_of_it(self):
        """A --purge that took a bare -y would be one shell-history arrow-up
        away from destroying somebody's tenant credentials."""
        assert 'Type PURGE to confirm' in SH
        assert '$reply -ne "PURGE"' in PS1

    def test_doing_nothing_is_the_default(self):
        """Run with no flags it reports and exits, so the first thing anyone
        does is see the list rather than lose it."""
        assert "Nothing was changed" in SH
        assert "Nothing was changed" in PS1


class TestItRefusesToActOnTheWrongThing:
    def test_system_directories_are_rejected(self):
        """--dir is caller-supplied and the script runs as root."""
        assert re.search(r"/bin\|/boot\|/etc", SH)
        assert "refusing to treat" in SH

    def test_it_will_not_delete_under_a_running_migration(self):
        """The tree would go while a migration writes into it: it neither
        stops nor finishes, and the ledger it was updating is exactly what a
        resume would need."""
        assert "refusing while it runs" in SH
        assert "refusing while it runs" in PS1
        assert "--force" in SH

    def test_the_busy_check_cannot_itself_block_the_uninstall(self):
        """Windows-only: with $ErrorActionPreference='Stop' a cmdlet that is
        absent is a TERMINATING error and -ErrorAction does not cover
        command-not-found -- the same shape that killed install_node.ps1 on
        the Store's python stub. A safety check that throws is worse than no
        check at all."""
        busy = PS1[PS1.index("$busy = @()"):PS1.index("$keepable")]
        assert "try {" in busy and "} catch {" in busy

    def test_it_finds_the_directory_from_the_unit_not_a_guess(self):
        """install.sh takes --dir and rewrites WorkingDirectory into it, so
        /root/migration is a default and not a fact."""
        assert "WorkingDirectory=" in SH


class TestItLeavesTheMachinesOtherSoftwareAlone:
    def test_caddy_itself_is_never_removed(self):
        """A general-purpose web server this box may be using for something
        else -- the machine this ran on already had Nextcloud and SABnzbd."""
        assert "apt-get remove" not in SH
        assert "caddy itself is NOT removed" in SH

    def test_a_caddyfile_that_is_not_ours_is_left_standing(self):
        assert "127.0.0.1:8090" in SH
        assert "not Bitport's -- left alone" in SH

    def test_the_installer_backs_the_caddyfile_up_so_this_can_restore_it(self):
        """install.sh overwrote /etc/caddy/Caddyfile with a plain `>`, so
        without a backup there was nothing to put back and another site on
        the same machine was simply gone."""
        assert "Caddyfile.bitport-backup" in INSTALL
        assert "Caddyfile.bitport-backup" in SH

    def test_the_backup_is_never_overwritten_by_a_second_install(self):
        """Re-running the installer would otherwise replace the real
        original with Bitport's own config."""
        assert "[ ! -f /etc/caddy/Caddyfile.bitport-backup ]" in INSTALL

    def test_python_is_not_uninstalled_on_windows(self):
        assert "left alone" in PS1


class TestItUndoesWhatTheInstallerDid:
    def test_every_unit_the_installer_enables_is_removed(self):
        units = re.findall(r"(bitport-[a-z]+\.(?:service|timer)|xvfb\.service)",
                           INSTALL)
        for unit in set(units):
            assert unit in SH, f"install.sh touches {unit}; uninstall.sh never does"

    def test_it_reloads_systemd_afterwards(self):
        assert "daemon-reload" in SH
