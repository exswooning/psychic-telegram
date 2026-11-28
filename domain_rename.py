"""
domain_rename.py -- carry one account's migration across a primary-domain rename.

The case (account 7, Nov 2026): Drive is copied dharan.gov.np -> new.mdharan.com, then
the operator renames both tenants in Google -- the source to old.mdharan.com (dharan.gov.np
leaves it), the target to dharan.gov.np. Delegation is per Google customer, so the one key
keeps working; everything Bitport keeps by address does not: every ledger row is keyed by
email, the ledger's pair stamp no longer matches (db.claim_for_pair would archive it and the
next run re-copy all of Drive), the source's government protection would not cover its new
name, and the domain guard would refuse every write into a target called dharan.gov.np.

  plan    records the rename (control-plane table domain_renames, kept for good); from
          that moment the source's new name is government too (domain_guard.is_government)
  detect  asks Google, per side, as that side's admin under the NEW name: does the new
          address exist as this tenant's admin, and has the old one stopped being primary?
          pending / renamed / unknown -- any error is unknown, never a guess
  apply   (no job running, or --stop-jobs) backs the ledger up, rewrites every address in
          every text column of every table -- source side first, then target, because the
          target's new name is the source's old one -- re-stamps the pair, and points the
          account's tenant_configs at the new names
  status  the plan as recorded

The API runs auto_step each oversight sweep for an account with a plan not yet applied:
when both sides read renamed it stops the account's jobs, applies, and says which run picks
the work up again (api_server._domain_rename_sweep).

  python domain_rename.py plan --account-id 7 --source dharan.gov.np:old.mdharan.com \
      --target new.mdharan.com:dharan.gov.np
  python domain_rename.py status|detect --account-id 7
  python domain_rename.py apply --account-id 7 [--dry-run] [--stop-jobs]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone

import control_plane_db as cpdb
import domain_guard
import job_admission

USER_RO = "https://www.googleapis.com/auth/admin.directory.user.readonly"
SIDES = ("source", "target")
STOP_WAIT_SEC = int(os.getenv("DOMAIN_RENAME_STOP_WAIT_SEC", "1800"))
_norm = domain_guard._norm


def plans(account_id: int, pending_only: bool = False) -> list[dict]:
    with cpdb.ro() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM domain_renames WHERE account_id=?"
            + (" AND applied_at IS NULL" if pending_only else "") + " ORDER BY id", (account_id,))]


def accounts_pending() -> list[int]:
    try:
        with cpdb.ro() as c:
            return [r[0] for r in c.execute(
                "SELECT DISTINCT account_id FROM domain_renames WHERE applied_at IS NULL")]
    except sqlite3.OperationalError:      # an install before migration 018
        return []


def _configs(account_id: int) -> dict:
    with cpdb.ro() as c:
        return {r["side"]: dict(r) for r in c.execute(
            "SELECT side, domain, admin_email FROM tenant_configs WHERE account_id=?", (account_id,))}


def plan(account_id: int, renames: dict, by: str) -> list[dict]:
    """Record `renames` ({side: (from, to)}) for the account. Each side's from must be what
    it is configured with now; its admin keeps the same local part under the new name."""
    if plans(account_id, pending_only=True):
        raise ValueError(f"account {account_id} already has a rename planned and not applied")
    cfg = _configs(account_id)
    rows = []
    for side, (frm, to) in renames.items():
        frm, to = _norm(frm), _norm(to)
        have = cfg.get(side) or {}
        if not frm or not to or frm == to:
            raise ValueError(f"{side}: a rename needs two different domains")
        if _norm(have.get("domain")) != frm:
            raise ValueError(f"{side} is configured as {have.get('domain')!r}, not {frm}")
        admin = (have.get("admin_email") or "").strip().lower()
        if not admin.endswith("@" + frm):
            raise ValueError(f"{side} admin {admin!r} is not on {frm}")
        rows.append((account_id, side, frm, to, admin.split("@")[0], by or "operator"))
    with cpdb.rw() as c:
        c.executemany("INSERT INTO domain_renames (account_id, side, from_domain, to_domain, "
                      "admin_local, recorded_by) VALUES (?,?,?,?,?,?)", rows)
        # The source's new name is a source for good, like its old one (migration 013).
        c.executemany("INSERT OR IGNORE INTO source_domain_protections(domain) VALUES (?)",
                      [(r[3],) for r in rows if r[1] == "source"])
    domain_guard.forget_renames()
    return plans(account_id, pending_only=True)


# -- detect ------------------------------------------------------------------------------
def _lookup(key_path: str, subject: str, user_key: str) -> dict | None:
    """users.get as `subject`, with that side's key. None: the address does not resolve
    in this tenant (404, or 403 for a user of another customer). Anything else raises."""
    import google_auth_httplib2
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    from auth import _http
    creds = service_account.Credentials.from_service_account_file(
        key_path, scopes=[USER_RO]).with_subject(subject)
    d = build("admin", "directory_v1", http=google_auth_httplib2.AuthorizedHttp(creds, http=_http(60)),
              cache_discovery=False)
    try:
        return d.users().get(userKey=user_key, fields="primaryEmail,customerId").execute()
    except HttpError as exc:
        if exc.resp.status == 404 or (exc.resp.status == 403 and b"Not Authorized" in (exc.content or b"")):
            return None
        raise


def _primary(u) -> str:
    return ((u or {}).get("primaryEmail") or "").strip().lower()


def side_state(p: dict, key_path: str, lookup=_lookup) -> tuple[str, str]:
    """(pending|renamed|unknown, why) for one planned side. renamed needs the new address
    to be this tenant's (the customer id read while it was pending) and primary, and the
    old one gone or only an alias; pending needs the old one still primary here."""
    old, new = f"{p['admin_local']}@{p['from_domain']}", f"{p['admin_local']}@{p['to_domain']}"
    cust = p.get("customer_id")
    try:
        u = lookup(key_path, new, new)
    except Exception:      # noqa: BLE001 - no token as the new address: not renamed (yet)
        u = None
    if u and cust and u.get("customerId") == cust and _primary(u) == new:
        try:
            o = lookup(key_path, new, old)
        except Exception as exc:      # noqa: BLE001
            return "unknown", f"{new} exists, but {old} could not be read: {exc}"[:300]
        if o is None or (o.get("customerId") == cust and _primary(o) != old):
            return "renamed", f"{new} is this tenant's admin; {old} " + (
                "no longer resolves" if o is None else f"is an alias of {_primary(o)}")
        return "unknown", f"{new} exists and {old} still reads as {_primary(o)}"
    # Not provably renamed here (the new address may be another tenant's: dharan's own
    # info@ lives on the source until the rename). Pending needs the old one still primary.
    try:
        o = lookup(key_path, old, old)
    except Exception as exc:      # noqa: BLE001
        return "unknown", f"neither {new} nor {old} could be read: {exc}"[:300]
    if o and _primary(o) == old and o.get("customerId") and (not cust or o["customerId"] == cust):
        if not cust:
            with cpdb.rw() as c:
                c.execute("UPDATE domain_renames SET customer_id=? WHERE id=? AND customer_id IS NULL",
                          (o["customerId"], p["id"]))
        return "pending", f"{old} is still the primary address"
    return "unknown", f"{old} reads as {_primary(o) or 'nothing'}"


def detect(account_id: int, lookup=_lookup) -> dict:
    from config import Settings
    st = Settings(account_id=account_id)
    keys = {"source": st.source_sa_key, "target": st.target_sa_key}
    out = {}
    for p in plans(account_id, pending_only=True):
        state, why = side_state(p, keys[p["side"]], lookup)
        out[p["side"]] = {"state": state, "detail": why, "from": p["from_domain"], "to": p["to_domain"]}
    return out


# -- apply -------------------------------------------------------------------------------
def _rewriter(frm: str, to: str):
    """'@frm' -> '@to' anywhere in a text (an address, a JSON payload), and a value that is
    just the domain; '@frm.other' and 'x.frm' are other domains and stay."""
    at = re.compile("@" + re.escape(frm) + r"(?![a-z0-9-]|\.[a-z0-9])", re.I)

    def fn(v):
        if not isinstance(v, str):
            return v
        return to if _norm(v) == frm else at.sub("@" + to, v)
    return fn


def _rewrite(conn, steps: list[dict], write: bool) -> list[dict]:
    """Every column of every table, step by step; {side, from, to, rows, columns}. Before a
    step writes, its new name must appear nowhere -- two meanings would merge."""
    cols = [(t, c[1]) for (t,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
        for c in conn.execute(f'PRAGMA table_info("{t}")').fetchall()]
    out = []
    for p in steps:
        frm, to = p["from_domain"], p["to_domain"]
        rw, probe = _rewriter(frm, to), _rewriter(to, "\0")
        conn.create_function("bp_rename", 1, rw, deterministic=True)
        conn.create_function("bp_has_to", 1, lambda v: int(isinstance(v, str) and probe(v) != v),
                             deterministic=True)
        conn.create_function("bp_has_from", 1, lambda v: int(isinstance(v, str) and rw(v) != v),
                             deterministic=True)
        if write:
            clash = [f"{t}.{c}" for t, c in cols if conn.execute(
                f'SELECT 1 FROM "{t}" WHERE typeof("{c}")=\'text\' AND instr(lower("{c}"), ?) > 0 '
                f'AND bp_has_to("{c}") LIMIT 1', (to,)).fetchone()]
            if clash:
                raise ValueError(f"{to} already appears in {', '.join(clash[:5])}: renaming {frm} to it "
                                 "would merge two tenants' rows")
        hits = {}
        for t, c in cols:
            where = f'WHERE typeof("{c}")=\'text\' AND instr(lower("{c}"), ?) > 0 AND bp_has_from("{c}")'
            n = (conn.execute(f'UPDATE "{t}" SET "{c}"=bp_rename("{c}") {where}', (frm,)).rowcount if write
                 else conn.execute(f'SELECT COUNT(*) FROM "{t}" {where}', (frm,)).fetchone()[0])
            if n:
                hits[f"{t}.{c}"] = n
        out.append({"side": p["side"], "from": frm, "to": to, "rows": sum(hits.values()), "columns": hits})
    return out


def _account_jobs(account_id: int) -> list[dict]:
    return [j for j in job_admission.list_active() if j.get("account_id") == account_id]


def stop_jobs(account_id: int, why: str) -> int:
    """The Stop an operator's button sends: SIGINT, noted on the admission row, audited
    (run_watch reads the audit and records the run STOPPED, not crashed)."""
    n = 0
    for j in _account_jobs(account_id):
        pid = j.get("pid")
        if not pid:
            continue
        action = cpdb.begin_action("system", "system", "domain-rename.stop", why, str(pid),
                                   {"pid": pid, "job": j.get("job_name")}, None, account_id)
        try:
            os.kill(pid, 2)
            job_admission.note_stop(pid)
            cpdb.finish_action(action, "OK", f"SIGINT -> {pid}")
            n += 1
        except ProcessLookupError:
            cpdb.finish_action(action, "OK", f"{pid} had already exited")
    return n


def apply(account_id: int, *, dry_run: bool = False, stop: bool = False, by: str = "operator",
          wait: int = STOP_WAIT_SEC) -> dict:
    todo = sorted(plans(account_id, pending_only=True), key=lambda p: SIDES.index(p["side"]))
    if not todo:
        return {"ok": False, "detail": f"account {account_id} has no rename waiting"}
    from config import Settings
    path = Settings(account_id=account_id).db_path
    cfg = _configs(account_id)
    # Only ever from the names the account is configured with: a second apply would rename
    # the target's new dharan.gov.np addresses to old.mdharan.com.
    wrong = [p["side"] for p in todo if _norm((cfg.get(p["side"]) or {}).get("domain")) != p["from_domain"]]
    if wrong:
        return {"ok": False, "detail": f"{', '.join(wrong)} is no longer configured as the planned "
                                       "old name -- refusing to rewrite the ledger again"}
    if dry_run:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        try:
            return {"ok": True, "dryRun": True, "ledger": path, "steps": _rewrite(conn, todo, write=False)}
        finally:
            conn.close()
    running = _busy(account_id, path)
    if running:
        if not stop:
            return {"ok": False, "detail": f"{', '.join(running)} running for account {account_id}; "
                                           "stop it first or pass --stop-jobs"}
        stop_jobs(account_id, "domain rename: the ledger is rewritten to the new addresses")
        # Quiet twice, 5 s apart: a stopped run's follow-on (repair, tally) starts the instant it leaves.
        deadline, quiet = time.monotonic() + wait, 0
        while quiet < 2 and time.monotonic() < deadline:
            time.sleep(5)
            quiet = 0 if _busy(account_id, path) else quiet + 1
        if quiet < 2:
            return {"ok": False, "detail": f"still running {wait}s after Stop: "
                                           f"{', '.join(_busy(account_id, path))}; nothing changed"}
    # Hold the account while the ledger is rewritten: verification and its repairs wait for it.
    held, why = job_admission.try_admit(account_id, "domain-rename", os.getpid())
    if not held:
        return {"ok": False, "detail": f"could not hold account {account_id}: {why}; nothing changed"}
    try:
        running = _busy(account_id, path)
        if running:
            return {"ok": False, "detail": f"{', '.join(running)} started meanwhile; nothing changed"}
        res = _commit(account_id, path, todo, cfg)
    finally:
        job_admission.release(account_id, "domain-rename")
    if res.get("ok"):
        action = cpdb.begin_action(by, "system", "domain-rename.apply", "primary-domain rename applied",
                                   f"account:{account_id}", res, None, account_id)
        cpdb.finish_action(action, "OK", " -> ".join(res["pair"]))
    domain_guard.forget_renames()
    return res


def _busy(account_id: int, path: str) -> list[str]:
    """What else may write this account's ledger: an admitted job, or the API's repair thread
    (on no admission list; its open repair_runs row says it runs)."""
    out = [j["job_name"] for j in _account_jobs(account_id) if j.get("job_name") != "domain-rename"]
    try:
        with cpdb.ro(path) as c:
            if c.execute("SELECT 1 FROM repair_runs WHERE finished_at IS NULL LIMIT 1").fetchone():
                out.append("repair")
    except sqlite3.Error:      # no ledger, or one before repair_runs: nothing repairs it
        pass
    return out


def _commit(account_id: int, path: str, todo: list[dict], cfg: dict) -> dict:
    """Backup, rewrite, re-stamp, re-point the config, mark applied -- or nothing at all."""
    backup = f"{path}.pre-rename-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    conn = sqlite3.connect(path, timeout=60, isolation_level=None)
    configured = False
    try:
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        dst = sqlite3.connect(backup)
        conn.backup(dst)
        dst.close()
        conn.execute("BEGIN IMMEDIATE")
        steps = _rewrite(conn, todo, write=True)      # source first: see SIDES
        new = {s: _norm((cfg.get(s) or {}).get("domain")) for s in SIDES}
        new.update({p["side"]: p["to_domain"] for p in todo})
        conn.execute("INSERT INTO ledger_pair (id, source_domain, target_domain, stamped_at) VALUES (1,?,?,?) "
                     "ON CONFLICT(id) DO UPDATE SET source_domain=excluded.source_domain, "
                     "target_domain=excluded.target_domain, stamped_at=excluded.stamped_at",
                     (new["source"], new["target"], datetime.now(timezone.utc).isoformat()))
        with cpdb.rw() as c:
            for p in todo:
                c.execute("UPDATE tenant_configs SET domain=?, admin_email=?, "
                          "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE account_id=? AND side=?",
                          (p["to_domain"], f"{p['admin_local']}@{p['to_domain']}", account_id, p["side"]))
        configured = True
        conn.execute("COMMIT")
    except BaseException as exc:      # noqa: BLE001 - a Stop too: the ledger rolls back; the backup stays
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        if configured:
            with cpdb.rw() as c:
                for side, row in cfg.items():
                    c.execute("UPDATE tenant_configs SET domain=?, admin_email=? WHERE account_id=? AND side=?",
                              (row["domain"], row["admin_email"], account_id, side))
        return {"ok": False, "detail": f"nothing changed: {exc}"[:500], "backup": backup}
    finally:
        conn.close()
    with cpdb.rw() as c:
        c.executemany("UPDATE domain_renames SET applied_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                      [(p["id"],) for p in todo])
    return {"ok": True, "ledger": path, "backup": backup, "steps": steps,
            "pair": [new["source"], new["target"]]}


# -- the automatic path ------------------------------------------------------------------
def needs_migrate(path: str) -> bool:
    """A user not finished: anything but DONE, or FAILED for something other than the
    one-to-one acceptance alone (a re-run would not change that; the checks would)."""
    with cpdb.ro(path) as c:
        rows = c.execute("SELECT status, notes FROM identity_map WHERE entity_type='user'").fetchall()
    for status, notes in rows:
        if status == "DONE":
            continue
        parts = [x.strip() for x in (notes or "").split(";") if x.strip()]
        if status == "FAILED" and parts and all(x.startswith("acceptance:") for x in parts):
            continue
        return True
    return False


def _incident(account_id, step: str, title: str, summary: str, severity: str = "warn") -> None:
    import run_watch
    run_watch.open_incident(kind="domain_rename", title=title, summary=summary, account_id=account_id,
                            job_name="domain-rename", fingerprint=f"domain-rename:{account_id}:{step}",
                            severity=severity)


def auto_step(account_id: int, *, lookup=_lookup, baseline=None, apply_fn=None) -> dict:
    """One automatic look. Both sides renamed: stop, apply, retake the source baseline, and
    return what should pick the work up ({"resume": "migrate"|"verify"}). One side, or any
    unknown: an incident and nothing else. Both pending: nothing."""
    states = detect(account_id, lookup)
    got = {s["state"] for s in states.values()}
    said = "; ".join(f"{side}: {s['state']} ({s['detail']})" for side, s in states.items())
    if not states or got == {"pending"}:
        return {"state": "pending", "sides": states}
    if got != {"renamed"}:
        _incident(account_id, "unsure", "Domain rename seen on one side only, or not readable",
                  f"Nothing was changed. {said}")
        return {"state": "waiting", "sides": states}
    _incident(account_id, "detected", "Domain rename detected on both sides",
              f"Stopping the account's jobs and rewriting its ledger. {said}", "info")
    res = (apply_fn or apply)(account_id, stop=True, by="automatic")
    if not res.get("ok"):
        _incident(account_id, "failed", "Domain rename could not be applied",
                  f"{res.get('detail')} (backup: {res.get('backup') or 'none taken'})", "error")
        return {"state": "failed", "sides": states, "apply": res}
    _incident(account_id, "applied", "Domain rename applied",
              f"Ledger now {' -> '.join(res.get('pair') or [])}; backup {res.get('backup')}; "
              + ", ".join(f"{s['side']} {s['from']} -> {s['to']}: {s['rows']} row(s)" for s in res["steps"]),
              "info")
    try:
        if baseline:
            baseline(account_id)
    except Exception as exc:      # noqa: BLE001 - the guard keeps the old baseline; say so
        _incident(account_id, "baseline", "Source baseline not retaken after the rename", repr(exc)[:300])
    from config import Settings
    resume = "migrate" if needs_migrate(Settings(account_id=account_id).db_path) else "verify"
    return {"state": "applied", "sides": states, "apply": res, "resume": resume}


def resumed(account_id: int, what: str, ok: bool, detail: str) -> None:
    _incident(account_id, "resumed" if ok else "resume-failed",
              f"After the domain rename: {what} {'started' if ok else 'not started'}", str(detail)[:500],
              "info" if ok else "error")


# -- CLI ---------------------------------------------------------------------------------
def _pair(text: str) -> tuple[str, str]:
    frm, sep, to = (text or "").partition(":")
    if not sep:
        raise argparse.ArgumentTypeError("FROM:TO")
    return frm, to


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=("plan", "status", "detect", "apply"))
    ap.add_argument("--account-id", type=int, required=True)
    ap.add_argument("--source", type=_pair)
    ap.add_argument("--target", type=_pair)
    ap.add_argument("--by", default=os.getenv("USER") or "operator")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--stop-jobs", action="store_true")
    a = ap.parse_args(argv)
    if a.command == "plan":
        got = {s: v for s, v in (("source", a.source), ("target", a.target)) if v}
        if not got:
            ap.error("plan needs --source and/or --target FROM:TO")
        out = {"planned": plan(a.account_id, got, a.by)}
        try:
            out["detect"] = detect(a.account_id)     # reads each tenant's id while it is still pending
        except Exception as exc:      # noqa: BLE001 - recorded either way
            out["detect"] = repr(exc)
    elif a.command == "status":
        out = {"plans": plans(a.account_id)}
    elif a.command == "detect":
        out = detect(a.account_id)
    else:
        out = apply(a.account_id, dry_run=a.dry_run, stop=a.stop_jobs, by=a.by)
    print(json.dumps(out, indent=2, default=str))
    return 0 if not isinstance(out, dict) or out.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
