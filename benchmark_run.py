"""
benchmark_run.py
================
One command that wipes, re-migrates, measures, and *judges* a Drive run —
with nobody watching it.

    python3 benchmark_run.py --label B5 --confirm-domain a.example.com --yes

Why this exists
---------------
Every benchmark so far was driven by an operator (or an agent) stitching
together reset_target.py, reset_drive_ledger.py, main.py, acl_audit.py and a
pile of ad-hoc SQL, then eyeballing the result. That process produced a
false pass: **B4 reported "0 file failures" while 20,714 of 20,714 ACL grant
creates were failing with a 404**, because the speed numbers and the fidelity
numbers were gathered by different people at different times, and nothing
compared them.

So this script's contract is: a run is not "fast" unless it is also correct.
Fidelity gates are evaluated in the same pass as the timings, and a run that
loses grants **exits non-zero**, however quick it was.

What it will not do
-------------------
It will not touch the source tenant, and it refuses to start if a migration
is already running (two engines against one ledger is not a benchmark, it is
a corruption). It takes a ledger backup before wiping anything.

Output
------
`benchmarks/<label>-<timestamp>.json`  — machine-readable, for diffing runs
`benchmarks/<label>-<timestamp>.md`    — the human summary
stdout                                 — progress, then a PASS/FAIL verdict
exit code                              — 0 pass, 1 fidelity failure, 2 aborted
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
OUT_DIR = os.path.join(HERE, "benchmarks")

# Accounts that fail on every run for reasons outside the migration (deleted
# or suspended source/target accounts). They are excluded from fidelity gates
# so a known-dead account cannot fail an otherwise clean benchmark -- but
# they are still reported, so "known dead" can never quietly become "silently
# ignored".
DEAD_ACCOUNTS_ENV = "BENCH_DEAD_ACCOUNTS"


def sh(argv: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=HERE, capture_output=True, text=True, **kw)


def benchmark_scope(account_id: int | None, confirm_domain: str):
    """Bind a benchmark to the selected account before any child is started."""
    import accounts_auth
    import account_isolation
    import control_plane_db as cpdb
    from config import Settings

    if account_id is not None and os.getenv("BITPORT_LEDGER"):
        raise ValueError("A selected-account benchmark cannot use a ledger override")
    settings = Settings(account_id=account_id)
    source = (settings.source_domain or "").strip().lower()
    target = (settings.target_domain or "").strip().lower()
    typed = confirm_domain.strip().lower()
    if not target:
        raise ValueError("TARGET_DOMAIN is not configured")
    if typed != target:
        extra = " -- that is the SOURCE domain" if typed and typed == source else ""
        raise ValueError(f"{typed!r} does not match the target domain {target!r}{extra}")
    if not source or source == target:
        raise ValueError("The benchmark requires distinct source and target domains")
    if account_id is None:
        return settings, None

    account = accounts_auth.get_account(account_id) or {}
    legacy = account_id == 1 and account.get("plan") == "legacy"
    if not legacy and settings.auth_mode != "key":
        # The reset and audit children use bare Settings. OAuth tokens and
        # impersonation principals come from the process, outside the account registry.
        raise ValueError("This benchmark requires the selected account's service account keys")
    control = os.path.realpath(cpdb._db_path())
    ledger = os.path.realpath(settings.db_path)
    if not legacy and ledger == control:
        raise ValueError("The selected account requires a separate migration ledger")
    for side, domain in (("source", source), ("target", target)):
        config = accounts_auth.get_tenant_config(account_id, side)
        if not config or (
            not legacy and not all(config.get(key) for key in ("domain", "admin_email", "sa_key_path", "db_path"))
        ):
            raise ValueError(f"The selected account has no complete {side} configuration")
        if config.get("db_path") and os.path.realpath(os.path.join(HERE, config["db_path"])) != ledger:
            raise ValueError("The selected account's source and target do not share one ledger")
        admin = getattr(settings, f"{side}_admin").strip().lower()
        if "@" not in admin or admin.rsplit("@", 1)[-1] != domain:
            raise ValueError(f"The selected account's {side} administrator belongs to another domain")

    env = dict(os.environ)
    env.update(CONTROL_PLANE_DB=control, MIGRATION_DB=ledger,
               SOURCE_DOMAIN=settings.source_domain, TARGET_DOMAIN=settings.target_domain,
               SOURCE_ADMIN=settings.source_admin, TARGET_ADMIN=settings.target_admin,
               SOURCE_SA_KEY=settings.source_sa_key, TARGET_SA_KEY=settings.target_sa_key)
    why = account_isolation.refusal(account_id, env)
    if why:
        raise ValueError(why)
    return settings, env


def db_path() -> str:
    from config import Settings

    return Settings().db_path


def query(sql: str, args: tuple = (), *, ledger_path: str | None = None) -> list[sqlite3.Row]:
    conn = sqlite3.connect(f"file:{ledger_path or db_path()}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


# ----------------------------------------------------------------------
# Config fingerprint -- a benchmark without one cannot be compared to
# anything, because you cannot prove what it measured.
# ----------------------------------------------------------------------
def fingerprint(settings=None) -> dict:
    from config import Settings

    s = settings if settings is not None else Settings()
    commit = sh(["git", "rev-parse", "--short", "HEAD"]).stdout.strip()
    dirty = bool(sh(["git", "status", "--porcelain"]).stdout.strip())
    if not commit:
        # The place a benchmark actually runs -- a deployed VPS -- is an
        # rsync of the tree, not a git checkout, so `git rev-parse` finds
        # nothing and every run there recorded "unknown". A benchmark whose
        # commit is unknown cannot be compared to another one, which defeats
        # the point of recording a fingerprint at all. sync_vps.sh writes
        # DEPLOYED_COMMIT on the way out; fall back to it.
        try:
            with open(os.path.join(HERE, "DEPLOYED_COMMIT")) as fh:
                commit = fh.read().strip() or "unknown"
        except OSError:
            commit = "unknown"
    return {
        "commit": commit,
        # A dirty tree means the code that ran is not the code in git, which
        # makes the run unreproducible. Recorded rather than blocked.
        "workingTreeDirty": dirty,
        "transferMode": s.transfer_mode,
        "userWorkers": s.user_workers,
        "driveFileWorkers": getattr(s, "drive_file_workers", 1),
        "driveWriteQps": getattr(s, "drive_write_qps", None),
        # Drive reads have their own budget now; per_user_qps still paces the
        # other engines, so both belong in a run's record.
        "driveReadQps": getattr(s, "drive_read_qps", None),
        "perUserQps": s.per_user_qps,
        "aclBatchSize": getattr(s, "acl_batch_size", None),
        "verifyServerSideMd5": getattr(s, "verify_server_side_md5", None),
        "migrateExternalShares": getattr(s, "migrate_external_shares", None),
        "python": sys.version.split()[0],
    }


# ----------------------------------------------------------------------
# Measurement
# ----------------------------------------------------------------------
def per_user_stats(since_iso: str | None = None, *, ledger_path: str | None = None) -> dict:
    """Per-user totals, scoped to this run.

    `reset_drive_ledger.py` clears Drive rows but leaves everything else in
    audit_log, so an unscoped query mixes in every previous run: B5 came
    back warning that 10 users had failed items when exactly one had, the
    other nine carrying nothing but B4's 20,714 stale ACL failures from the
    day before. The Drive numbers happened to be clean because the ledger
    reset had removed the old ones -- that is luck, not a guarantee, and it
    would not survive `--skip-wipe`.
    """
    where = "WHERE timestamp >= ?" if since_iso else ""
    args = (since_iso,) if since_iso else ()
    rows = query(f"""
        SELECT source_user,
               SUM(status='SUCCESS')                    AS ok,
               SUM(status='FAILED')                     AS failed,
               SUM(status LIKE 'SKIPPED%')              AS skipped,
               SUM(item_type='file'  AND status='SUCCESS') AS files,
               SUM(item_type='acl'   AND status='FAILED')  AS acl_failed,
               SUM(item_type='acl'
                   AND status='SKIPPED_UNMAPPED_IDENTITY') AS acl_unmapped,
               SUM(bytes_moved)                         AS bytes
        FROM audit_log {where} GROUP BY source_user ORDER BY source_user""",
                 args, ledger_path=ledger_path)
    return {r["source_user"]: dict(r) for r in rows}


def checksum_failures(*, ledger_path: str | None = None) -> int:
    """MD5 mismatches specifically -- distinct from transport failures. A
    non-zero count here means bytes arrived corrupted, which is the one
    failure mode that must never be traded for speed."""
    return query("SELECT COUNT(*) n FROM audit_log WHERE status='FAILED' "
                 "AND error_message LIKE '%checksum mismatch%'", ledger_path=ledger_path)[0]["n"]


def rate_limit_hits(*, ledger_path: str | None = None) -> int:
    """429s. The signal that drive_write_qps is not holding -- the whole
    safety argument for raising drive_file_workers rests on this staying 0."""
    return query("SELECT COUNT(*) n FROM audit_log WHERE "
                 "error_message LIKE '%rateLimitExceeded%' OR "
                 "error_message LIKE '%429%'", ledger_path=ledger_path)[0]["n"]


def run_acl_audit(label: str, *, env: dict | None = None) -> dict:
    """
    The gate that B4 needed and did not have.

    Run as a subprocess so its own guards apply, and read the JSON rather
    than the printed summary. Note it exits non-zero when it finds problems
    -- that is a finding, not a crash, so returncode is not treated as
    failure here.
    """
    out = os.path.join(OUT_DIR, f"{label}-acl.json")
    sh([PY, "acl_audit.py", "--json", out], timeout=3600, env=env)
    if not os.path.isfile(out):
        return {"error": "acl_audit produced no JSON -- fidelity unverified"}
    with open(out, encoding="utf-8") as fh:
        data = json.load(fh)
    t = data.get("totals", {})
    src = t.get("grants_source", 0)
    return {
        "grantsSource": src,
        "grantsMatched": t.get("grants_matched", 0),
        "missingGrants": t.get("missing_grants", 0),
        "extraGrants": t.get("extra_grants", 0),
        "missingFiles": t.get("missing_files", 0),
        "filesCompared": t.get("compared", 0),
        "exactFiles": t.get("exact", 0),
        "fidelityPct": round(t.get("grants_matched", 0) / src * 100, 2) if src else None,
        "failedUsers": data.get("failed_users", []),
        "detailPath": out,
    }


# ----------------------------------------------------------------------
# Verdict
# ----------------------------------------------------------------------
def judge(result: dict, dead: set[str]) -> tuple[bool, list[str]]:
    """
    Gates, in the order they have actually caught real bugs on this project.

    Each one exists because it fired: the ACL gate would have caught B4's
    404 storm, the extra-grant gate caught link_flip leaving 93 target files
    world-readable, and the checksum gate is the only thing standing between
    "fast" and "fast and wrong".
    """
    fails: list[str] = []
    warns: list[str] = []
    acl = result["acl"]

    # 0. Did the run happen at all?
    #
    # Every gate below is a statement about what landed on the target, so a
    # run that migrated nothing satisfies all of them vacuously. B5's first
    # attempt proved it: the engine died after 17 seconds with
    # `free(): invalid next size (normal)` (SIGABRT, rc=-6) having copied 0
    # files, and this function returned PASS -- a green benchmark for a
    # crashed migration. A judge that cannot fail an empty run is not a
    # safety net, so these two come before anything about fidelity.
    rc = result.get("migrateReturnCode")
    if rc:
        detail = f"signal {-rc}" if rc < 0 else f"exit {rc}"
        fails.append(f"MIGRATE CRASHED: {detail} -- see {result.get('migrateLog')}")
    if not result.get("totalFiles"):
        fails.append("NOTHING MIGRATED: 0 files copied, so every fidelity "
                     "check below passed vacuously")

    if acl.get("error"):
        fails.append(f"FIDELITY UNVERIFIED: {acl['error']}")
    else:
        # 1. Over-sharing. Any public grant the source did not have is a
        #    security regression, and one is too many.
        if acl.get("extraGrants", 0) > 0:
            fails.append(
                f"SECURITY: {acl['extraGrants']} grant(s) exist on the target "
                f"that the source did not have (link_flip left 93 files "
                f"world-readable this way)")
        # 2. Grant loss. B4 scored 0 here while reporting a clean run.
        fid = acl.get("fidelityPct")
        if acl.get("missingGrants", 0) > 0 or (fid is not None and fid < 100.0):
            fails.append(f"FIDELITY: only {fid}% of source grants preserved "
                         f"({acl['missingGrants']} missing)")
        if acl.get("missingFiles", 0) > 0:
            fails.append(f"FIDELITY: {acl['missingFiles']} file(s) mapped but "
                         f"absent on the target")

    # 3. Corruption. Never acceptable at any speed.
    if result["checksumFailures"] > 0:
        fails.append(f"CORRUPTION: {result['checksumFailures']} MD5 mismatch(es)")

    # 4. Unexpected user failures, excluding the known-dead accounts.
    unexpected = [u for u, s in result["users"].items()
                  if u not in dead and s["failed"] > 0]
    if unexpected:
        warns.append(f"{len(unexpected)} user(s) have failed items: "
                     f"{', '.join(sorted(unexpected)[:5])}")

    # 5. Rate limiting. Not a failure -- the engine retries -- but it means
    #    the write ceiling is being pushed and drive_file_workers is too high.
    if result["rateLimitHits"] > 0:
        warns.append(f"{result['rateLimitHits']} rate-limit error(s): "
                     f"lower DRIVE_FILE_WORKERS or DRIVE_WRITE_QPS")

    result["warnings"] = warns
    result["failures"] = fails
    return (not fails), fails


def render_markdown(r: dict) -> str:
    fp, acl = r["config"], r["acl"]
    ok = "PASS" if r["passed"] else "FAIL"
    lines = [
        f"# Benchmark {r['label']} — {ok}",
        "",
        f"- started `{r['startedAt']}` · elapsed **{r['elapsedS']:.0f}s "
        f"({r['elapsedS']/3600:.2f}h)**",
        f"- commit `{fp['commit']}`{' **(dirty tree)**' if fp['workingTreeDirty'] else ''}",
        "",
        "## Config",
        "",
        "| knob | value |",
        "|---|---|",
    ]
    for k, v in fp.items():
        lines.append(f"| {k} | `{v}` |")
    lines += [
        "",
        "## Performance",
        "",
        "| metric | value |",
        "|---|---|",
        f"| files copied | {r['totalFiles']:,} |",
        f"| **seconds/file** | **{r['secPerFile']:.2f}** |",
        f"| files/sec | {r['filesPerSec']:.2f} |",
        f"| bytes moved | {r['totalBytes']:,} |",
        f"| slowest user (critical path) | {r['slowestUser']} |",
        "",
        "## Fidelity",
        "",
        "| check | value | gate |",
        "|---|---|---|",
        f"| grants preserved | {acl.get('grantsMatched', 0):,} / "
        f"{acl.get('grantsSource', 0):,} ({acl.get('fidelityPct')}%) | 100% |",
        f"| **extra grants (over-share)** | **{acl.get('extraGrants', 0)}** | **0** |",
        f"| missing files | {acl.get('missingFiles', 0)} | 0 |",
        f"| MD5 mismatches | {r['checksumFailures']} | 0 |",
        f"| rate-limit (429) hits | {r['rateLimitHits']} | 0 (warn) |",
        "",
    ]
    if r["failures"]:
        lines += ["## Failures", ""] + [f"- {f}" for f in r["failures"]] + [""]
    if r["warnings"]:
        lines += ["## Warnings", ""] + [f"- {w}" for w in r["warnings"]] + [""]
    lines += ["## Per user", "",
              "| user | files | failed | acl_failed | skipped |", "|---|---|---|---|---|"]
    for u, s in sorted(r["users"].items()):
        lines.append(f"| {u} | {s['files'] or 0} | {s['failed'] or 0} | "
                     f"{s['acl_failed'] or 0} | {s['skipped'] or 0} |")
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[3])
    ap.add_argument("--label", required=True, help="benchmark id, e.g. B5")
    ap.add_argument("--confirm-domain", required=True,
                    help="TARGET domain, typed back as a wipe confirmation")
    ap.add_argument("--services", default="drive",
                    help="keep this identical across runs you intend to compare")
    ap.add_argument("--yes", action="store_true", help="non-interactive")
    ap.add_argument("--skip-wipe", action="store_true",
                    help="still migrates data; skips the target wipe and ledger reset")
    ap.add_argument("--account-id", type=int, default=None, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    try:
        settings, env = benchmark_scope(args.account_id, args.confirm_domain)
        ledger_path = env["MIGRATION_DB"] if env is not None else settings.db_path
        if env is not None:
            import control_plane_db as cpdb
            from db import _ledger_pair_of

            with cpdb.ro(ledger_path) as conn:
                pair = _ledger_pair_of(conn)
            source = settings.source_domain.strip().lower()
            target = settings.target_domain.strip().lower()
            if pair and (pair[0] != source or pair[1] not in (None, target)):
                raise ValueError("The selected account's ledger belongs to a different tenant pair")
    except (ValueError, sqlite3.Error) as exc:
        print(f"ABORT: {exc}", file=sys.stderr)
        return 2

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    dead = {a.strip() for a in os.getenv(DEAD_ACCOUNTS_ENV, "").split(",") if a.strip()}

    # Refuse to race another engine. Two migrations against one ledger is
    # not a slow benchmark, it is a corrupted one.
    ps = sh(["ps", "-eo", "args="]).stdout
    if any("main.py" in ln and "migrate" in ln for ln in ps.splitlines()):
        print("ABORT: a migration is already running.", file=sys.stderr)
        return 2

    fp = fingerprint(settings)
    print(f"== {args.label} == commit {fp['commit']} "
          f"mode={fp['transferMode']} fileWorkers={fp['driveFileWorkers']} "
          f"writeQps={fp['driveWriteQps']}")
    if fp["workingTreeDirty"]:
        print("   WARNING: working tree is dirty; this run is not reproducible.")

    if not args.skip_wipe:
        backup = f"{ledger_path}.bak_{args.label}_{stamp}"
        shutil.copy2(ledger_path, backup)
        print(f"-- ledger backed up to {backup}")

        print("-- wiping target (drive only)")
        r = sh([PY, "reset_target.py", "--confirm-domain", settings.target_domain,
                "--yes", "--services", args.services], timeout=7200, env=env)
        if r.returncode != 0:
            print(r.stdout[-2000:], r.stderr[-2000:], file=sys.stderr)
            return 2

        # Wiping target FILES does not reset the LEDGER. Skip this and every
        # user is marked DONE, migrate skips all of them, and the benchmark
        # measures an empty no-op -- which has already happened once.
        print("-- resetting drive ledger (otherwise every user is skipped)")
        r = sh([PY, "reset_drive_ledger.py", "--confirm-domain", settings.source_domain, "--yes"],
               timeout=600, env=env)
        if r.returncode != 0:
            print(r.stdout[-2000:], r.stderr[-2000:], file=sys.stderr)
            return 2

    print(f"-- migrating (services={args.services}) …")
    t0 = time.monotonic()
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    migrate = [PY, "main.py"]
    if args.account_id is not None:
        migrate += ["--account-id", str(args.account_id)]
    mig = sh(migrate + ["migrate", "--services", args.services], timeout=86400, env=env)
    elapsed = time.monotonic() - t0
    log_path = os.path.join(OUT_DIR, f"{args.label}-{stamp}-migrate.log")
    with open(log_path, "w", encoding="utf-8") as fh:
        fh.write(mig.stdout + "\n" + mig.stderr)
    print(f"-- migrate rc={mig.returncode} in {elapsed:.0f}s (log: {log_path})")

    print("-- auditing ACLs (the gate B4 did not have) …")
    acl = run_acl_audit(f"{args.label}-{stamp}", env=env)

    # `started_at` is stamped immediately before migrate runs, so this is
    # exactly this run's window.
    users = per_user_stats(since_iso=started_at, ledger_path=ledger_path)
    total_files = sum(s["files"] or 0 for s in users.values())
    total_bytes = sum(s["bytes"] or 0 for s in users.values())
    slowest = max(users.items(), key=lambda kv: kv[1]["files"] or 0,
                  default=("n/a", {}))[0]

    result = {
        "label": args.label, "startedAt": started_at, "elapsedS": round(elapsed, 1),
        "migrateReturnCode": mig.returncode, "config": fp,
        "services": args.services,
        "totalFiles": total_files, "totalBytes": total_bytes,
        "secPerFile": round(elapsed / total_files, 3) if total_files else 0,
        "filesPerSec": round(total_files / elapsed, 3) if elapsed else 0,
        "slowestUser": slowest,
        "checksumFailures": checksum_failures(ledger_path=ledger_path),
        "rateLimitHits": rate_limit_hits(ledger_path=ledger_path),
        "acl": acl, "users": users, "deadAccountsExcluded": sorted(dead),
        "migrateLog": log_path,
    }
    passed, fails = judge(result, dead)
    result["passed"] = passed

    base = os.path.join(OUT_DIR, f"{args.label}-{stamp}")
    with open(base + ".json", "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, default=str)
    with open(base + ".md", "w", encoding="utf-8") as fh:
        fh.write(render_markdown(result))

    print("\n" + "=" * 62)
    print(f"  {args.label}: {'PASS' if passed else 'FAIL'}   "
          f"{elapsed:.0f}s  ·  {result['secPerFile']:.2f} s/file  ·  "
          f"{total_files:,} files")
    if acl.get("fidelityPct") is not None:
        print(f"  ACL fidelity {acl['fidelityPct']}%  ·  "
              f"extra grants {acl.get('extraGrants', 0)}  ·  "
              f"md5 fails {result['checksumFailures']}")
    for f in fails:
        print(f"  FAIL  {f}")
    for w in result["warnings"]:
        print(f"  warn  {w}")
    print(f"  report: {base}.md")
    print("=" * 62)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
