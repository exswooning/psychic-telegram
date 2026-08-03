"""
deploy_remote.py
================
Put this migration tool onto a VPS and start its web UI there.

Why run remotely at all
-----------------------
A tenant migration runs for hours. A laptop sleeps, changes networks, and gets
closed. Every one of those interrupts a run -- recoverably, because the engine
resumes, but a migration that needs babysitting through six resume cycles is a
migration that will be got wrong. A VPS stays up.

Why this copies the tool rather than driving it over SSH
--------------------------------------------------------
The alternative design -- keep the UI on the laptop and send each command over
SSH -- means every action has two execution paths to keep in step, and the
migration still dies when the laptop sleeps mid-`migrate`, because the SSH
channel carrying it goes with it. Copying the tool to the VPS and running the
UI *there* leaves exactly one execution path. You then reach it through an SSH
tunnel, so the UI stays bound to loopback on the remote host and is never
exposed to the internet.

What gets copied
----------------
The code, and -- only with --include-credentials -- the service-account keys
and OAuth tokens. That flag is deliberate: those files let the holder read
every mailbox in both tenants, and sending them to a host should be a decision,
not a side effect of clicking Deploy.

    python3 deploy_remote.py --host 203.0.113.10 --user root
    python3 deploy_remote.py --host 203.0.113.10 --user root --include-credentials
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
HOST_RE = re.compile(rf"^(?:{_LABEL}\.)*{_LABEL}$|^\d{{1,3}}(?:\.\d{{1,3}}){{3}}$", re.I)
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$", re.I)

# Never copied: local state that would overwrite the remote's own, and the
# virtualenv, which is built for the wrong platform and OS.
EXCLUDES = [
    ".git/", "__pycache__/", ".pytest_cache/", ".venv/", "venv/",
    "scratch/", "migration.db", "migration.db-journal", "migration.db-wal",
    "*.log", "sandbox_manifest*.json", "*.pyc", ".DS_Store",
]
SECRET_EXCLUDES = ["keys/", "oauth/", "env.sh", "*.json.key", "identities*.csv"]


def say(msg: str) -> None:
    print(msg, flush=True)


def ssh_base(key: str, port: int) -> list[str]:
    """The -e argument for rsync, and the prefix for direct ssh calls."""
    parts = ["ssh", "-p", str(port),
             "-o", "BatchMode=yes",           # never sit waiting for a password
             "-o", "StrictHostKeyChecking=accept-new",
             "-o", "ConnectTimeout=15"]
    if key:
        parts += ["-i", key]
    return parts


def run(argv: list[str], timeout: int = 900) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, f"{argv[0]} is not installed locally"
    except subprocess.TimeoutExpired:
        return 124, "timed out"


def validate(host: str, user: str, port: int, key: str) -> str:
    if not HOST_RE.match(host or ""):
        return f"{host!r} is not a hostname or IPv4 address"
    if not USER_RE.match(user or ""):
        return f"{user!r} is not a valid username"
    if not (1 <= port <= 65535):
        return f"port {port} is out of range"
    if key:
        if not os.path.isfile(key):
            return f"no SSH key at {key}"
        # rsync's -e argument is re-parsed by rsync itself, so a path with
        # whitespace or quotes would be split into separate words.
        if re.search(r"[\s\"']", key):
            return "the SSH key path must not contain spaces or quotes"
    return ""


def rsync_argv(ssh: list[str], excludes: list[str], target: str,
               dest: str) -> list[str]:
    """
    The copy command.

    Note the absence of --delete-excluded, for two independent reasons:

    1. Safety. It deletes remote files matching the exclude list, so a
       code-only deploy would delete the remote's keys/, oauth/ and env.sh --
       precisely the files a code-only deploy exists to leave alone.
    2. Portability. macOS ships openrsync (protocol 29, "2.6.9 compatible"),
       not GNU rsync, and the filter rules it sends for that flag make a modern
       remote rsync abort with "buffer overflow: recv_rules (exclude.c)".

    Both were found deploying to a real host.
    """
    argv = ["rsync", "-az", "-e", " ".join(ssh)]
    for pattern in excludes:
        argv += ["--exclude", pattern]
    return argv + [HERE + os.sep, f"{target}:{dest}/"]


def start_command(dest: str, ui_port: int) -> str:
    """
    Restart the remote UI, stopping the previous instance by recorded PID.

    Never by pattern match. ssh passes the whole command as a single argument
    to the remote shell, so that shell's own command line contains every word
    of this string -- including "webui.py --port N" from the nohup. A
    `pkill -f webui` therefore matches the shell running the command and kills
    it mid-line, before anything starts. Observed exactly that: the command
    died silently after `cd`, and took an unrelated instance on another port
    with it. The `[w]ebui` bracket trick does not help, because the nohup text
    still matches.

    A PID file can only ever stop the instance this script started in this
    directory. The `ps` check guards against the PID having been recycled.
    """
    return (
        f"cd {dest}; "
        f"if [ -f webui.pid ]; then P=$(cat webui.pid); "
        f'  ps -p "$P" -o args= 2>/dev/null | grep -q \'webui\' && kill "$P"; '
        f"fi; sleep 1; "
        f"nohup .venv/bin/python webui.py --port {ui_port} "
        f"> {dest}/webui.log 2>&1 & "
        f"echo $! > {dest}/webui.pid; sleep 4; "
        f"curl -s -o /dev/null -w '%{{http_code}}' http://127.0.0.1:{ui_port}/"
    )


def deploy(host: str, user: str, port: int, key: str, dest: str,
           include_credentials: bool, start_ui: bool, ui_port: int) -> int:
    err = validate(host, user, port, key)
    if err:
        say(f"error: {err}")
        return 2

    target = f"{user}@{host}"
    ssh = ssh_base(key, port)

    say(f"== checking SSH to {target}")
    rc, out = run(ssh + [target, "echo ok"], timeout=30)
    if rc != 0:
        say(out or "ssh failed")
        say("\nThe usual causes: the key is not authorised for this user, or the\n"
            "host/port is wrong. This never prompts for a password on purpose --\n"
            "an interactive prompt would hang with nothing to type into.")
        return 1
    say("   ok")

    excludes = list(EXCLUDES) + ([] if include_credentials else SECRET_EXCLUDES)
    if include_credentials:
        say("== copying code AND credentials (keys/, oauth/, env.sh)")
    else:
        say("== copying code only — credentials excluded")
        say("   the remote will need its own keys/env.sh, or re-run with "
            "--include-credentials")

    rsync = rsync_argv(ssh, excludes, target, dest)

    rc, out = run(rsync)
    if rc != 0:
        say(out or "rsync failed")
        return 1
    say(f"   copied to {target}:{dest}")

    if include_credentials:
        # env.sh carries absolute paths from the machine that wrote it, and
        # rsync ships them verbatim. Observed: a Mac's
        # /Users/aryan/Repos/calude-workspace/migration.db was recreated
        # literally on a Linux VPS -- the migration worked, against a ledger
        # nobody would ever think to look for. Strip the path variables so the
        # remote falls back to its own repo-relative defaults; the tenant and
        # credential settings, which are machine-independent, stay.
        say("== rewriting env.sh for this host")
        strip = "MIGRATION_DB|SCRATCH_DIR|LOG_FILE|SOURCE_SA_KEY|TARGET_SA_KEY|" \
                "OAUTH_TOKEN_DIR|OAUTH_CLIENT_SECRETS"
        rc, out = run(ssh + [target,
                             f"cd {dest} && [ -f env.sh ] && "
                             f"grep -vE '^export ({strip})=' env.sh > env.sh.tmp "
                             f"&& mv env.sh.tmp env.sh && "
                             f"echo '  dropped machine-specific paths'"],
                      timeout=60)
        say(out.strip() or "   (no env.sh to rewrite)")

    say("== preparing the remote environment")
    # One command, but a fixed string we wrote -- no client input reaches it.
    setup_cmd = (
        f"set -e; cd {dest}; "
        f"python3 -m venv .venv 2>/dev/null || true; "
        f".venv/bin/pip install -q --upgrade pip >/dev/null 2>&1 || true; "
        f".venv/bin/pip install -q -r requirements.txt 2>&1 | tail -3; "
        f".venv/bin/python -c 'import googleapiclient, google.auth; print(\"deps ok\")'"
    )
    rc, out = run(ssh + [target, setup_cmd], timeout=900)
    say("   " + (out.strip().splitlines()[-1] if out.strip() else f"rc={rc}"))
    if rc != 0:
        say("   dependency install failed — is python3-venv installed on the host?")
        return 1

    if start_ui:
        say("== starting the web UI on the remote host")
        start_cmd = start_command(dest, ui_port)
        rc, out = run(ssh + [target, start_cmd], timeout=120)
        code = (out or "").strip().splitlines()[-1] if out.strip() else ""
        if code == "200":
            say("   UI is serving on the remote loopback")
        else:
            say(f"   UI did not answer (got {code!r}); check {dest}/webui.log")

    keyarg = f"-i {key} " if key else ""
    say("\n" + "=" * 62)
    say(" Deployed. The remote UI is bound to 127.0.0.1 there, so reach it")
    say(" through a tunnel rather than opening a port:")
    say("")
    say(f"   ssh {keyarg}-p {port} -L {ui_port}:localhost:{ui_port} {target}")
    say("")
    say(f" then open  http://localhost:{ui_port}")
    say("=" * 62)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Deploy this tool to a VPS.")
    ap.add_argument("--host", required=True)
    ap.add_argument("--user", default="root")
    ap.add_argument("--port", type=int, default=22)
    ap.add_argument("--key", default="", help="SSH private key (default: agent/config)")
    ap.add_argument("--dest", default="/root/workspace-migrator")
    ap.add_argument("--ui-port", type=int, default=8080)
    ap.add_argument("--include-credentials", action="store_true",
                    help="also copy keys/, oauth/ and env.sh to the host")
    ap.add_argument("--no-start", action="store_true",
                    help="copy and install, but do not start the remote UI")
    args = ap.parse_args(argv)

    return deploy(args.host, args.user, args.port, args.key, args.dest.rstrip("/"),
                  args.include_credentials, not args.no_start, args.ui_port)


if __name__ == "__main__":
    raise SystemExit(main())
