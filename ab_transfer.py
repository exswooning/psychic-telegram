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
import copy
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from auth import AuthManager          # noqa: E402
from config import FOLDER_MIME, Settings  # noqa: E402
from db import MigrationDB            # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
PROGRESS = os.path.join(HERE, "logs", "ab_progress.json")
ACCOUNT_ENV: dict = {}      # the tenant pair of --account-id, for the child scripts that read env
ACCOUNT_ARGS: list = []     # main.py takes the account as a flag
# Chat is left out for now (operator, 2026-10-31): its 4 findings are Chat API limits that add the same
# count to every arm, and its import is slow. One switch: put "chat" back here and in VERIFY_SERVICES.
ALL_SERVICES = "drive,gmail,calendar,contacts,tasks"
VERIFY_SERVICES = "drive,gmail,calendar,contacts,tasks,directory"


_STATE: dict = {}
_ARMS: dict = {}            # arm label -> what it is doing now; several at once when arms run side by side
_LOCK = threading.Lock()
_DMS_LOCK = threading.Lock()    # the unattended approval drives one browser (Xvfb :99): one import at a time
# Measured 2026-10-30 on the sandbox pair: 68 min against 27 for the same arm on server_side, and
# its Drive differences were download_upload's own. Skipped by default; --include-slow runs it.
SLOW_ARM = ("download_upload", "engine")


def arm_label(mode: str, mail_mode: str | None) -> str:
    return mode + (f" + {mail_mode}" if mail_mode else "")


def stage(name: str, label: str | None = None) -> None:
    """Move the live page to the next stage of an arm (the only one, unless several run at once)."""
    with _LOCK:
        arm = _ARMS.get(label) if label else next(iter(_ARMS.values()), None)
        if arm is None:
            return
        arm["stage"] = name
    write_progress()


