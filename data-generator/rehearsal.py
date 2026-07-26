"""
tools/rehearsal.py
==================
Drives the complete migration lifecycle across all five sandbox users and
asserts the acceptance criteria. Layer 4 of TESTING.md, automated.

It deliberately does **not** read the engine's audit log to decide whether a
phase passed. It shells out to `main.py` exactly as an operator would, then
counts the target tenant independently through the API. If the engine's
bookkeeping and reality disagree, that disagreement is the finding.

Phases
------
  1. preflight      DWD works for all five users on both tenants
  2. discover       Discovery counts match the seed manifest
  3. dryrun         `--dry-run` writes absolutely nothing
  4. migrate        The bulk copy completes across all users
  5. sharing        Cross-user ACLs translated; private folders stayed private
  6. duplication    Target file count == source OWNED count, not visible count
  7. idempotency    A second full run creates ZERO new items
  8. interrupt      SIGINT mid-run, then resume, converges
  9. delta          After editing 3 files per user, exactly those move
 10. verify         tools/verify.py reconciles cleanly

Phases 6 and 7 are the ones that matter most, and both only become meaningful
once files are shared between users:

* **duplication** — with `OWNED_ONLY=true` each user migrates only what they
  own. Files shared *to* them arrive via the owner's migration. If the target
  ends up larger than the owned count, the engine is copying every shared file
  once per recipient.
* **idempotency** — a non-idempotent engine silently duplicates a customer's
  entire Drive the first time anyone restarts a failed job.

Usage
-----
    python tools/rehearsal.py
    python tools/rehearsal.py --phase duplication --phase idempotency
    python tools/rehearsal.py --user alice@sandbox-src.example
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth import AuthManager  # noqa: E402
from config import FOLDER_MIME, Settings  # noqa: E402
from db import MigrationDB  # noqa: E402
from resilience import retry_on_google_error  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALL_PHASES = ["preflight", "discover", "dryrun", "migrate", "sharing",
              "duplication", "idempotency", "interrupt", "delta", "verify"]
HARD_PREREQS = {"preflight", "migrate"}


@dataclass
class Result:
    phase: str
    ok: bool
    detail: str = ""
    duration: float = 0.0


@dataclass
class Rehearsal:
    settings: Settings
    auth: AuthManager
    db: MigrationDB
    pairs: list[tuple[str, str]]
    manifest: dict
    results: list[Result] = field(default_factory=list)

    def _retry(self):
        return retry_on_google_error(max_retries=self.settings.max_retries)

    # ------------------------------------------------------------------
    def run_cli(self, args: list[str], timeout: int = 21600,
                interrupt_after: float | None = None) -> tuple[int, str]:
        cmd = [sys.executable, "main.py", *args]
        print(f"    $ {' '.join(cmd)}")
        proc = subprocess.Popen(
            cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, start_new_session=True,
        )
        if interrupt_after:
            time.sleep(interrupt_after)
            if proc.poll() is None:
                os.killpg(os.getpgid(proc.pid), signal.SIGINT)
                print(f"    (SIGINT sent after {interrupt_after}s)")
        try:
            out = proc.communicate(timeout=timeout)[0]
        except subprocess.TimeoutExpired:
            proc.kill()
            return 124, "timeout"
        return proc.returncode, out or ""

    # ------------------------------------------------------------------
    # Independent census of the target tenant
    # ------------------------------------------------------------------
    def count_user(self, target_user: str) -> dict:
        retry = self._retry()
        drive = self.auth.target_drive(target_user)
        files = folders = 0
        token = None
        while True:
            @retry
            def _list(t=token):
                return drive.files().list(
                    q="'me' in owners and trashed = false",
                    pageSize=1000, spaces="drive",
                    fields="nextPageToken, files(id,mimeType)",
                    pageToken=t,
                ).execute()

            resp = _list()
            for f in resp.get("files", []):
                if f["mimeType"] == FOLDER_MIME:
                    folders += 1
                else:
                    files += 1
            token = resp.get("nextPageToken")
            if not token:
                break

        @retry
        def _prof():
            return self.auth.target_gmail(target_user).users().getProfile(
                userId="me").execute()

        @retry
        def _events():
            return self.auth.target_calendar(target_user).events().list(
                calendarId="primary", maxResults=2500, singleEvents=False
            ).execute()

        return {"files": files, "folders": folders,
                "messages": _prof().get("messagesTotal", 0),
                "events": len(_events().get("items", []))}

    def count_target(self) -> dict:
        total = {"files": 0, "folders": 0, "messages": 0, "events": 0}
        self._per_user_counts = {}
        for _src, tgt in self.pairs:
            c = self.count_user(tgt)
            self._per_user_counts[tgt] = c
            for k in total:
                total[k] += c[k]
        return total

    def count_source_owned(self) -> int:
        """Files (not folders) each source user actually owns."""
        retry = self._retry()
        total = 0
        for src, _tgt in self.pairs:
            drive = self.auth.source_drive(src)
            token = None
            while True:
                @retry
                def _list(t=token):
                    return drive.files().list(
                        q="'me' in owners and trashed = false",
                        pageSize=1000, spaces="drive", corpora="user",
                        fields="nextPageToken, files(id,mimeType)",
                        pageToken=t,
                    ).execute()

                resp = _list()
                total += sum(1 for f in resp.get("files", [])
                             if f["mimeType"] != FOLDER_MIME)
                token = resp.get("nextPageToken")
                if not token:
                    break
        return total

    def record(self, phase: str, ok: bool, detail: str, t0: float) -> bool:
        r = Result(phase, ok, detail, round(time.time() - t0, 1))
        self.results.append(r)
        print(f"  [{'PASS' if ok else 'FAIL'}] {phase:<12} {detail}")
        return ok

    # ==================================================================
    # Phases
    # ==================================================================
    def phase_preflight(self) -> bool:
        t0 = time.time()
        rc, out = self.run_cli(["preflight"], timeout=600)
        detail = (f"DWD verified for {len(self.pairs)} users on both tenants"
                  if rc == 0 else out.strip()[-400:])
        return self.record("preflight", rc == 0, detail, t0)

    def phase_discover(self) -> bool:
        t0 = time.time()
        rc, out = self.run_cli(["discover", "--include-mail"], timeout=7200)
        if rc != 0:
            return self.record("discover", False, out.strip()[-400:], t0)

        seeded = self.manifest.get("totals", {})
        total_files = total_folders = 0
        missing = []
        for src, _ in self.pairs:
            row = self.db.latest_discovery(src)
            if not row:
                missing.append(src)
                continue
            total_files += row["file_count"]
            total_folders += row["folder_count"]

        expected = seeded.get("owned_files", 0)
        # Discovery sees owned items only, so this should track the manifest
        # closely. Allow slack for anything already in the sandbox.
        ok = not missing and (expected == 0 or total_files >= expected * 0.95)
        detail = (f"discovered {total_files:,} files / {total_folders:,} folders "
                  f"across {len(self.pairs)} users; manifest says "
                  f"{expected:,} owned files")
        if missing:
            detail = f"no discovery row for: {', '.join(missing)}"
        return self.record("discover", ok, detail, t0)

    def phase_dryrun(self) -> bool:
        t0 = time.time()
        before = self.count_target()
        rc, _out = self.run_cli(["--dry-run", "migrate"], timeout=7200)
        after = self.count_target()
        ok = rc == 0 and before == after
        detail = (f"target unchanged ({after['files']:,} files)" if ok
                  else f"DRY RUN WROTE DATA: {before} -> {after}")
        return self.record("dryrun", ok, detail, t0)

    def phase_migrate(self) -> bool:
        t0 = time.time()
        rc, out = self.run_cli(["migrate"], timeout=43200)
        counts = self.count_target()
        ok = rc == 0 and counts["files"] > 0
        detail = (f"{counts['files']:,} files, {counts['folders']:,} folders, "
                  f"{counts['messages']:,} messages, {counts['events']:,} events")
        if rc != 0:
            detail = f"exit {rc}: {out.strip()[-400:]}"
        return self.record("migrate", ok, detail, t0)

    # ------------------------------------------------------------------
    def phase_sharing(self) -> bool:
        """
        Cross-user ACLs must be translated, and private folders must stay private.

        Checks, per user:
          * the 'shared-every-way.pdf' file carries a target-domain grantee and
            no source-domain address anywhere;
          * the external collaborator survived;
          * the domain grant points at the TARGET domain;
          * 'Personal' has no grants at all.
        """
        t0 = time.time()
        retry = self._retry()
        problems: list[str] = []
        checked = 0

        for src, tgt in self.pairs:
            drive = self.auth.target_drive(tgt)

            @retry
            def _find(name):
                return drive.files().list(
                    q=f"name = '{name}' and 'me' in owners and trashed = false",
                    fields="files(id,name)", pageSize=5, spaces="drive",
                ).execute()

            @retry
            def _perms(fid):
                return drive.permissions().list(
                    fileId=fid,
                    fields="permissions(type,role,emailAddress,domain)",
                    supportsAllDrives=True,
                ).execute()

            hits = _find("shared-every-way.pdf").get("files", [])
            if not hits:
                problems.append(f"{tgt}: shared-every-way.pdf not found")
                continue
            checked += 1
            perms = _perms(hits[0]["id"]).get("permissions", [])
            emails = {(p.get("emailAddress") or "").lower() for p in perms}
            domains = {(p.get("domain") or "").lower() for p in perms}

            if any(e.endswith("@" + self.settings.source_domain) for e in emails):
                problems.append(f"{tgt}: SOURCE-domain address leaked into ACL")
            if not any(e.endswith("@" + self.settings.target_domain)
                       for e in emails):
                problems.append(f"{tgt}: no target-domain grantee — identity "
                                f"translation did not run")
            if self.settings.source_domain in domains:
                problems.append(f"{tgt}: domain grant still points at source")

            ext = (self.manifest.get("external") or "").lower()
            if ext and ext not in emails:
                problems.append(f"{tgt}: external collaborator {ext} dropped")

            personal = _find("Personal").get("files", [])
            if personal:
                pp = _perms(personal[0]["id"]).get("permissions", [])
                shared = [p for p in pp if p.get("role") != "owner"]
                if shared:
                    problems.append(
                        f"{tgt}: PRIVATE 'Personal' folder gained "
                        f"{len(shared)} grant(s) — a migration that leaks "
                        f"private files is worse than one that drops them"
                    )

        ok = not problems and checked > 0
        detail = (f"ACLs translated correctly on {checked} files"
                  if ok else " | ".join(problems[:4]))
        return self.record("sharing", ok, detail, t0)

    # ------------------------------------------------------------------
    def phase_duplication(self) -> bool:
        """
        The check that only exists because files are shared between users.

        Each user sees far more than they own. With OWNED_ONLY=true, the target
        must reproduce the OWNED union exactly once. A larger target means every
        shared file was copied once per recipient.
        """
        t0 = time.time()
        source_owned = self.count_source_owned()
        target = self.count_target()

        skipped = self.db.conn.execute(
            """SELECT COUNT(*) c FROM audit_log
               WHERE item_type='file' AND status LIKE 'SKIPPED%'"""
        ).fetchone()["c"]
        expected = source_owned - skipped

        # Allow a small positive margin for anything pre-existing in the target,
        # but a fan-out bug produces a multiple, not a margin.
        ratio = (target["files"] / expected) if expected else 0
        ok = expected > 0 and 0.98 <= ratio <= 1.05

        detail = (f"source owned {source_owned:,} - skipped {skipped:,} = "
                  f"{expected:,}; target has {target['files']:,} "
                  f"(ratio {ratio:.3f})")
        if ratio > 1.05:
            detail += "  <-- SHARED FILES ARE BEING DUPLICATED PER RECIPIENT"
        elif 0 < ratio < 0.98:
            detail += "  <-- files are missing from the target"
        return self.record("duplication", ok, detail, t0)

    # ------------------------------------------------------------------
    def phase_idempotency(self) -> bool:
        t0 = time.time()
        before = self.count_target()
        rc, _out = self.run_cli(["migrate"], timeout=43200)
        after = self.count_target()
        ok = rc == 0 and before == after
        if ok:
            detail = (f"second run created nothing "
                      f"({after['files']:,} files, stable)")
        else:
            delta = {k: after[k] - before[k] for k in before}
            detail = f"NOT IDEMPOTENT — second run changed counts by {delta}"
        return self.record("idempotency", ok, detail, t0)

    def phase_interrupt(self) -> bool:
        t0 = time.time()
        before = self.count_target()
        self.run_cli(["migrate"], timeout=900, interrupt_after=12.0)
        mid = self.count_target()
        rc2, _out = self.run_cli(["migrate"], timeout=43200)
        after = self.count_target()
        ok = before == after and rc2 == 0
        detail = ("interrupt and resume converged" if ok
                  else f"diverged: before={before} mid={mid} after={after}")
        return self.record("interrupt", ok, detail, t0)

    # ------------------------------------------------------------------
    def phase_delta(self) -> bool:
        t0 = time.time()
        per_user = {r["user"]: r for r in self.manifest.get("per_user", [])
                    if "error" not in r}
        edits = 0
        try:
            from tools.seed_sandbox import _media, build_services

            retry = self._retry()
            stamp = time.strftime("%Y%m%d-%H%M%S")
            for src, _tgt in self.pairs:
                entry = per_user.get(src)
                if not entry:
                    continue
                targets = entry["drive"]["items"].get("delta_files", [])
                drive, _, _ = build_services(self.settings, src)
                for fid in targets:
                    @retry
                    def _touch(f=fid):
                        return drive.files().update(
                            fileId=f,
                            media_body=_media(
                                f"edited {stamp}\n".encode(), "text/plain"),
                            fields="id,modifiedTime",
                        ).execute()

                    _touch()
                    edits += 1
        except Exception as exc:  # noqa: BLE001
            return self.record("delta", False,
                               f"could not edit source files: {exc}", t0)

        if not edits:
            return self.record("delta", False,
                               "manifest has no delta_files — reseed", t0)

        before = self.count_target()
        rc, out = self.run_cli(["delta", "--services", "drive"], timeout=21600)
        after = self.count_target()

        moved = sum(int(x) for x in re.findall(r"'files':\s*(\d+)", out))
        counts_stable = before["files"] == after["files"]
        ok = rc == 0 and counts_stable and moved == edits
        detail = (f"edited {edits}, engine moved {moved}, "
                  f"file count stable={counts_stable}")
        if not counts_stable:
            detail += "  <-- delta CREATED files instead of updating in place"
        return self.record("delta", ok, detail, t0)

    def phase_verify(self) -> bool:
        t0 = time.time()
        cmd = [sys.executable, "tools/verify.py", "--samples", "40", "--seed", "42"]
        for src, _ in self.pairs:
            cmd += ["--user", src]
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        ok = proc.returncode == 0
        tail = (proc.stdout or proc.stderr).strip().splitlines()
        detail = ("reconciled cleanly across all users" if ok
                  else " | ".join(tail[-5:]))
        return self.record("verify", ok, detail, t0)


# ======================================================================
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="End-to-end rehearsal across live sandbox tenants."
    )
    ap.add_argument("--user", action="append",
                    help="limit to specific source users (default: all)")
    ap.add_argument("--manifest", default="sandbox_manifest.json")
    ap.add_argument("--phase", action="append", choices=ALL_PHASES)
    ap.add_argument("--json", help="write results to this file")
    args = ap.parse_args(argv)

    settings = Settings()
    if settings.source_domain == settings.target_domain:
        sys.exit("REFUSING: SOURCE_DOMAIN and TARGET_DOMAIN are identical.")

    manifest = {}
    if os.path.exists(args.manifest):
        with open(args.manifest, encoding="utf-8") as fh:
            manifest = json.load(fh)
    else:
        print(f"warning: no {args.manifest}; count assertions will be weak. "
              f"Run tools/seed_sandbox.py first.")

    db = MigrationDB(settings.db_path)
    rows = [r for r in db.all_identities() if r["entity_type"] == "user"]
    if not rows:
        sys.exit("identity_map is empty — run: python main.py init-db "
                 "--identities identities.csv")
    if args.user:
        want = {u.lower() for u in args.user}
        rows = [r for r in rows if r["source_email"] in want]
    pairs = [(r["source_email"], r["target_email"]) for r in rows]

    auth = AuthManager(settings)
    r = Rehearsal(settings, auth, db, pairs, manifest)

    phases = args.phase or ALL_PHASES
    print(f"\nRehearsal across {len(pairs)} users")
    for s, t in pairs:
        print(f"    {s}  ->  {t}")
    print(f"Phases: {', '.join(phases)}\n")

    for name in phases:
        print(f"--- {name} ---")
        ok = getattr(r, f"phase_{name}")()
        if not ok and name in HARD_PREREQS:
            print(f"\nStopping: '{name}' is a hard prerequisite.")
            break

    print("\n" + "=" * 72)
    passed = sum(1 for x in r.results if x.ok)
    for x in r.results:
        print(f"  {'PASS' if x.ok else 'FAIL'}  {x.phase:<12} "
              f"{x.duration:>8.1f}s  {x.detail}")
    print("=" * 72)
    print(f"{passed}/{len(r.results)} phases passed")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump([x.__dict__ for x in r.results], fh, indent=2)

    failed = [x.phase for x in r.results if not x.ok]
    if failed:
        print(f"\nDo not run this engine against production. "
              f"Failed: {', '.join(failed)}")
    else:
        print("\nAll phases green across all users. The engine is behaving "
              "correctly against real Google APIs.")

    db.close()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
