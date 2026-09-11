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


def test_scan_step_is_best_effort_not_fatal():
    # The scan step is pure cleanup of a PRIOR install; a hidden non-zero in
    # it (a BusyBox `ps` that rejects -o pgid=, a missing pgrep) must never
    # abort THIS install via `set -e`+pipefail. Regression: an installer that
    # died silently right after printing the scan header on a fresh box.
    scan = SH[SH.index("Scan for prior/broken installs"):SH.index("System packages")]
    assert "set +e" in scan, "scan step must disable -e so cleanup can't abort the install"
    assert "set -e" in scan.split("set +e", 1)[1], "scan step must restore -e before real work"
    # and it only hunts strays when it actually knows its own group
    assert 'if [ -n "$MYPGID" ]' in scan


def test_stray_hunt_cannot_kill_its_own_sudo_parent():
    # "install.sh" is a substring of "bitport-selfinstall.sh"; sudo runs the
    # installer in a different process group, so a bare `pgrep -f install.sh`
    # would flag the run's own sudo parent as a stray and SIGKILL the whole
    # run. Regression: the installer died with "Killed" at the scan step.
    #
    # This first asserted the guard was `$$` plus `$PPID`, which is one level
    # short and let the SAME symptom come back -- see the ancestor test
    # below. The pattern half is unchanged and still load-bearing.
    scan = SH[SH.index("Scan for prior/broken installs"):SH.index("System packages")]
    assert 'pgrep -f "[ /]install\\.sh"' in scan, \
        "stray-hunt pattern must require a space/slash so selfinstall.sh can't match"
    assert '"$ANCESTORS"' in scan, \
        "stray hunt must never signal any process it is running underneath"


def test_packagers_exclude_dev_env_sh():
    # env.sh is gitignored but the packagers use rsync/tar directly, which do
    # not consult .gitignore. api_server.py's main() loads env.sh into its
    # process env unconditionally on startup (so subprocesses inherit tenant
    # config); shipping the packaging author's own dev env.sh once poisoned a
    # fresh install with their local machine's MIGRATION_DB path, sending
    # bitport-api into a permanent "unable to open database file" crash loop.
    tarball = open(os.path.join(ROOT, "make-tarball.sh"), encoding="utf-8").read()
    selfinstall = open(os.path.join(ROOT, "make-selfinstall.sh"), encoding="utf-8").read()
    assert "exclude='env.sh'" in tarball or "exclude=env.sh" in tarball
    assert "exclude=env.sh" in selfinstall or "exclude='env.sh'" in selfinstall


def test_no_domain_install_gets_non_secure_cookie():
    # systemd/bitport-api.service hardcodes BITPORT_COOKIE_SECURE=1, correct
    # only when Caddy terminates real HTTPS. install.sh's own no-domain
    # branch serves plain HTTP -- a browser silently drops a Secure cookie
    # over HTTP, so login succeeds server-side but every following request
    # looks signed-out and the SPA bounces back to /login forever.
    # Regression: confirmed live (login 200 + Set-Cookie, but /auth/me still
    # 401'd on the next request) until this patch was applied.
    services_block = SH[SH.index('step "Services"'):SH.index('step "Reverse proxy"')]
    assert "BITPORT_COOKIE_SECURE=1" in services_block
    assert "BITPORT_COOKIE_SECURE=0" in services_block
    assert '-z "$BITPORT_DOMAIN"' in services_block


def test_installer_auto_backgrounds_an_interactive_run():
    # A human running this by hand over SSH used to lose the whole install
    # the moment their connection dropped -- "Killed" or a broken pipe
    # partway through, recoverable only by knowing to re-run inside tmux.
    # Once settings are gathered, install.sh must re-exec itself detached
    # (setsid+nohup) so a dropped connection can't take the install with it,
    # UNLESS the caller was already unattended (CI/Ansible), which controls
    # its own supervision and must keep running synchronously.
    assert "ORIG_NONINTERACTIVE=" in SH
    assert "setsid nohup bash" in SH
    assert 'ORIG_NONINTERACTIVE" != 1' in SH
    # the daemonize block must sit after Settings validation, before Scan
    assert SH.index("Superadmin password") < SH.index("setsid nohup bash") \
        < SH.index("Scan for prior/broken installs")


def test_installer_picks_free_ports_instead_of_failing():
    # webui.py's default 8080 and Caddy's default :80 are common ports for
    # OTHER software on a shared box to have grabbed first (asuswb had
    # SABnzbd on 8080, Nextcloud's Apache on 80) -- Bitport's own services
    # crash-looped/failed forever until someone noticed and hand-patched the
    # units. install.sh must detect a busy default and fall back to a free
    # port automatically, for both the internal webui port and (when no
    # domain is set, so Caddy isn't required to use the ACME-mandated 80/443)
    # the public Caddy port.
    assert "pick_port" in SH and "port_free" in SH
    assert "WEBUI_PORT=$(pick_port 8080)" in SH
    assert "PUBLIC_PORT=$(pick_port 80)" in SH
    # the webui target must be patched into BOTH the unit and BOTH Caddyfile
    # branches (domain and no-domain), not just one
    assert "--port $WEBUI_PORT" in SH
    assert "127.0.0.1:$WEBUI_PORT" in SH