def write_progress(**fields) -> None:
    """The live state the Benchmarks page reads. Written whole each time (tmp + rename) so a
    reader never sees half a file. Totals are known up front here, so an ETA is a real
    division of measured trial times, never a guess. `arms` lists every arm in flight."""
    with _LOCK:
        _STATE.update(fields)
        arms = [dict(a) for a in _ARMS.values()]
        state = {**_STATE, "arms": arms}
        if arms:
            state["current"] = {k: arms[0][k] for k in ("mode", "mailMode", "stage")}
        try:
            os.makedirs(os.path.dirname(PROGRESS), exist_ok=True)
            tmp = PROGRESS + ".tmp"
            with open(tmp, "w") as fh:
                json.dump({**state, "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
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
    """Bytes received on every interface but loopback, for the host-cost measure. A fixed list of
    names (eth0, ens3, enp1s0) found none on the laptop box and read 0 MB for download_upload."""
    total = 0
    for iface in os.listdir("/sys/class/net") if os.path.isdir("/sys/class/net") else []:
        p = f"/sys/class/net/{iface}/statistics/rx_bytes"
        if iface != "lo" and os.path.exists(p):
            with open(p) as fh:
                total += int(fh.read().strip())
    return total


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
    for i, u in enumerate(users):
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
                # Keyed by the user's place in the list and the file's name, so the two sides
                # compare without ids. Not by address: audit@ vs a fresh audit-ab1-… never matched.
                key = f"{i}/{f.get('name','')}"
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
             mail_mode: str | None = None, fresh_target: str | None = None,
             ledger: str | None = None, side_by_side: bool = False) -> dict:
    """One arm. `ledger` is this arm's own copy of the account's ledger (side-by-side arms), with
    `settings.db_path` already pointing at it; its scratch space is its own too."""
    label = arm_label(mode, mail_mode)
    env = dict(os.environ)
    env["TRANSFER_MODE"] = mode
    env.update(mail_env(mail_mode))
    env.update(ACCOUNT_ENV)
    if ledger:
        env["BITPORT_LEDGER"] = ledger
        env["SCRATCH_DIR"] = settings.scratch_dir
    env["SANDBOX_MODE"] = "true"
    env["MIGRATE_CHAT"] = "true" if "chat" in ALL_SERVICES.split(",") else "false"
    env["MIGRATE_COMMENTS"] = "true"
    env["MIGRATE_SECONDARY_CALENDARS"] = "true"

    scoped = [a for u in users_src for a in ("--user", u)]

    print(f"\n=== {mode}: emptying the target ({len(users_src)} user(s)) ===",
          flush=True)
    stage("reset target", label)
    if fresh_target:
        # reset_target.py only removes seeder data, never what a migration copied in, so a
        # second arm into the same account stacks on the first (audit@t.: 758 -> 1,486 messages).
        # A brand-new empty account per arm is a fair start and deletes nothing.
        print(f"=== {mode}: fresh empty target {fresh_target} (nothing deleted) ===", flush=True)
        from db import bulk_seed_identities
        bulk_seed_identities(MigrationDB(settings.db_path), [(users_src[0], fresh_target)])
        users_tgt = [fresh_target]
    else:
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
    stage("reset ledger", label)
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
    stage("migrate", label)
    net0 = net_bytes()
    services = services_for(mail_mode)
    rc, secs = run([PY, "main.py", *ACCOUNT_ARGS, "migrate", "--services", services, *scoped], env)
    net1 = net_bytes()
    dms = None
    if mail_mode in ("dms", "split") and len(users_src) == 1:
        print(f"=== {mode}+{mail_mode}: Google's mail import ===", flush=True)
        stage("dms import", label)
        with _DMS_LOCK:
            dms, dms_secs = dms_tail(settings, users_src, users_tgt, env)
        secs += dms_secs
        print(f"=== DMS: {dms} ===", flush=True)

    db = MigrationDB(settings.db_path)
    counts = {t: n for t, n in db.conn.execute(
        f"SELECT item_type, COUNT(*) FROM audit_log WHERE status='SUCCESS' "
        f"AND source_user IN ({ph}) GROUP BY item_type", users_src)}
    failed = db.conn.execute(
        f"SELECT COUNT(*) FROM audit_log WHERE status LIKE 'FAILED%' "
        f"AND source_user IN ({ph})", users_src).fetchone()[0]

    print(f"=== {mode}: snapshotting the target ===", flush=True)
    stage("snapshot target", label)
    tgt_snap = snapshot_drive(auth, users_tgt, "target")

    verdicts, details = {}, {}
    if mail_mode is not None:
        print(f"=== {mode}+{mail_mode}: verifying one to one ===", flush=True)
        stage("verify 1:1", label)
        # Cleared first, so an arm only ever reads rows its own check wrote: with `--services all`
        # checking nothing, every arm reported a stray backfill's mid-migration rows as its own.
        with db.write() as c:
            c.execute(f"DELETE FROM user_verification WHERE source_user IN ({ph})", users_src)
        vrc, vout = run_tail([PY, "verify_sample.py", *ACCOUNT_ARGS[:2], *scoped, "--services", VERIFY_SERVICES], env)
        db = MigrationDB(settings.db_path)
        for u in users_src:
            for svc, v, payload in db.conn.execute(
                    "SELECT service, verdict, payload FROM user_verification WHERE source_user=?", (u,)):
                key = f"{u.split('@')[0]}/{svc}"
                verdicts[key] = v
                if v not in ("IDENTICAL", "COMPLETE"):
                    details[key] = difference_summary(payload, service=svc)
        if vrc != 0 or not verdicts:
            verdicts["verify"] = "UNVERIFIED"
            details["verify"] = {"exit": vrc, "output": vout}

    return {
        "mode": mode,
        "mailMode": mail_mode,
        "targetUser": users_tgt[0] if len(users_tgt) == 1 else None,
        "verdicts": verdicts,
        "differences": details,
        "dms": dms,
        "clean": rc == 0 and not failed and (dms is None or dms == "done") and all(
            v in ("IDENTICAL", "COMPLETE") for v in verdicts.values()),
        "exit": rc,
        "seconds": round(secs, 1),
        "migrated": counts,
        "failed": failed,
        "host_bytes_in": None if side_by_side else net1 - net0,   # the interface is shared by every arm
        "timing": "side by side" if side_by_side else "alone",
        "target": {k: v for k, v in tgt_snap.items() if k != "sample"},
        "fidelity": fidelity(src_snap, tgt_snap),
    }


def mailbox_size(auth: AuthManager, user: str) -> int | None:
    """The source mailbox's message count (Gmail's own messagesTotal), for weighting an arm's
    results by how much mail it had to move. None if it could not be read."""
    try:
        return int(auth.source_gmail(user).users().getProfile(userId="me").execute()
                   .get("messagesTotal") or 0)
    except Exception:  # noqa: BLE001 - a weight we cannot read is reported as missing, not zero
        logging.getLogger(__name__).warning("could not read %s's mailbox size", user, exc_info=True)
        return None


def ledger_copy(settings: Settings, directory: str) -> str:
    """A consistent copy of the account's ledger (sqlite's own backup, safe while it is written to)
    for one side-by-side arm. The real ledger is never touched by an arm that has its own."""
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "migration.db")
    src, dst = sqlite3.connect(settings.db_path), sqlite3.connect(path)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    return path


