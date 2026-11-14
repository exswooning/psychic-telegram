"""Whatever a first-time user hits, Bitport fixes -- not the user (operator, 14 Nov 2026).

Found by installing Bitport on a clean Ubuntu 24.04 machine and following only what it said: the README never
mentioned install.sh; the installer ran silent for 40 minutes on a slow link; a generated admin password was
never shown to anyone; it called its own Caddy a stranger on port 80 and told the user to type ':81'; it
printed '<server-ip>'; it said 'SSH is key-only' while passwords were still on; and no short command opened
the dashboard."""
import os
import re
import stat
import subprocess
import textwrap

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = open(os.path.join(ROOT, "install.sh")).read()


def _bash(script, tmp_path, **env):
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", **env})


def _fake(tmp_path, name, body):
    p = tmp_path / name
    p.write_text("#!/bin/sh\n" + body + "\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)


def _port_free_fn():
    return re.search(r"^port_free\(\) \{.*\}$", INSTALL, re.M).group(0)


def test_a_port_held_only_by_caddy_is_free_for_the_installer(tmp_path):
    _fake(tmp_path, "ss", 'echo \'LISTEN 0 4096 *:80 *:* users:(("caddy",pid=7,fd=6))\'')
    r = _bash(f'{_port_free_fn()}\nport_free 80 && echo FREE || echo BUSY', tmp_path)
    assert r.stdout.strip() == "FREE", r.stderr


def test_a_port_held_by_anything_else_is_busy(tmp_path):
    _fake(tmp_path, "ss", 'echo \'LISTEN 0 4096 *:80 *:* users:(("apache2",pid=7,fd=6))\'')
    assert _bash(f'{_port_free_fn()}\nport_free 80 && echo FREE || echo BUSY', tmp_path).stdout.strip() == "BUSY"


def test_an_unused_port_is_free(tmp_path):
    _fake(tmp_path, "ss", "true")
    assert _bash(f'{_port_free_fn()}\nport_free 80 && echo FREE || echo BUSY', tmp_path).stdout.strip() == "FREE"


def test_a_generated_password_reaches_the_user():
    assert "PASSWORD_GENERATED=1" in INSTALL and "/root/bitport-admin-password.txt" in INSTALL
    assert 'password:  $BITPORT_ADMIN_PASSWORD' in INSTALL, "the end screen prints it"
    assert "(as supplied / generated -- check your env" not in INSTALL


def test_the_end_screen_has_a_real_address_and_the_next_steps():
    assert "<server-ip>" not in INSTALL
    assert "hostname -I" in INSTALL and "Setup Wizard" in INSTALL and "bitport ui" in INSTALL


def test_ssh_says_what_is_true():
    assert "sshd -T" in INSTALL and "SSH still accepts passwords" in INSTALL


def test_long_silent_steps_say_they_are_alive():
    assert "with_heartbeat" in INSTALL and INSTALL.count("with_heartbeat \"installing Python packages\"") == 2
    assert "PIP_CACHE_DIR=/var/cache/bitport-pip" in INSTALL


def test_the_background_install_stays_on_screen_until_it_is_done():
    assert "tail -n +1 -f --pid=$!" in INSTALL and "The install stopped before it finished" in INSTALL


def test_the_bitport_command_explains_itself(tmp_path):
    script = open(os.path.join(ROOT, "bitport-cli.sh")).read().replace("__INSTALL_DIR__", str(tmp_path))
    (tmp_path / "bitport").write_text(script)
    _fake(tmp_path, "id", "echo 0")
    r = _bash(f"bash {tmp_path}/bitport help", tmp_path)
    assert r.returncode == 0 and "bitport ui" in r.stdout and "bitport status" in r.stdout
    r = _bash(f"bash {tmp_path}/bitport frobnicate", tmp_path)
    assert r.returncode == 2 and "I do not know 'frobnicate'" in r.stdout


def test_the_readme_points_a_new_user_at_the_install_guide():
    readme = open(os.path.join(ROOT, "README.md")).read()
    assert readme.index("INSTALL.md") < readme.index("migration_engine/"), "before anything else"
    guide = open(os.path.join(ROOT, "INSTALL.md")).read()
    assert "sudo bash install.sh" in guide and "cPanel" in guide


def test_a_failing_step_stops_its_heartbeat_and_reports_its_status(tmp_path):
    """set -e used to end the script inside with_heartbeat: no message, and the heartbeat printed for ever."""
    fn = re.search(r"^with_heartbeat\(\) \{.*?^\}", INSTALL, re.M | re.S).group(0).replace("sleep 30", "sleep 0.2")
    script = textwrap.dedent(f"""
        set -euo pipefail
        DIM=; RESET=
        {fn}
        run() {{ eval "$@"; }}
        rc=0
        with_heartbeat testing run "sleep 0.5; false" || rc=$?
        echo "status=$rc"
        sleep 0.6
        echo done
    """)
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=20)
    assert "status=1" in r.stdout and r.stdout.rstrip().endswith("done")
    assert r.stdout.count("still working") <= 4, "the heartbeat stopped when the step failed"


def _stop_harness(body):
    step = re.search(r"^CURRENT_STEP=.*\n^step\(\).*$", INSTALL, re.M).group(0)
    fn = re.search(r"^on_unexpected_stop\(\) \{.*?^\}\n^trap on_unexpected_stop ERR$", INSTALL, re.M | re.S).group(0)
    script = f"set -euo pipefail\nBOLD=; RESET=; RED=\n{step}\n{fn}\n{body}"
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=20)


def test_an_unexpected_stop_says_where_and_what_to_do():
    r = _stop_harness('step "Python environment"\nfalse\necho not-reached')
    assert r.returncode != 0 and "not-reached" not in r.stdout
    assert "stopped unexpectedly during: Python environment" in r.stdout and "safe to repeat" in r.stdout


def test_a_best_effort_step_that_ignores_failures_stays_quiet():
    r = _stop_harness('step "Scan"\nset +e\nfalse\nls /nonexistent-dir 2>/dev/null\nset -e\necho carried-on')
    assert r.returncode == 0 and "carried-on" in r.stdout and "stopped unexpectedly" not in r.stdout


def test_pip_is_retried_like_every_other_network_step():
    assert INSTALL.count('with_heartbeat "installing Python packages" runnet') == 2
    assert "Installing the Python packages failed" in INSTALL