def test_selfinstall_temp_dir_is_actually_cleaned_up():
    # The wrapper's `trap ... EXIT` never fires because `exec bash install.sh`
    # replaces that process image entirely -- this silently leaked a fresh
    # /tmp/bitport-src.XXXXXX on every single run (five accumulated on one
    # box before anyone noticed). install.sh must remove it itself, as the
    # last thing it does, using the DEST path the wrapper hands it via env.
    selfinstall = open(os.path.join(ROOT, "make-selfinstall.sh"), encoding="utf-8").read()
    assert 'export BITPORT_SRC_DEST="$DEST"' in selfinstall
    assert 'rm -rf "$BITPORT_SRC_DEST"' in SH


def test_caddyfile_is_readable_by_the_caddy_user():
    # Both Caddyfile branches write with a plain `>`, which takes the
    # invoking shell's umask -- and the installer daemonises itself, so that
    # umask is whatever systemd/ssh handed it. Where the file already exists
    # the redirect keeps the package's 0644, which is why this only appeared
    # on a genuinely clean install: it landed 0600 root:root and caddy died
    # with "reading config from file: ... permission denied".
    assert "chmod 644 /etc/caddy/Caddyfile" in SH


def test_the_reverse_proxy_step_verifies_caddy_actually_started():
    # It printed "caddy configured" on a run where caddy had just failed to
    # start: backends up, health check green, and the URL in the summary
    # served nothing at all.
    block = SH[SH.index('step "Reverse proxy"'):SH.index('step "Superadmin account"')]
    assert "systemctl is-active caddy" in block
    assert "caddy is NOT running" in block


def test_stray_hunt_skips_every_ancestor_not_just_the_parent():
    """The installer SIGKILLed the sudo it was running under.

    sudo allocating a pty forks a MONITOR child that calls setsid(), so the
    tree is  sudo -> sudo monitor ($PPID) -> bash install.sh ($$).  Both
    sudo argv strings contain "bash install.sh", so pgrep matches all three;
    the grandparent sits in a different process group and passed both the
    $$ and $PPID guards. Live on Ubuntu 26.04 that printed

        ==> Scan for prior/broken installs
        Killed    sudo -E INSTALL_DIR=/opt/bitport ... bash install.sh

    which reads like an OOM kill and is not one. Reproduced under `ssh -tt`;
    without a pty sudo forks no monitor and every pid shares one process
    group, which is exactly why this hid from the earlier fix.
    """
    scan = SH[SH.index('step "Scan for prior/broken installs"'):]
    scan = scan[:scan.index("# 2.")]
    assert "ANCESTORS" in scan, "no ancestor set is built"
    # Walks ppids rather than trusting $PPID alone.
    assert "ps -o ppid=" in scan
    assert 'case "$ANCESTORS" in *" $pid "*) continue' in scan
    # The old two-name guard must be gone: it is what let the grandparent
    # through, and leaving it beside the new one would read as belt-and-
    # braces while still being the thing that failed.
    assert '[ "$pid" = "$PPID" ]' not in scan


def test_the_ancestor_walk_cannot_spin_forever():
    """`ps` returning something unexpected must end the loop, not hang the
    installer before it has done anything."""
    scan = SH[SH.index('step "Scan for prior/broken installs"'):]
    scan = scan[:scan.index("# 2.")]
    assert "_depth" in scan and "-lt 32" in scan


def test_it_still_kills_a_genuine_stray():
    """The guard must not have been widened into doing nothing: a real
    leftover installer from an earlier run is the reason this step exists."""
    scan = SH[SH.index('step "Scan for prior/broken installs"'):]
    scan = scan[:scan.index("# 2.")]
    assert 'kill -9 "$pid"' in scan
    assert '"$pg" != "$MYPGID"' in scan


def test_a_rerun_does_not_reset_the_password_it_invented():
    """install.sh promises "Safe to re-run: every step checks before it acts"
    at the top, and the superadmin step did not.

    It resets an existing account's password to whatever BITPORT_ADMIN_PASSWORD
    holds. On a re-run without one, NONINTERACTIVE makes ask_secret INVENT a
    password and print it only as "(generated)" -- so re-running to pick up
    new code would silently change the password of the account you log in
    with, to a string nobody ever saw. A lockout from your own install, from
    the documented way to update it.

    The reset now happens only when a human actually supplied a password.
    """
    assert "PASSWORD_GIVEN=0" in SH
    assert "PASSWORD_GIVEN=1" in SH
    assert 'BP_PASS_GIVEN="$PASSWORD_GIVEN"' in SH
    step = SH[SH.index('step "Superadmin account"'):]
    assert "if given:" in step
    assert step.index("if given:") < step.index("manage_account.set_password")
    assert "password left as it was" in step


def test_both_ways_of_supplying_one_count_as_given():
    """From the environment (the unattended path) and from the prompt (the
    interactive one). A blank prompt means "generate", which does not."""
    fn = SH[SH.index("ask_secret() {"):SH.index("\nask INSTALL_DIR")]
    assert fn.count("PASSWORD_GIVEN=1") == 2
    assert '[ -n "$reply" ] && PASSWORD_GIVEN=1' in fn
