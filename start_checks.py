"""Before Start: what a run needs that is not the tenants' data, asked at once and refused with the reason.

A run started on a box with a full disk, no route to Google, an upgrade in flight, a DMS login that
belongs to another pair or a tenant Google no longer serves fails minutes or hours in, and a restart
after the fix has already lost its ordered passes. `refusal(check(...))` answers before anything starts.
(The tenants' storage is `tenant_capacity`, asked by the same endpoint.)

Each check returns {"name", "ok", "detail"}: True passes, False is a definite refusal, None could not be
checked -- logged, never a refusal, never a pass in the detail. No secret is read into a row: the DMS
login is judged by which names the file holds and whose domain they carry, never by a value.
"""
from __future__ import annotations

import logging
import os
import socket

log = logging.getLogger(__name__)

DWD_ENV = os.getenv("BITPORT_DWD_ENV", "/etc/bitport/dwd.env")
DISK_REFUSE_PCT = int(os.getenv("PREFLIGHT_DISK_PCT", "90"))     # housekeeping opens its incident at 90
GOOGLE_HOST = ("oauth2.googleapis.com", 443)


def _row(name: str, ok, detail: str) -> dict:
    return {"name": name, "ok": ok, "detail": detail}


def check_network(connect=socket.create_connection) -> dict:
    try:
        connect(GOOGLE_HOST, timeout=8).close()
        return _row("network", True, f"{GOOGLE_HOST[0]} answers")
    except OSError as exc:
        return _row("network", False, f"cannot reach {GOOGLE_HOST[0]}: {exc}")


def check_disk(path: str, disk=None) -> dict:
    try:
        if disk is None:
            import housekeeping
            disk = housekeeping.disk
        d = disk(path if path and os.path.isdir(path) else "/")
    except Exception as exc:      # noqa: BLE001 - unknown, not a refusal
        return _row("disk", None, f"could not read the disk: {exc}")
    free_gb = d["free"] / 1e9
    if d["pct"] >= DISK_REFUSE_PCT:
        return _row("disk", False, f"the disk is {d['pct']}% full ({free_gb:.0f} GB free); "
                                   f"download-and-upload stages files on it")
    return _row("disk", True, f"{d['pct']}% used, {free_gb:.0f} GB free")


def check_upgrade(exists=os.path.exists, running=None) -> dict:
    """An unattended upgrade restarts services or wants a reboot; either kills a run's children."""
    if exists("/var/run/reboot-required"):
        return _row("upgrade", False, "a reboot is pending after an upgrade; reboot before starting")
    if running is None:
        def running():
            import subprocess
            # The upgrade itself, not unattended-upgrade-shutdown: that helper runs for the life of the box and
            # its name truncates to "unattended-upgr", so `pgrep -x unattended-upgr` always matched it (17 Nov).
            return subprocess.run(["pgrep", "-f", r"/usr/bin/unattended-upgrade( |$)"], capture_output=True).returncode == 0
    try:
        if running():
            return _row("upgrade", False, "an unattended upgrade is running; start when it has finished")
    except Exception as exc:      # noqa: BLE001
        return _row("upgrade", None, f"could not look for a running upgrade: {exc}")
    return _row("upgrade", True, "no upgrade pending or running")


def _domain(address: str) -> str:
    return (address or "").rsplit("@", 1)[-1].strip().lower()


def check_dms_login(source_domain: str, target_domain: str, path: str | None = None) -> dict:
    """The DMS signs in to the target's Admin console (and approves as the source admin) with the logins in
    one root-only file that serves ONE pair; webui._dms_env ignores the whole file when a login names
    another domain, so the DMS would start and find no password."""
    path = path or DWD_ENV
    stored: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    stored[k.strip()] = v.strip()
    except FileNotFoundError:
        return _row("dms login", False, f"{path} does not exist: the DMS cannot sign in to the target's Admin console")
    except OSError as exc:
        return _row("dms login", None, f"could not read {path}: {exc}")
    other = [f"{k} names {_domain(stored[k])}" for k, want in (("DWD_EMAIL_SOURCE", source_domain), ("DWD_EMAIL", target_domain))
             if stored.get(k) and _domain(stored[k]) != (want or "").lower()]
    if other:
        return _row("dms login", False, f"{path} holds the logins of another pair ({'; '.join(other)}), "
                                        f"not {source_domain} to {target_domain}")
    if not (stored.get("DWD_PASSWORD_TARGET") or stored.get("DWD_PASSWORD")):
        return _row("dms login", False, f"{path} has no target admin password for {target_domain}")
    if not (stored.get("DWD_EMAIL_SOURCE") and stored.get("DWD_PASSWORD_SOURCE")):
        return _row("dms login", True, "the target admin can sign in; the source admin's approval waits for a click")
    return _row("dms login", True, "both admins' logins are on file, so the approval needs no click")


def check_ready(settings) -> list[dict]:
    """Both tenants, as readiness asks Google (delegation, one real read per service, ownership of the ledger)."""
    import readiness
    rows = []
    for side in ("source", "target"):
        try:
            out = readiness.check(settings, side, "migrate", gate=True)
        except Exception as exc:      # noqa: BLE001
            rows.append(_row(f"{side} ready", None, f"could not ask Google: {exc}"))
            continue
        bad = [f"{c['name']}: {c['detail']}" for c in out["checks"] if c["ok"] is False]
        unknown = [c["name"] for c in out["checks"] if c["ok"] is None]
        rows.append(_row(f"{side} ready", False if bad else (None if unknown else True),
                         "; ".join(bad[:4]) if bad else
                         (f"could not check {', '.join(unknown)}" if unknown else f"{out['domain']} is ready")))
    return rows


def check(settings, *, needs_dms: bool = False, ready=check_ready) -> list[dict]:
    if os.environ.get("BITPORT_PREFLIGHT", "1") == "0":      # the test suite: no network, no box
        return []
    rows = [check_network(), check_disk(getattr(settings, "scratch_dir", "")), check_upgrade()]
    if needs_dms:
        rows.append(check_dms_login(getattr(settings, "source_domain", ""), getattr(settings, "target_domain", "")))
    # Readiness needs the network; with none it only repeats the same cause.
    if rows[0]["ok"] is not False:
        rows += ready(settings)
    return rows


def refusal(rows: list[dict]) -> str | None:
    bad = [f"{r['name']}: {r['detail']}" for r in rows if r["ok"] is False]
    for r in rows:
        if r["ok"] is None:
            log.warning("preflight: %s could not be checked (%s); starting anyway", r["name"], r["detail"])
    return "; ".join(bad) if bad else None
