"""
ab_transfer.py
==============
Measure `server_side` against `download_upload` on the *same* corpus, into a
clean target each time, and report what actually differs.

Why a controlled run
--------------------
The two modes have been compared so far across different corpora, worker
counts and days -- confounds that make any speed ratio meaningless. This
migrates one fixed corpus twice, resetting the target between runs, so the
only variable is the transfer mode.

What is measured, and why each matters
--------------------------------------
  wall clock        the obvious one, and the least interesting
  items migrated    a mode that is fast because it moved less is not faster
  failures          a mode that is fast because it gave up is not faster
  bytes through     download_upload streams every byte twice through the host;
    the host        server_side streams none. This is what made a laptop with
                    no free memory fail, so it is a real operational cost
  native fidelity   download_upload round-trips Docs through .docx and back,
                    which is lossy by construction. server_side never converts.
                    Measured by comparing target mimeTypes against source
  modifiedTime      preserved, or reset to the migration date
  checksums         binaries should be byte-identical either way

Run it with nothing else touching the tenants.

    python3 ab_transfer.py --report ab_results.md
"""

from __future__ import annotations

import logging

import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from auth import AuthManager          # noqa: E402
from config import FOLDER_MIME, Settings  # noqa: E402
from db import MigrationDB            # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
PROGRESS = os.path.join(HERE, "logs", "ab_progress.json")
ACCOUNT_ENV: dict = {}      # the tenant pair of --account-id, for the child scripts that read env
ACCOUNT_ARGS: list = []     # main.py takes the account as a flag
ALL_SERVICES = "drive,gmail,calendar,contacts,tasks,chat"


_STATE: dict = {}


def stage(name: str) -> None:
    """Move the live page to the next stage of the current arm."""
    if _STATE:
        cur = dict(_STATE.get("current") or {}, stage=name)
        write_progress(**{**_STATE, "current": cur})


