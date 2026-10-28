"""Opt-in background admission, cooperative yielding and bounded API pacing."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone

import control_plane_db as cpdb

log = logging.getLogger(__name__)
_active = False
_reason = ""
_until = 0.0
_account_id = None
_lock = threading.Lock()
_pace_lock = threading.Lock()
_next_request = 0.0


class Deferred(RuntimeError):
    """Read-only identity discovery yielded before any migration could start."""


def pace():
    global _next_request
    with _pace_lock:
        delay = _next_request - time.monotonic()
        if delay > 0:
            time.sleep(min(delay, 1.0))
        _next_request = time.monotonic() + 1.0


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def config(account_id):
    try:
        with cpdb.ro() as c:
            row = c.execute("SELECT * FROM background_deltas WHERE account_id=?", (account_id,)).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return {"background": False, "follow_domains": False, "state": "OFF", "recovery_admins": {}}
    out = dict(row) if row else {}
    out["background"] = bool(out.get("background"))
    out["follow_domains"] = bool(out.get("follow_domains"))
    out["recovery_admins"] = json.loads(out.get("recovery_admins") or "{}")
    if out.get("observation"):
        out["observation"] = json.loads(out["observation"])
    return out


def state(account_id, value, detail):
    with cpdb.rw() as c:
        c.execute("UPDATE background_deltas SET state=?,detail=?,updated_at=? WHERE account_id=?",
                  (value, detail[:500], now(), account_id))


def waiting_reason(account_id=None):
    """No background overlap with foreground work, even on another account."""
    try:
        with cpdb.ro() as c:
            enabled = c.execute("SELECT enabled FROM mirror_settings WHERE account_id=?", (account_id,)).fetchone()
            if enabled is not None and not enabled[0]:
                return "Background follow-up is switched off"
            if c.execute("SELECT 1 FROM active_jobs WHERE job_name!='mirror' OR account_id IS NOT ? LIMIT 1",
                         (account_id,)).fetchone():
                return "Foreground migration, verification or repair has priority"
            if c.execute("SELECT 1 FROM job_queue WHERE status IN ('queued','running') AND job_name!='mirror' LIMIT 1").fetchone():
                return "An operator job is waiting for resources"
        from resources import cached_probe, pressure_severe
        r = cached_probe()
        if r.ram_estimated or not r.ram_total_gb:
            return "Reliable memory availability is unknown"
        if pressure_severe(r) or r.ram_usable_gb < max(1.0, r.ram_total_gb * .2):
            return "Keeping memory available for foreground work"
        if hasattr(os, "getloadavg") and os.getloadavg()[0] > max(1, r.cpu_logical) * .75:
            return "CPU is busy"
    except Exception:
        return "Cannot establish that background work is safe to run"
    return ""


def install(settings):
    """This process only: one worker and one API request/second, low OS priority."""
    global _active, _account_id
    if _active:
        return
    _active, _account_id = True, settings.account_id
    settings.user_workers = settings.drive_file_workers = 1
    settings.mail_workers = 1
    settings.download_chunk_bytes = min(settings.download_chunk_bytes, 4 * 1024 * 1024)
    settings._background_delta = True
    if settings.transfer_mode in ("link_flip", "move"):
        settings.transfer_mode = "server_side"
    try:
        os.nice(15)
    except (AttributeError, OSError):
        log.info("OS niceness unavailable; single-worker and API pacing remain active")
    import shutil
    import subprocess
    ionice = shutil.which("ionice")
    if ionice:
        subprocess.run([ionice, "-c", "3", "-p", str(os.getpid())],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    from googleapiclient.http import HttpRequest
    execute = HttpRequest.execute

    def paced(request, *args, **kwargs):
        # Do not throw midway through an item's write/readback/ledger commit.
        # The ordinary engine checkpoints stop after that item is settled.
        checkpoint(settings.account_id)
        pace()
        return execute(request, *args, **kwargs)
    HttpRequest.execute = paced


def checkpoint(account_id=None):
    global _until, _reason
    if not _active:
        return False
    if account_id is None:
        account_id = _account_id
    with _lock:
        if _reason:
            return True
        if time.monotonic() < _until:
            return False
        _until = time.monotonic() + 2.0
        reason = waiting_reason(account_id)
        if reason:
            _reason = reason
            import main
            main.SHUTDOWN.set()
            log.info("Background delta yielding at its next safe checkpoint: %s", reason)
            return True
    return False


def yield_reason():
    return _reason


def finish(account_id, db, result, observation=None):
    """Stop only after handover and a later quiet, complete supported-change pass."""
    if _reason:
        state(account_id, "OFF" if "switched off" in _reason else "WAITING", _reason)
        return
    cfg = config(account_id)
    if not cfg.get("follow_domains"):
        state(account_id, "FOLLOWING", "Background cycle finished; see the saved cycle for findings")
        return
    if not observation or not observation.get("handover"):
        state(account_id, "FOLLOWING", "Waiting for the verified tenant domain handover")
        return
    import mirror_scheduler
    selected = mirror_scheduler.get_settings(account_id).get("users") or [
        r["source_email"] for r in db.all_identities() if r["entity_type"] == "user" and r["status"] == "DONE"]
    if not selected:
        state(account_id, "BLOCKED", "No completed mapped users are selected")
        return
    scope = "source_user IN (" + ",".join("?" for _ in selected) + ")"
    pending = db.conn.execute("SELECT COUNT(*) FROM mirror_retry WHERE " + scope, selected).fetchone()[0]
    held = db.conn.execute("SELECT COUNT(*) FROM mirror_deletions WHERE status IN ('proposed','awaiting','failed') AND " + scope, selected).fetchone()[0]
    unfinished = db.conn.execute("SELECT COUNT(*) FROM audit_log WHERE ((item_type='links_pass' AND status='PENDING') OR (item_type='link_rewrite' AND status='FAILED')) AND " + scope, selected).fetchone()[0]
    clean = (result.get("status") == "ok" and not result.get("errors") and not result.get("unknown")
             and not result.get("conflicts") and not pending and not held and not unfinished)
    quiet = clean and not any(result.get("counts", {}).values())
    with cpdb.rw() as c:
        prior = c.execute("SELECT quiet_since FROM background_deltas WHERE account_id=?", (account_id,)).fetchone()
        since = prior[0] if prior else None
        complete = bool(quiet and since and (datetime.now(timezone.utc) -
            datetime.fromisoformat(since.replace('Z', '+00:00'))).total_seconds() >= 300)
        c.execute("UPDATE background_deltas SET quiet_since=?, state=?, detail=?, completed_at=?, updated_at=? WHERE account_id=?",
            (since or now() if quiet else None, "COMPLETE" if complete else "FINAL_CATCHUP",
             "Final supported-change catch-up completed; this is not a full 1:1 certificate" if complete else
             "Handover verified; waiting for a clean quiet catch-up pass and no pending items",
             now() if complete else None, now(), account_id))
        if complete:
            c.execute("UPDATE mirror_settings SET enabled=0 WHERE account_id=?", (account_id,))