def arm_errors(r: dict) -> dict:
    """One number to rank arms by: failed items plus every counted difference, missing, duplicate,
    extra, not-copied item and verifier error. `judged` is False when the arm cannot be ranked on
    it -- its own check never ran (UNVERIFIED, or no verdicts), or mail it owes Google's import was
    never imported -- because a low count there means nothing was looked at, not that nothing differed."""
    verdicts = r.get("verdicts") or {}
    if r.get("mailMode") and (not verdicts or "UNVERIFIED" in verdicts.values()):
        return {"errors": None, "judged": False, "why": "its 1:1 check did not run"}
    if r.get("dms") not in (None, "done"):
        return {"errors": None, "judged": False, "why": f"Google's mail import: {r['dms']}"}
    if r.get("exit") not in (None, 0):
        return {"errors": None, "judged": False, "why": f"the migration exited {r['exit']}"}
    n = int(r.get("failed") or 0) + int((r.get("fidelity") or {}).get("missing_on_target") or 0) * (not r.get("mailMode"))
    for d in (r.get("differences") or {}).values():
        n += sum(v for v in (d.get("counts") or {}).values() if isinstance(v, int))
    # Arms on different users carry different amounts of data: per 1,000 source items and per 100
    # items is what makes them comparable. An arm with no corpus (one shared user) ranks on raw counts.
    items = (r.get("corpus") or {}).get("items")
    return {"errors": n, "judged": True, "why": None,
            "errorsPer1k": round(n * 1000 / items, 2) if items else None,
            "secondsPer100": round(r["seconds"] * 100 / items, 1) if items and r.get("seconds") else None}


def rank_key(r: dict) -> tuple:
    """Fewest errors, then fastest -- weighted per item when the arm knows its corpus."""
    e = arm_errors(r)
    if not e["judged"]:
        return (1, 1e12, 1e12)
    err = e["errorsPer1k"] if e["errorsPer1k"] is not None else e["errors"]
    sec = e["secondsPer100"] if e["secondsPer100"] is not None else (r.get("seconds") or 1e12)
    return (0, err, sec)