def write_progress(**fields) -> None:
    """The live state the Benchmarks page reads. Written whole each time (tmp + rename) so a
    reader never sees half a file. Totals are known up front here, so an ETA is a real
    division of measured trial times, never a guess."""
    _STATE.clear()
    _STATE.update(fields)
    try:
        os.makedirs(os.path.dirname(PROGRESS), exist_ok=True)
        tmp = PROGRESS + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({**fields, "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                       "pid": os.getpid()}, fh)
        os.replace(tmp, PROGRESS)
    except OSError:
        pass


def services_for(mail_mode: str | None) -> str:
    if mail_mode is None:
        return "drive"
    if mail_mode == "dms":      # the DMS carries all mail; this pass moves the rest
        return ",".join(x for x in ALL_SERVICES.split(",") if x != "gmail")
    return ALL_SERVICES


def mail_env(mail_mode: str | None) -> dict:
    if mail_mode == "split":    # 2026-10-30: search-first missed 9/9 known links, so exhaustive
        return {"REWRITE_DRIVE_LINKS": "true", "MAIL_ONLY_WITH_LINKS": "true",
                "MAIL_LINK_SCAN": "exhaustive"}
    return {}

NATIVE = "application/vnd.google-apps."


def net_bytes() -> int:
    """Bytes received on the primary interface, for the host-cost measure."""
    for iface in ("eth0", "ens3", "enp1s0"):
        p = f"/sys/class/net/{iface}/statistics/rx_bytes"
        if os.path.exists(p):
            with open(p) as fh:
                return int(fh.read().strip())
    return 0


def scratch_peak(settings: Settings) -> int:
    d = settings.scratch_dir
    if not os.path.isdir(d):
        return 0
    return sum(os.path.getsize(os.path.join(d, f))
               for f in os.listdir(d) if os.path.isfile(os.path.join(d, f)))


def snapshot_drive(auth: AuthManager, users: list[str], side: str) -> dict:
    """Per-user counts, mime breakdown, and the fields fidelity turns on."""
    get = auth.source_drive if side == "source" else auth.target_drive
    out = {"files": 0, "folders": 0, "bytes": 0, "native": 0, "binary": 0,
           "mimes": {}, "sample": {}}
    for u in users:
        token = None
        while True:
            r = get(u).files().list(
                q="trashed=false and 'me' in owners", spaces="drive",
                pageSize=1000, pageToken=token,
                fields=("nextPageToken,files(id,name,mimeType,size,"
                        "md5Checksum,modifiedTime)"),
                supportsAllDrives=True).execute()
            for f in r.get("files", []):
                mime = f.get("mimeType", "")
                if mime == FOLDER_MIME:
                    out["folders"] += 1
                    continue
                out["files"] += 1
                out["bytes"] += max(0, int(f.get("size") or 0))
                if mime.startswith(NATIVE):
                    out["native"] += 1
                else:
                    out["binary"] += 1
                out["mimes"][mime] = out["mimes"].get(mime, 0) + 1
                # Keyed by name so the two sides can be compared without ids.
                key = f"{u.split('@')[0]}/{f.get('name','')}"
                if key not in out["sample"]:
                    out["sample"][key] = {
                        "mime": mime,
                        "md5": f.get("md5Checksum"),
                        "mtime": (f.get("modifiedTime") or "")[:19],
                    }
            token = r.get("nextPageToken")
            if not token:
                break
    return out


def fidelity(src: dict, tgt: dict) -> dict:
    """Compare the two snapshots on the things a migration can quietly lose."""
    common = set(src["sample"]) & set(tgt["sample"])
    mime_changed = md5_changed = mtime_changed = 0
    examples = []
    for k in common:
        a, b = src["sample"][k], tgt["sample"][k]
        if a["mime"] != b["mime"]:
            mime_changed += 1
            if len(examples) < 3:
                examples.append(f"{k}: {a['mime']} -> {b['mime']}")
        if a["md5"] and b["md5"] and a["md5"] != b["md5"]:
            md5_changed += 1
        if a["mtime"] and b["mtime"] and a["mtime"] != b["mtime"]:
            mtime_changed += 1
    return {
        "compared": len(common),
        "missing_on_target": len(set(src["sample"]) - set(tgt["sample"])),
        "mime_changed": mime_changed,
        "md5_changed": md5_changed,
        "mtime_changed": mtime_changed,
        "examples": examples,
    }


def run(cmd: list[str], env: dict) -> tuple[int, float]:
    started = time.time()
    p = subprocess.run(cmd, cwd=HERE, env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return p.returncode, time.time() - started


def run_tail(cmd: list[str], env: dict) -> tuple[int, str]:
    """Like run(), but keeps the end of the output: a refusal says why in its last lines."""
    p = subprocess.run(cmd, cwd=HERE, env=env, capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()[-500:]


def one_mode(mode: str, settings: Settings, auth: AuthManager,
             users_src: list[str], users_tgt: list[str], src_snap: dict,
             mail_mode: str | None = None) -> dict:
    env = dict(os.environ)
    env["TRANSFER_MODE"] = mode
    env.update(mail_env(mail_mode))
    env.update(ACCOUNT_ENV)
    env["SANDBOX_MODE"] = "true"
    env["MIGRATE_COMMENTS"] = "true"
    env["MIGRATE_SECONDARY_CALENDARS"] = "true"

    scoped = [a for u in users_src for a in ("--user", u)]

    print(f"\n=== {mode}: emptying the target ({len(users_src)} user(s)) ===",
          flush=True)
    stage("reset target")
    reset_rc, why = run_tail([PY, "reset_target.py", "--confirm-domain", settings.target_domain,
                              "--yes", *scoped], env)
    if reset_rc != 0:
        raise RuntimeError(f"target reset refused/failed (exit {reset_rc}); ledger is unchanged and migration was not launched: {why}")

    # Scoped to the users under test, never the whole table. On a live tenant
    # the unscoped DELETE threw away 1.27M audit rows -- the record of every
    # migration ever run here, which wipe_target itself deliberately preserves
    # precisely because it is the only evidence of what happened. An
    # experiment on three mailboxes has no business destroying that.
    print(f"=== {mode}: clearing the ledger for those users ===", flush=True)
    stage("reset ledger")
    db = MigrationDB(settings.db_path)
    ph = ",".join("?" * len(users_src))
    with db.write() as c:
        for t, col in (("id_mapping", "source_user"), ("audit_log", "source_user"),
                       ("label_map", "source_user")):
            try:
                c.execute(f"DELETE FROM {t} WHERE {col} IN ({ph})", users_src)
            except Exception:  # noqa: BLE001
                logging.getLogger(__name__).debug("ignored an error", exc_info=True)
        try:
            c.execute(f"DELETE FROM upload_ledger WHERE target_user IN ({ph})",
                      users_tgt)
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).debug("ignored an error", exc_info=True)
        c.execute(f"UPDATE identity_map SET status='PENDING', notes=NULL "
                  f"WHERE source_email IN ({ph})", users_src)
    db._mapping_cache.clear()
    db._mapping_cached_users.clear()

    print(f"=== {mode}: migrating ===", flush=True)
    stage("migrate")
    net0 = net_bytes()
    services = services_for(mail_mode)
    rc, secs = run([PY, "main.py", *ACCOUNT_ARGS, "migrate", "--services", services, *scoped], env)
    net1 = net_bytes()

    db = MigrationDB(settings.db_path)
    counts = {t: n for t, n in db.conn.execute(
        f"SELECT item_type, COUNT(*) FROM audit_log WHERE status='SUCCESS' "
        f"AND source_user IN ({ph}) GROUP BY item_type", users_src)}
    failed = db.conn.execute(
        f"SELECT COUNT(*) FROM audit_log WHERE status LIKE 'FAILED%' "
        f"AND source_user IN ({ph})", users_src).fetchone()[0]

    print(f"=== {mode}: snapshotting the target ===", flush=True)
    stage("snapshot target")
    tgt_snap = snapshot_drive(auth, users_tgt, "target")

    verdicts = {}
    if mail_mode is not None:
        print(f"=== {mode}+{mail_mode}: verifying one to one ===", flush=True)
        stage("verify 1:1")
        run([PY, "verify_sample.py", *ACCOUNT_ARGS[:2], *scoped, "--services", "all"], env)
        db = MigrationDB(settings.db_path)
        for u in users_src:
            for svc, v in db.conn.execute(
                    "SELECT service, verdict FROM user_verification WHERE source_user=?", (u,)):
                verdicts[f"{u.split('@')[0]}/{svc}"] = v

    return {
        "mode": mode,
        "mailMode": mail_mode,
        "verdicts": verdicts,
        "clean": rc == 0 and not failed and all(
            v in ("IDENTICAL", "COMPLETE") for v in verdicts.values()),
        "exit": rc,
        "seconds": round(secs, 1),
        "migrated": counts,
        "failed": failed,
        "host_bytes_in": net1 - net0,
        "target": {k: v for k, v in tgt_snap.items() if k != "sample"},
        "fidelity": fidelity(src_snap, tgt_snap),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="A/B the two Drive transfer modes.")
    ap.add_argument("--report", default="ab_results.md")
    ap.add_argument("--user", action="append", metavar="SOURCE_EMAIL",
                    help="run the experiment on these users only. Strongly "
                         "advised on a real tenant: the full corpus here is "
                         "489k files, which extrapolates to ~6 days PER ARM.")
    ap.add_argument("--mode", action="append",
                    choices=["server_side", "download_upload"],
                    help="default: both, server_side first")
    ap.add_argument("--account-id", type=int,
                    help="a SaaS account's tenant pair and ledger (default: the box's own)")
    ap.add_argument("--mail-mode", action="append", choices=["engine", "split", "dms"],
                    help="also move mail, once per value, crossed with every --mode. "
                         "Without it this is the Drive-only A/B it always was.")
    args = ap.parse_args(argv)

    settings = Settings(account_id=args.account_id)
    if args.account_id is not None:
        ACCOUNT_ARGS[:] = ["--account-id", str(args.account_id)]
        ACCOUNT_ENV.update(TARGET_DOMAIN=settings.target_domain, TARGET_ADMIN=settings.target_admin,
                           TARGET_SA_KEY=settings.target_sa_key, MIGRATION_DB=settings.db_path)
    auth = AuthManager(settings)
    db = MigrationDB(settings.db_path)
    rows = [r for r in db.all_identities() if r["entity_type"] == "user"]
    if args.user:
        wanted = {u.lower() for u in args.user}
        rows = [r for r in rows if r["source_email"].lower() in wanted]
        missing = wanted - {r["source_email"].lower() for r in rows}
        if missing:
            print(f"not in identity_map: {sorted(missing)}")
            return 1
    users_src = [r["source_email"] for r in rows]
    users_tgt = [r["target_email"] for r in rows]
    if not users_src:
        print("identity_map is empty.")
        return 1

    print("=== snapshotting the source (the fixed corpus) ===", flush=True)
    src_snap = snapshot_drive(auth, users_src, "source")
    print(f"    {src_snap['files']} files ({src_snap['native']} native, "
          f"{src_snap['binary']} binary), {src_snap['folders']} folders, "
          f"{src_snap['bytes']/1e9:.2f} GB", flush=True)

    results = []
    arms = [(m, mm) for m in (args.mode or ["server_side", "download_upload"])
            for mm in (args.mail_mode or [None])]
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    durations: list[float] = []
    try:
        for i, (mode, mail_mode) in enumerate(arms):
            write_progress(running=True, startedAt=started, total=len(arms), done=i,
                           armStartedAt=time.time(),
                           current={"mode": mode, "mailMode": mail_mode, "stage": "starting"},
                           durations=durations, users=users_src,
                           corpus={"files": src_snap["files"], "bytes": src_snap["bytes"]})
            results.append(one_mode(mode, settings, auth, users_src, users_tgt,
                                    src_snap, mail_mode))
            durations.append(results[-1]["seconds"])
            with open(args.report + ".json", "w") as fh:
                json.dump({"source": {k: v for k, v in src_snap.items()
                                      if k != "sample"},
                           "runs": results}, fh, indent=2)
    except BaseException as exc:      # the page must say it failed, and why, not sit at "0 of N"
        write_progress(**{**_STATE, "running": False, "current": None,
                          "error": f"{type(exc).__name__}: {str(exc)[:600]}",
                          "finishedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        raise
    write_progress(running=False, startedAt=started, total=len(arms), done=len(arms),
                   current=None, durations=durations, users=users_src,
                   finishedAt=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    lines = ["# Transfer mode A/B", "",
             f"Source corpus: **{src_snap['files']} files** "
             f"({src_snap['native']} native, {src_snap['binary']} binary), "
             f"{src_snap['folders']} folders, {src_snap['bytes']/1e9:.2f} GB",
             "",
             "| metric | " + " | ".join(
                 r["mode"] + (f" + {r['mailMode']}" if r.get("mailMode") else "") for r in results) + " |",
             "|---|" + "---|" * len(results)]

    def row(name, fn):
        lines.append(f"| {name} | " + " | ".join(str(fn(r)) for r in results) + " |")

    row("wall clock (s)", lambda r: f"{r['seconds']:,.0f}")
    row("files migrated", lambda r: f"{r['migrated'].get('file', 0):,}")
    row("folders", lambda r: f"{r['migrated'].get('folder', 0):,}")
    row("failures", lambda r: r["failed"])
    row("bytes through host", lambda r: f"{r['host_bytes_in']/1e6:.0f} MB")
    row("target files", lambda r: f"{r['target']['files']:,}")
    row("target native", lambda r: f"{r['target']['native']:,}")
    row("files compared", lambda r: r["fidelity"]["compared"])
    row("missing on target", lambda r: r["fidelity"]["missing_on_target"])
    row("**mimeType changed**", lambda r: r["fidelity"]["mime_changed"])
    row("checksum changed", lambda r: r["fidelity"]["md5_changed"])
    row("modifiedTime changed", lambda r: r["fidelity"]["mtime_changed"])

    for r in results:
        if r["fidelity"]["examples"]:
            lines += ["", f"### {r['mode']} — type changes", ""]
            lines += [f"- `{e}`" for e in r["fidelity"]["examples"]]

    lines += ["", "Generated " + time.strftime("%Y-%m-%d %H:%M:%S UTC",
                                               time.gmtime())]
    with open(args.report, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    print("\n" + "\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
