#!/usr/bin/env python3
"""Destroy the credentials if nobody has been here for too long.

The case for it on this deployment is real: four SSH keys hold root on a
shared box that stores service-account keys for live tenants, the DWD admin
password, and every customer's password hash. If the owner loses access,
that material should not simply sit there.

The case for extreme care is equally real, and the login history settles
the design. `last` on this host shows a THIRTEEN DAY gap (2026-08-28 to
2026-09-10) during which migrations ran and the code was deployed daily --
because `last` records interactive sessions only, and an ssh command with
no pty leaves no wtmp entry. Over the same window sshd logged 6,553
accepted authentications. A switch keyed on `last` alone would have wiped a
working system while its owner used it every day.

So aliveness is the NEWEST of every signal that means a human is still
here, and the destructive step is opt-in, announced in advance, and
disarmable from anywhere that can reach the box.

    ./deadman.py --status          # what it thinks, changes nothing
    ./deadman.py --check           # what a real run would do, changes nothing
    ./deadman.py --arm --days 30   # write the config. Still needs --yes to fire.
    ./deadman.py --disarm          # stop, immediately and permanently
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = "/etc/bitport/deadman.json"
DISARM = "/etc/bitport/deadman.disarmed"
TOUCH = "/etc/bitport/deadman.alive"
LOG = os.path.join(HERE, "logs", "deadman.log")

# Warn at these fractions of the deadline. Silence until the moment of
# destruction is how an automation like this takes out someone who was one
# day from coming back.
WARN_AT = (0.5, 0.75, 0.9)


EMAIL_ENV = "/etc/bitport/deadman.env"


def _email_config() -> dict:
    """Read SMTP or API credentials from a root-only file.

    Not from argv and not from this repo: it is a password, and argv is
    readable by every process on the box. Absent config is not an error --
    the switch still warns to its log and the countdown page still renders;
    it just cannot reach you off the machine.
    """
    cfg = {}
    try:
        with open(EMAIL_ENV, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    cfg[k.strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    return cfg


def notify(subject: str, body: str) -> bool:
    """Best effort, and never fatal.

    A switch that crashes because it could not send mail is a switch that
    stops warning and then fires silently -- the worst of both behaviours.
    """
    cfg = _email_config()
    to = cfg.get("DEADMAN_EMAIL_TO", "")
    if not to:
        _log(f"[no email configured] {subject}")
        return False
    try:
        import smtplib
        from email.message import EmailMessage

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = cfg.get("SMTP_FROM") or cfg.get("SMTP_USER") or to
        msg["To"] = to
        msg.set_content(body)
        host = cfg.get("SMTP_HOST", "smtp.gmail.com")
        port = int(cfg.get("SMTP_PORT", "587"))
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls()
            if cfg.get("SMTP_USER"):
                smtp.login(cfg["SMTP_USER"], cfg.get("SMTP_PASS", ""))
            smtp.send_message(msg)
        _log(f"emailed {to}: {subject}")
        return True
    except Exception as exc:      # noqa: BLE001
        _log(f"could not send mail ({str(exc)[:120]}): {subject}")
        return False


def _log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}  {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Aliveness
# ---------------------------------------------------------------------------
def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _last_interactive_login() -> float:
    """wtmp. Interactive sessions only -- the signal that MISSES deploys."""
    try:
        out = subprocess.run(["last", "-F", "-n", "1"], capture_output=True,
                             text=True, timeout=20).stdout.splitlines()
    except Exception:      # noqa: BLE001
        return 0.0
    # Located by finding the weekday, not by column index. `last` pads its
    # columns by content width -- a long hostname or a wtmp entry with no
    # host shifts everything -- so counting from the left silently returned
    # "never", which on a dead man switch reads as "nobody has ever been
    # here". It was off by one against the real output.
    days = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    for line in out:
        parts = line.split()
        for i, tok in enumerate(parts):
            if tok not in days or len(parts) < i + 5:
                continue
            try:
                return time.mktime(time.strptime(" ".join(parts[i:i + 5]),
                                                 "%a %b %d %H:%M:%S %Y"))
            except (ValueError, OverflowError):
                break
    return 0.0


def _last_sshd_auth() -> float:
    """Every accepted authentication, pty or not. This is what a deploy
    looks like, and what `last` cannot see."""
    try:
        out = subprocess.run(
            ["journalctl", "-u", "ssh", "-u", "sshd", "--since", "60 days ago",
             "-o", "short-unix", "--no-pager"],
            capture_output=True, text=True, timeout=60).stdout
    except Exception:      # noqa: BLE001
        return 0.0
    newest = 0.0
    for line in out.splitlines():
        if "Accepted" not in line:
            continue
        try:
            newest = max(newest, float(line.split()[0]))
        except (ValueError, IndexError):
            continue
    return newest


def _last_webui_login() -> float:
    """A session row in the control plane.

    The strongest signal of all on this deployment: the tool is operated by
    clicking its web UI, so somebody signing in there is unambiguously a
    live human -- and it needs no shell access at all.
    """
    try:
        sys.path.insert(0, HERE)
        import control_plane_db as cpdb
        with cpdb.ro() as conn:
            row = conn.execute(
                "SELECT MAX(expires_at) AS newest FROM sessions").fetchone()
        if not row or not row["newest"]:
            return 0.0
        # Stored as an expiry; the login was SESSION_LIFETIME_S before it.
        import accounts_auth
        expiry = time.mktime(time.strptime(row["newest"][:19],
                                           "%Y-%m-%dT%H:%M:%S"))
        return expiry - accounts_auth.SESSION_LIFETIME_S
    except Exception:      # noqa: BLE001 - one signal failing is not death
        return 0.0


CHECKIN = "/etc/bitport/deadman.checkin"


def _last_checkin() -> float:
    """The deliberate one: somebody typed a current 2-Step code.

    Every other signal here is INCIDENTAL -- a deploy ran, a cron
    authenticated, a browser session existed. Those prove the machine is
    being used, which is not the same claim as the owner being alive and in
    possession of their second factor, and it is the second claim a dead man
    switch is actually asking about.

    It is also what makes a short deadline defensible. Twelve hours of
    incidental quiet happens on an ordinary weekend -- measured on this
    host, three gaps past 12h in 21 days, the longest 27.9. Twelve hours
    without a deliberate check-in means the person did not check in.
    """
    return _mtime(CHECKIN)


def record_checkin(code: str, email: str = "") -> tuple[bool, str]:
    """Verify a 2-Step code and, if it is right, reset the clock.

    The previous code is accepted as well as the current one. Clocks drift,
    and a code typed at second 29 of its window arrives in the next -- so
    rejecting the previous window would reject correct codes, on the one
    control standing between an operator and the destruction of everything.
    """
    import totp

    secrets = totp.load_secrets()
    if not secrets:
        return False, ("no authenticator seed stored -- add one on the "
                       "Authenticator page before arming a check-in deadline")
    typed = re.sub(r"\D", "", code or "")
    if len(typed) != totp.DIGITS:
        return False, f"a code is {totp.DIGITS} digits"

    now = time.time()
    wanted = [email.strip().lower()] if email.strip() else list(secrets)
    for who in wanted:
        secret = secrets.get(who)
        if not secret:
            continue
        for offset in (0, -totp.PERIOD):
            if totp.code_at(secret, when=now + offset) == typed:
                os.makedirs(os.path.dirname(CHECKIN), exist_ok=True)
                with open(CHECKIN, "w", encoding="utf-8") as fh:
                    fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} "
                             f"{who}\n")
                os.chmod(CHECKIN, 0o600)
                _log(f"check-in accepted for {who}")
                return True, who
    return False, "that code is not current for any stored account"


def signals() -> dict[str, float]:
    """Every independent reason to believe somebody is still here."""
    return {
        "2-Step check-in": _last_checkin(),
        "interactive login": _last_interactive_login(),
        "sshd auth": _last_sshd_auth(),
        "web UI login": _last_webui_login(),
        "deploy": _mtime(os.path.join(HERE, "DEPLOYED_COMMIT")),
        "manual touch": _mtime(TOUCH),
        # A running migration is NOT in here on purpose: a job left running
        # says nothing about whether its owner still exists, and counting it
        # would let a stuck process keep the switch alive indefinitely.
    }


def last_seen(cfg: dict | None = None) -> tuple[float, str]:
    """When somebody was last here, and how we know.

    With require_checkin set, ONLY the deliberate 2-Step check-in counts.
    That is the whole point of that mode: a deploy proves the machine is in
    use, not that its owner is alive and holding their second factor, and
    letting an incidental signal hold the switch open would quietly turn a
    12-hour proof-of-life into "has anything happened lately".
    """
    sig = signals()
    if (cfg or {}).get("require_checkin"):
        return sig["2-Step check-in"], "2-Step check-in"
    name = max(sig, key=lambda k: sig[k])
    return sig[name], name


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load() -> dict:
    try:
        with open(CONFIG, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def armed() -> tuple[bool, str]:
    if os.path.exists(DISARM):
        return False, f"disarmed ({DISARM} exists)"
    cfg = load()
    if not cfg.get("armed"):
        return False, "not armed"
    if not cfg.get("confirmed"):
        # Arming writes the schedule; confirming is a separate, explicit act.
        return False, "armed but never confirmed -- will not destroy anything"
    return True, "armed"


# ---------------------------------------------------------------------------
# The destructive part
# ---------------------------------------------------------------------------
def targets(cfg: dict) -> list[str]:
    """What gets destroyed, secrets first and the code last.

    Order matters and is not cosmetic. The code directory contains this
    script, its venv and everything else, so removing it ends the process
    doing the removing. Credentials go first so that a failure partway
    through has already taken the material that matters; the code is the
    last thing standing.

    Not the operating system. A box that still boots can be inspected
    afterwards to confirm the switch did what it said, and wiping the OS
    protects nothing extra -- the secret material is these paths.

    On "unrecoverable", plainly: rm is what this can do. Overwriting does
    not reliably destroy data on an SSD, because wear levelling means the
    blocks just written are usually not the blocks that held the old copy,
    and a copy-on-write filesystem or a hypervisor snapshot defeats it
    outright. This makes the data gone from the running system and from
    anyone with ordinary access. It is not a forensic wipe and this file
    will not pretend otherwise. If the provider's disk images matter,
    destroy the VPS from the provider's console -- which is the only thing
    that actually reaches them.
    """
    out = [os.path.join(HERE, "keys"), "/etc/bitport",
           os.path.join(HERE, "data"), os.path.join(HERE, "migration.db"),
           os.path.join(HERE, "env.sh"),
           os.path.join(HERE, "identities.csv")]

    # The browser profile. This is not housekeeping: full_setup drives a
    # real Chrome through Google's sign-in as a super-admin, and the profile
    # keeps the resulting SESSION COOKIES. Leaving it behind after a wipe
    # means the credentials are gone and a logged-in browser is not, which
    # is most of what the credentials were for.
    out += [os.path.expanduser("~/.config/google-chrome"),
            os.path.expanduser("~/.config/chromium")]
    out += sorted(glob.glob("/tmp/playwright*"))

    if cfg.get("include_backups", True):
        out.append(os.path.join(HERE, "backups"))
    if cfg.get("include_code", True):
        # Everything inside HERE is subsumed by this; the entries above are
        # listed separately so the credentials go FIRST and a failure part
        # way through has already taken the material that matters.
        out.append(HERE)
        # The playwright browsers themselves -- hundreds of megabytes, and
        # no use to anything once the code is gone.
        out.append(os.path.expanduser("~/.cache/ms-playwright"))
    return [p for p in out if os.path.exists(p)]


def system_traces() -> list[str]:
    """What lives outside the install directory and still names this tenant.

    Removing the code and leaving these behind produces a machine that
    advertises what it used to be: unit files naming paths and ports, a
    Caddyfile carrying the public domain, and a cron entry that goes on
    firing every ten minutes against an interpreter that no longer exists.
    """
    return sorted(glob.glob("/etc/systemd/system/bitport-*")
                  + glob.glob("/etc/systemd/system/xvfb.service")
                  + glob.glob("/etc/systemd/system/x11vnc.service"))


GIT_ENV = "/etc/bitport/deadman.git"

# Anything matching these never leaves this machine, whatever git thinks.
#
# The push happens moments before everything is destroyed, to a repository
# that is PUBLIC -- install_node.sh clones it with no credential at all. A
# mistake here does not lose data, it publishes service-account keys for
# live tenants and every customer's password hash, permanently, to an
# archive that is mirrored the moment it lands.
#
# So the filter is a denylist on top of an allowlist, not either alone.
_NEVER_PUSH = re.compile(
    r"(^|/)(keys|data|backups|node_modules|\.venv|logs)(/|$)"
    r"|\.(db|db-wal|db-shm|pem|p12|key)$"
    r"|(^|/)(env\.sh|node\.env|identities\.csv|.*sa\.json|"
    r"deadman\.(env|git|json)|totp\.env)$"
)


def _safe_to_push(rel: str) -> bool:
    return not _NEVER_PUSH.search(rel)


def push_code(cfg: dict, dry: bool = True) -> list[str]:
    """Save the code to GitHub before destroying the machine it is on.

    What this is for: the wipe should cost the credentials and the tenant
    data, not the work. Everything else here is recoverable by redeploying
    -- but only if the code survives, and the deployed tree is an rsync of
    somebody's laptop rather than a git checkout, so a local change that
    never went back has no other copy.

    The allowlist is taken from the REMOTE's own file list, not from this
    machine. Only paths git already tracks upstream can be pushed, so a
    stray keys/ directory or a new .env cannot be swept in by a `git add .`
    -- there is no `git add .` here, and a file the remote has never heard
    of is never sent. _NEVER_PUSH then rejects the obvious shapes on top of
    that, because two independent filters fail independently.
    """
    out: list[str] = []
    env = _email_config()
    try:
        with open(GIT_ENV, encoding="utf-8") as fh:
            for line in fh:
                if "=" in line and not line.strip().startswith("#"):
                    k, _, v = line.strip().partition("=")
                    env[k.strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    remote, token = env.get("DEADMAN_GIT_REMOTE", ""), env.get("DEADMAN_GIT_TOKEN", "")
    if not remote or not token:
        out.append("no git remote or token configured -- code NOT pushed "
                   f"(set DEADMAN_GIT_REMOTE and DEADMAN_GIT_TOKEN in {GIT_ENV})")
        return out

    branch = env.get("DEADMAN_GIT_BRANCH", "deadman-snapshot")
    # The token goes through the ENVIRONMENT, never argv. A URL of the form
    # https://<token>@github.com/... is the obvious way to do this and it
    # puts the token in the process table, where on this shared VPS every
    # other account can read it with `ps`. git's credential helper can be an
    # inline shell function, so the secret is passed the same way the rest
    # of this codebase passes passwords.
    genv = dict(os.environ, DEADMAN_GIT_TOKEN=token)
    helper = ["-c", "credential.helper=!f() { echo username=x; "
                    "echo password=$DEADMAN_GIT_TOKEN; }; f"]
    git = ["git"] + helper
    # 0700 and unpredictable: /tmp is world-traversable, and a fixed
    # pid-based name is guessable by anyone who can watch for the wipe.
    work = tempfile.mkdtemp(prefix="deadman-push-")
    try:
        if dry:
            out.append(f"WOULD push tracked files to {remote} branch {branch}")
            return out
        os.rmdir(work)          # git clone wants to create it
        subprocess.run(git + ["clone", "--depth", "1", remote, work],
                       capture_output=True, timeout=300, check=True, env=genv)
        tracked = subprocess.run(["git", "-C", work, "ls-files"],
                                 capture_output=True, text=True,
                                 timeout=120).stdout.splitlines()
        if not tracked:
            # The allowlist IS the remote's file list, so an empty one means
            # nothing can ever be staged. Reported loudly because the next
            # branch would otherwise call that "nothing to push -- the
            # deployed code matches the remote", which is a false all-clear
            # delivered moments before the code is destroyed. Seen for real:
            # a --depth 1 clone of a repo whose HEAD names a branch that
            # does not exist comes back empty and silent.
            out.append("the remote lists NO tracked files -- nothing could be "
                       "pushed (wrong branch, or an empty repository?)")
            return out
        copied = 0
        for rel in tracked:
            if not _safe_to_push(rel):
                continue
            src = os.path.join(HERE, rel)
            if not os.path.isfile(src):
                continue
            dst = os.path.join(work, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1
        # Stage by explicit path, never `git add .`: an untracked secret in
        # the working tree is exactly what that would sweep up.
        subprocess.run(["git", "-C", work, "add", "--"] +
                       [r for r in tracked if _safe_to_push(r)],
                       capture_output=True, timeout=120)
        staged = subprocess.run(["git", "-C", work, "diff", "--cached",
                                 "--name-only"], capture_output=True,
                                text=True, timeout=60).stdout.split()
        leaked = [f for f in staged if not _safe_to_push(f)]
        if leaked:
            # Belt and braces, checked after staging rather than trusted
            # before it. Refusing to push is always better than publishing
            # a key.
            out.append(f"REFUSED to push: {len(leaked)} unsafe path(s) staged")
            return out
        if not staged:
            out.append("nothing to push -- the deployed code matches the remote")
            return out
        subprocess.run(["git", "-C", work, "-c", "user.email=deadman@bitport",
                        "-c", "user.name=Bitport dead man switch",
                        "commit", "-m",
                        f"Snapshot before dead man switch wipe "
                        f"({time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})"],
                       capture_output=True, timeout=120)
        r = subprocess.run(git + ["-C", work, "push", remote,
                                  f"HEAD:refs/heads/{branch}"],
                           capture_output=True, text=True, timeout=300, env=genv)
        if r.returncode == 0:
            out.append(f"pushed {len(staged)} changed file(s) to {branch}")
        else:
            out.append(f"push FAILED: {r.stderr[-200:]}")
    except Exception as exc:      # noqa: BLE001
        # Never block the wipe. The destruction is the point; saving the
        # code is a courtesy, and a courtesy that could prevent the
        # destruction would defeat the whole mechanism.
        out.append(f"push failed ({str(exc)[:160]}) -- wiping anyway")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return out


def wipe(cfg: dict, dry: bool = True) -> list[str]:
    done = []
    # BEFORE anything is removed, and before the services are stopped: the
    # push reads the working tree, so it has to happen while there still is
    # one. It cannot block the wipe -- push_code swallows its own failures.
    if cfg.get("push_code", True):
        done += push_code(cfg, dry=dry)
    if not dry:
        # First, so nothing is writing to what is about to be removed and
        # no restart brings a service back up mid-wipe.
        subprocess.run(["systemctl", "stop", "bitport-api", "bitport-webui",
                        "bitport-fleet"], capture_output=True, timeout=60)
        done.append("stopped the services")
    for path in targets(cfg):
        if dry:
            done.append(f"WOULD REMOVE {path}")
            continue
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            done.append(f"removed {path}")
        except OSError as exc:
            done.append(f"FAILED {path}: {exc}")
    # What is left outside the install directory. Removing the code and
    # leaving these produces a machine that advertises what it used to be.
    for unit in system_traces():
        if dry:
            done.append(f"WOULD REMOVE {unit}")
            continue
        try:
            os.remove(unit)
            done.append(f"removed {unit}")
        except OSError as exc:
            done.append(f"FAILED {unit}: {exc}")

    if dry:
        done.append("WOULD remove the cron entry, restore the Caddyfile "
                    "and vacuum the journal")
        return done

    subprocess.run(["systemctl", "disable", "bitport-api", "bitport-webui",
                    "bitport-fleet", "bitport-backup.timer"],
                   capture_output=True, timeout=60)
    subprocess.run(["systemctl", "daemon-reload"], capture_output=True, timeout=60)
    done.append("disabled the services and reloaded systemd")

    # The cron entry outlives everything it refers to, and goes on firing
    # every ten minutes against an interpreter that is no longer there.
    try:
        cur = subprocess.run(["crontab", "-l"], capture_output=True,
                             text=True, timeout=30).stdout
        kept = [ln for ln in cur.splitlines() if HERE not in ln]
        if len(kept) != len(cur.splitlines()):
            subprocess.run(["crontab", "-"], input="\n".join(kept) + "\n",
                           text=True, capture_output=True, timeout=30)
            done.append("removed the cron entries pointing at the install")
    except Exception as exc:      # noqa: BLE001
        done.append(f"could not edit crontab: {exc}")

    # Caddy carries the public domain. install.sh saved whatever was there
    # before, so put it back rather than leaving a config for a site that
    # no longer exists.
    try:
        backup = "/etc/caddy/Caddyfile.bitport-backup"
        if os.path.exists(backup):
            shutil.move(backup, "/etc/caddy/Caddyfile")
            done.append("restored the Caddyfile that predated Bitport")
        elif os.path.exists("/etc/caddy/Caddyfile"):
            os.remove("/etc/caddy/Caddyfile")
            done.append("removed Bitport's Caddyfile")
        subprocess.run(["systemctl", "reload", "caddy"],
                       capture_output=True, timeout=30)
    except OSError as exc:
        done.append(f"could not clear the Caddyfile: {exc}")

    # The journal and syslog quote tenant domains, user addresses and file
    # names on every run. Vacuuming is the only lever available; it is not
    # a secure erase, and neither is anything else here.
    subprocess.run(["journalctl", "--vacuum-time=1s"],
                   capture_output=True, timeout=120)
    done.append("vacuumed the systemd journal")
    return done


# ---------------------------------------------------------------------------
def _how_to_stop(cfg: dict) -> str:
    """What actually resets the clock -- which differs by mode.

    Under require_checkin it is ONLY the 2-Step code. Telling somebody to
    "sign in to the web UI" there is worse than telling them nothing: they
    do it, see they are signed in, believe they are safe, and the machine is
    destroyed anyway on schedule.
    """
    if cfg.get("require_checkin"):
        return ("  - open the Dead man switch page and type a current 2-Step "
                "code\n"
                "    (signing in is NOT enough in this mode, and neither is "
                "an ssh login)\n"
                f"  - or run: {HERE}/deadman.py --checkin <code>\n"
                f"  - or, to stop it entirely: {HERE}/deadman.py --disarm\n")
    return ("  - sign in to the web UI\n"
            "  - ssh to the box\n"
            f"  - run: {HERE}/deadman.py --disarm\n")


def report(cfg: dict) -> str:
    sig = signals()
    seen, why = last_seen(cfg)
    now = time.time()
    lines = ["  Aliveness signals (newest wins):"]
    for name, ts in sorted(sig.items(), key=lambda kv: -kv[1]):
        age = (now - ts) / 86400 if ts else None
        lines.append(f"    {name:<18} "
                     + (f"{age:6.1f} days ago" if ts else "     never"))
    ok, state = armed()
    days = cfg.get("days", 0)
    lines.append(f"  State: {state}")
    if not seen:
        lines.append("  NO SIGNAL AT ALL -- refusing to act on that; a switch "
                     "that fires when it cannot measure is a switch that "
                     "fires at random.")
        return "\n".join(lines)
    age_days = (now - seen) / 86400
    lines.append(f"  Last seen {age_days:.1f} days ago, via {why}")
    if days:
        lines.append(f"  Deadline {days} days -> "
                     f"{max(0.0, days - age_days):.1f} days remaining")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--check", action="store_true", help="what a run would do")
    ap.add_argument("--run", action="store_true", help="the scheduled check")
    ap.add_argument("--arm", action="store_true")
    ap.add_argument("--disarm", action="store_true")
    ap.add_argument("--days", type=float, default=0)
    ap.add_argument("--yes", action="store_true",
                    help="with --arm: confirm that it may actually destroy")
    ap.add_argument("--touch", action="store_true", help="I am alive")
    ap.add_argument("--checkin", metavar="CODE",
                    help="prove it with a current 2-Step code")
    ap.add_argument("--email", default="",
                    help="with --checkin: which account's code this is")
    ap.add_argument("--require-checkin", action="store_true",
                    help="with --arm: ONLY a 2-Step check-in counts as life")
    args = ap.parse_args(argv)
    cfg = load()

    if args.touch:
        os.makedirs(os.path.dirname(TOUCH), exist_ok=True)
        with open(TOUCH, "w", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        _log("alive signal recorded by hand")
        return 0

    if args.disarm:
        os.makedirs(os.path.dirname(DISARM), exist_ok=True)
        with open(DISARM, "w", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        _log("DISARMED -- nothing will be destroyed until this file is removed")
        return 0

    if args.checkin is not None:
        ok, who = record_checkin(args.checkin, args.email)
        print(f"  {'check-in accepted for ' + who if ok else who}")
        return 0 if ok else 1

    if args.arm:
        # The floor applies to the INCIDENTAL signals only. Twelve hours of
        # no deploy and no ssh is an ordinary weekend -- measured here,
        # three gaps past 12h in 21 days, the longest 27.9. Twelve hours
        # without somebody typing a current 2-Step code is not ambiguous:
        # it means they did not type one. A deliberate signal is what makes
        # a short deadline a proof of life rather than a coin toss.
        if args.days < 7 and not args.require_checkin:
            sys.exit("refusing a deadline under 7 days without --require-checkin: "
                     "this host has measured 12-hour gaps in ordinary use, so a "
                     "short deadline on incidental signals fires on a quiet "
                     "weekend. With --require-checkin the deadline measures a "
                     "deliberate act and a short one is meaningful.")
        if args.require_checkin:
            import totp
            if not totp.load_secrets():
                sys.exit("--require-checkin needs an authenticator seed stored "
                         "first, or the deadline can never be met. Add one on "
                         "the Authenticator page.")
            cfg["require_checkin"] = True
        cfg.update({"armed": True, "days": args.days,
                    "confirmed": bool(args.yes),
                    "armed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                              time.gmtime())})
        os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
        with open(CONFIG, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
        os.chmod(CONFIG, 0o600)
        _log(f"armed: {args.days} days, "
             f"{'CONFIRMED - will destroy' if args.yes else 'unconfirmed - will only warn'}")
        print(report(cfg))
        return 0

    if args.status or args.check or not args.run:
        print(report(cfg))
        if args.check:
            print("\n  A real run would:")
            for line in wipe(cfg, dry=True) or ["    nothing to remove"]:
                print(f"    {line}")
        return 0

    # --run: the scheduled check.
    ok, state = armed()
    seen, why = last_seen(cfg)
    if not seen:
        _log("no aliveness signal could be read at all -- doing nothing. "
             "A switch that fires when it cannot measure fires at random.")
        return 0
    age_days = (time.time() - seen) / 86400
    days = float(cfg.get("days") or 0)
    if not ok or not days:
        _log(f"{state}; last seen {age_days:.1f}d ago via {why}")
        return 0

    # One email per threshold crossed, remembered on disk. A cron running
    # every few minutes would otherwise send hundreds of identical warnings
    # and train the recipient to ignore exactly the message that matters.
    sent = set(cfg.get("warned", []))
    for frac in WARN_AT:
        if age_days < days * frac:
            continue
        left = max(0.0, days - age_days)
        _log(f"WARNING: {age_days:.1f} of {days} days with no sign of life "
             f"(newest signal: {why}).")
        if str(frac) not in sent:
            notify(
                f"[Bitport] dead man switch: {left * 24:.0f} hours left",
                f"No sign of life on {os.uname().nodename} for "
                f"{age_days * 24:.0f} hours.\n"
                f"Newest signal was: {why}.\n\n"
                f"At {days} days everything below is destroyed permanently:\n"
                + "\n".join(f"  {p}" for p in targets(cfg))
                + "\n\nTo stop it:\n" + _how_to_stop(cfg))
            sent.add(str(frac))
            cfg["warned"] = sorted(sent)
            try:
                with open(CONFIG, "w", encoding="utf-8") as fh:
                    json.dump(cfg, fh, indent=2)
            except OSError:
                pass
    if age_days < days:
        if age_days < days * WARN_AT[0] and cfg.get("warned"):
            # Back below the first threshold: forget the warnings so the
            # next quiet spell warns again from the start rather than
            # silently skipping straight to destruction.
            cfg["warned"] = []
            try:
                with open(CONFIG, "w", encoding="utf-8") as fh:
                    json.dump(cfg, fh, indent=2)
            except OSError:
                pass
        return 0

    _log(f"DEADLINE PASSED: {age_days:.1f} days since {why}. Destroying "
         f"credentials and tenant data.")
    notify("[Bitport] dead man switch FIRED -- data destroyed",
           f"No sign of life for {age_days:.1f} days on "
           f"{os.uname().nodename}. Credentials and tenant data have been "
           f"permanently destroyed.")
    for line in wipe(cfg, dry=False):
        _log(f"  {line}")
    _log("dead man switch complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