def dms_tail(settings, users_src, users_tgt, env) -> tuple[str, float]:
    """Google's import of the mail the engine left owed, for the arm's one user: a one-row identity
    map, then dms_migrate.py --apply --until-done. Returns (outcome, seconds); the arm's time
    includes it. "skipped: ..." when the box holds no Admin console login -- the unattended
    approval cannot run without it, and that is a missing prerequisite, not a failed arm."""
    import csv
    import webui
    denv = webui._dms_env(settings.account_id)
    if not denv.get("DWD_PASSWORD"):
        return f"skipped: no Admin console login for {settings.target_admin} in {webui.DWD_ENV_FILE}", 0.0
    path = os.path.join("logs", "dms", f"ab-{os.getpid()}.csv")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(("source_email", "target_email"))
        w.writerow((users_src[0], users_tgt[0]))
    argv = [PY, "dms_migrate.py", "--apply", "--watch", "60", "--until-done", "1440", "--timeout", "200",
            "--fresh-import", "--no-tally", "--watch-interval", "120",
            "--identities", path, "--source-domain", settings.source_domain,
            "--target-admin", settings.target_admin]
    # Each status read is a fresh sign-in; every 2 minutes keeps the arm's measured time within
    # two minutes of Google's own finish without tripping sign-in challenges.
    rc, secs = run(argv, {**env, **denv, "DMS_POLL_SEC": "120"})
    return ("done" if rc == 0 else f"failed (exit {rc})"), secs


def difference_kinds(payload: dict, service: str) -> list[dict]:
    """Every difference, missing, extra, duplicate and not-copied item of one service counted by
    kind (known_issues), most first, with each kind's cause and fix -- the whole list, not the
    handful of examples kept, so "what went wrong" reads as a few causes rather than a count."""
    import known_issues
    counts: dict[str, int] = {}
    for category in ("differences", "missing", "extras", "duplicates", "notCopied", "errors"):
        for item in payload.get(category) or []:
            kind = known_issues.classify(service, item if category != "notCopied" else {"notCopied": item})
            key = kind if category in ("differences", "notCopied") or kind != "unclassified" else f"{service}.{category}"
            counts[key] = counts.get(key, 0) + 1
    out = []
    for kind, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        row = known_issues.BY_ID.get(kind)
        out.append({"kind": kind, "count": n, "title": row[3] if row else kind.replace(".", " "),
                    "cause": row[4] if row else None, "fix": row[5] if row else None,
                    "auto": bool(row and row[6])})
    return out


