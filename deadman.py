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
import json
import os
import shutil
import subprocess
import sys
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


def signals() -> dict[str, float]:
    """Every independent reason to believe somebody is still here."""
    return {
        "interactive login": _last_interactive_login(),
        "sshd auth": _last_sshd_auth(),
        "web UI login": _last_webui_login(),
        "deploy": _mtime(os.path.join(HERE, "DEPLOYED_COMMIT")),
        "manual touch": _mtime(TOUCH),
        # A running migration is NOT in here on purpose: a job left running
        # says nothing about whether its owner still exists, and counting it
        # would let a stuck process keep the switch alive indefinitely.
    }


def last_seen() -> tuple[float, str]:
    sig = signals()
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
    """What gets destroyed. Credentials and tenant data -- not the OS.

    Wiping the operating system leaves an unusable box and protects nothing
    extra: the secret material is these paths, and a machine that still
    boots can be inspected afterwards to confirm the switch did what it
    said.
    """
    out = [os.path.join(HERE, "keys"), "/etc/bitport",
           os.path.join(HERE, "data"), os.path.join(HERE, "migration.db"),
           os.path.join(HERE, "env.sh"),
           os.path.join(HERE, "identities.csv")]
    if cfg.get("include_backups", True):
        out.append(os.path.join(HERE, "backups"))
    return [p for p in out if os.path.exists(p)]


def wipe(cfg: dict, dry: bool = True) -> list[str]:
    done = []
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
    if not dry:
        # Stop the services last: doing it first would leave the box quiet
        # while the files were still there.
        subprocess.run(["systemctl", "stop", "bitport-api", "bitport-webui"],
                       capture_output=True, timeout=60)
        done.append("stopped bitport-api, bitport-webui")
    return done


# ---------------------------------------------------------------------------
def report(cfg: dict) -> str:
    sig = signals()
    seen, why = last_seen()
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

    if args.arm:
        if args.days < 7:
            sys.exit("refusing a deadline under 7 days: this host has a "
                     "measured 13-day gap in interactive logins during "
                     "normal operation")
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
    seen, why = last_seen()
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
                + "\n\nTo stop it, do ANY of:\n"
                  "  - sign in to the web UI\n"
                  "  - ssh to the box\n"
                  f"  - run: {HERE}/deadman.py --disarm\n")
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