def difference_summary(payload, sample: int = 5, service: str = "") -> dict:
    """What a non-identical verdict was about: the counts and a few examples of each list, so
    the cause survives the next arm clearing the ledger (the first A/B lost it)."""
    try:
        p = json.loads(payload) if isinstance(payload, str) else (payload or {})
    except ValueError:
        return {"unreadable": str(payload)[:200]}
    out = {"counts": p.get("counts"), "completion": p.get("completion"), "kinds": difference_kinds(p, service)}
    for k in ("differences", "missing", "extras", "notCopied", "errors"):
        if p.get(k):
            out[k] = p[k][:sample]
    out["failedChecks"] = [c.get("key") for c in p.get("checks", [])
                           if c.get("status") not in ("IDENTICAL", "MATCH", "EMPTY", "OK")]
    return out


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
    ap.add_argument("--fresh-targets", action="store_true",
                    help="each arm migrates into a brand-new empty target account (one pinned user "
                         "only). Implied by --mail-mode: a reset cannot empty migrated mail, so a "
                         "reused account would double. Costs one licence per arm; deletes nothing.")
    ap.add_argument("--account-id", type=int,
                    help="a SaaS account's tenant pair and ledger (default: the box's own)")
    ap.add_argument("--mail-mode", action="append", choices=["engine", "split", "dms"],
                    help="also move mail, once per value, crossed with every --mode. "
                         "Without it this is the Drive-only A/B it always was.")
    ap.add_argument("--sequential", action="store_true",
                    help="one arm at a time on the account's own ledger (the original way). The "
                         "default runs the arms side by side, each on its own copy of the ledger, "
                         "then re-times the leading arm(s) alone: side-by-side times share one box, "
                         "one Google quota and one network card, so they rank errors, not speed.")
    ap.add_argument("--include-slow", action="store_true",
                    help=f"also run {arm_label(*SLOW_ARM)}, skipped by default: it took ~2.5x "
                         "the next slowest arm and its differences were download_upload's own.")
    args = ap.parse_args(argv)

    settings = Settings(account_id=args.account_id)
    if args.account_id is not None:
        ACCOUNT_ARGS[:] = ["--account-id", str(args.account_id)]
        ACCOUNT_ENV.update(TARGET_DOMAIN=settings.target_domain, TARGET_ADMIN=settings.target_admin,
                           TARGET_SA_KEY=settings.target_sa_key, MIGRATION_DB=settings.db_path)
    auth = AuthManager(settings)
    db = MigrationDB(settings.db_path)
    fresh = args.fresh_targets or bool(args.mail_mode)
    mapped = {r["source_email"].lower(): r["target_email"] for r in db.all_identities()
              if r["entity_type"] == "user"}
    if args.user:
        missing = sorted({u.lower() for u in args.user} - set(mapped))
        if missing and not fresh:
            print(f"not in identity_map: {missing}")
            return 1
        # A fresh arm maps its user to a brand-new target account, so an unmapped user is fine.
        users_src = list(dict.fromkeys(args.user))
    else:
        users_src = list(mapped)
    users_tgt = [mapped.get(u.lower()) or f"{u.partition('@')[0]}@{settings.target_domain}"
                 for u in users_src]
    if not users_src:
        print("identity_map is empty.")
        return 1

    arms = [(m, mm) for m in (args.mode or ["server_side", "download_upload"])
            for mm in (args.mail_mode or [None])]
    skipped = []
    if fresh and not args.include_slow and SLOW_ARM in arms and len(arms) > 1:
        arms.remove(SLOW_ARM)
        skipped.append(arm_label(*SLOW_ARM))
        print(f"=== skipping {skipped[0]} (slow; --include-slow runs it) ===", flush=True)
    # One user per arm (as many --user as arms): every arm reads its own mailbox, so Gmail's
    # per-user limit never pits one arm against another (2026-10-31: four arms on one user ran it
    # dry -- 729 messages unverifiable). Users differ in size, so results are weighted per item.
    per_arm = fresh and len(users_src) == len(arms) > 1
    if fresh and not per_arm and len(users_src) != 1:
        print(f"--fresh-targets/--mail-mode need one --user, or one per arm ({len(arms)}): "
              "a fair comparison pins each arm's data, and each arm needs its own new account.")
        return 1
    arm_users = ([(users_src[i], users_tgt[i]) for i in range(len(arms))] if per_arm
                 else [(users_src[0], users_tgt[0])] * len(arms))
    snaps = {}
    for u in dict.fromkeys(u for u, _ in arm_users):
        print(f"=== snapshotting the source: {u} ===", flush=True)
        snaps[u] = snapshot_drive(auth, [u], "source")
        snaps[u]["messages"] = mailbox_size(auth, u)
        print(f"    {snaps[u]['files']} files, {snaps[u]['bytes']/1e9:.2f} GB, "
              f"{snaps[u]['messages']} messages", flush=True)
    src_snap = snaps[arm_users[0][0]]
    original_target = users_tgt[0]
    side_by_side = fresh and not args.sequential and len(arms) > 1
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    stamp = started[5:7] + started[8:10] + started[11:13] + started[14:16]
    durations: list[float] = []
    results: list = [None] * len(arms)
    corpus = {"files": sum(x["files"] for x in snaps.values()), "bytes": sum(x["bytes"] for x in snaps.values()),
              "messages": sum(x["messages"] or 0 for x in snaps.values())}

    def save() -> None:
        with _LOCK:
            with open(args.report + ".json", "w") as fh:
                json.dump({"source": {k: v for k, v in src_snap.items() if k != "sample"},
                           "perArmUsers": per_arm,
                           "skipped": skipped, "sideBySide": side_by_side,
                           "runs": [r for r in results if r]}, fh, indent=2)

    def arm(i: int, mode: str, mail_mode: str | None, together: bool, tag: str) -> dict:
        label = arm_label(mode, mail_mode)
        with _LOCK:
            _ARMS[label] = {"arm": label, "mode": mode, "mailMode": mail_mode, "stage": "starting",
                            "startedAt": time.time(), "user": arm_users[i][0]}
        write_progress()
        user, target = arm_users[i]
        snap = snaps[user]
        try:
            fresh_target = f"{target.partition('@')[0]}-ab{tag}-{stamp}@" \
                           f"{target.partition('@')[2]}" if fresh else None
            st, au, ledger = settings, auth, None
            if together:        # its own ledger copy and scratch, its own Google clients
                ledger = ledger_copy(settings, os.path.join(HERE, "data", "ab", stamp, f"arm{tag}"))
                st = copy.copy(settings)
                st.db_path = ledger
                st.scratch_dir = os.path.join(os.path.dirname(ledger), "scratch")
                au = AuthManager(st)
            r = one_mode(mode, st, au, [user], [target], snap, mail_mode, fresh_target,
                         ledger=ledger, side_by_side=side_by_side and together)
            items = snap["files"] + (snap["messages"] or 0 if mail_mode else 0)
            r["corpus"] = {"user": user, "files": snap["files"], "bytes": snap["bytes"],
                           "messages": snap["messages"], "items": items}
        finally:
            with _LOCK:
                _ARMS.pop(label, None)
        return r

    def finished(r: dict) -> None:
        with _LOCK:
            durations.append(r["seconds"])
        save()
        write_progress(done=len(durations), durations=durations)

    try:
        write_progress(running=True, startedAt=started, total=len(arms), done=0,
                       accountId=args.account_id, armStartedAt=time.time(), durations=durations,
                       users=users_src, corpus=corpus, parallel=side_by_side, skipped=skipped,
                       phase="side by side" if side_by_side else "one at a time")
        if side_by_side:
            def go(i):
                results[i] = arm(i, *arms[i], True, str(i + 1))
                finished(results[i])
            with ThreadPoolExecutor(len(arms)) as ex:
                for f in [ex.submit(go, i) for i in range(len(arms))]:
                    f.result()
            # Errors are comparable across arms that shared the box; speed is not. Re-time alone
            # whichever arms hold the lowest error count (at most three, fastest first).
            judged = [(rank_key(r)[1], rank_key(r)[2], i) for i, r in enumerate(results)
                      if arm_errors(r)["judged"] and r.get("mailMode")]
            lowest = min((e for e, _, _ in judged), default=None)
            lead = sorted((sec, i) for e, sec, i in judged if e == lowest)[:3]
            write_progress(total=len(arms) + len(lead), phase="re-timing the leading arm alone")
            for k, (_, i) in enumerate(lead):
                again = arm(i, *arms[i], False, f"{i + 1}t")
                again["contendedSeconds"] = results[i]["seconds"]
                results[i] = again
                durations.append(again["seconds"])
                save()
                write_progress(done=len(arms) + k + 1, durations=durations)
        else:
            for i, (mode, mail_mode) in enumerate(arms):
                write_progress(done=i, armStartedAt=time.time())
                results[i] = arm(i, mode, mail_mode, False, str(i + 1))
                finished(results[i])
    except BaseException as exc:      # the page must say it failed, and why, not sit at "0 of N"
        with _LOCK:
            _ARMS.clear()
        write_progress(running=False, current=None,
                       error=f"{type(exc).__name__}: {str(exc)[:600]}",
                       finishedAt=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        raise
    finally:
        if fresh and not side_by_side and not per_arm and users_src[0].lower() in mapped:
            # the pinned user goes back to its own target account
            from db import bulk_seed_identities
            bulk_seed_identities(MigrationDB(settings.db_path), [(users_src[0], original_target)])
    results = [r for r in results if r]
    write_progress(running=False, total=len(durations), done=len(durations), current=None,
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
    row("bytes through host", lambda r: "n/a" if r["host_bytes_in"] is None else f"{r['host_bytes_in']/1e6:.0f} MB")
    row("timed", lambda r: r.get("timing", "alone"))
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
