"""
webui.py
========
Browser front-end for the migration: live status, one-click actions, streaming
output.

Security posture — read this before changing the bind address
-------------------------------------------------------------
This process runs commands on the host. That makes it a remote-code-execution
surface, so it is built to be hard to expose by accident:

* It binds to **127.0.0.1** only. Reach it over an SSH tunnel:

      ssh -L 8080:localhost:8080 root@your-vps
      # then open http://localhost:8080

* There is **no arbitrary command execution**. The browser can only name an
  action from ACTIONS below; the server maps that name to a fixed argv list.
  Nothing the client sends is ever concatenated into a shell string, and
  `shell=True` appears nowhere.

* Destructive actions are marked `destructive` and require the client to echo
  back a confirmation phrase the server checks. That is a guard against a
  mis-click, not against an attacker -- the real protection is the loopback
  bind.

* `--host` exists for unusual setups but prints a loud warning. Binding this
  to a public interface would hand anyone who finds the port a root shell in
  all but name.

No dependencies beyond the standard library, deliberately: this should not
need a pip install on a migration host at 2am.

    python3 webui.py                 # http://127.0.0.1:8080
    python3 webui.py --port 9000
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import accounts_auth  # stdlib-only itself; does not break the no-pip-install promise

try:
    from wizard import State, build_steps
except Exception:  # noqa: BLE001 - the UI should still load and say why
    State = None
    build_steps = None

PY = sys.executable

# ----------------------------------------------------------------------
# The complete set of things the browser may ask for. Anything not here
# cannot be run, however the request is crafted.
# ----------------------------------------------------------------------
ACTIONS: dict[str, dict] = {
    "preflight": {
        "label": "Preflight",
        "blurb": "Mint a token and make one call per user, both tenants.",
        "argv": [PY, "main.py", "preflight"],
    },
    "scope": {
        "label": "Show scope + required OAuth",
        "blurb": "What migrates, and the exact scopes this config needs.",
        "argv": [PY, "main.py", "scope"],
    },
    "export_scope": {
        "label": "Export SCOPE.md",
        "blurb": "Write the scope matrix to SCOPE.md for the approval ticket.",
        "argv": [PY, "main.py", "scope", "--format", "markdown", "--out", "SCOPE.md"],
    },
    "discover": {
        "label": "Discover",
        "blurb": "Read-only scan: counts, depth, size, duration estimate.",
        "argv": [PY, "main.py", "discover", "--include-mail"],
    },
    "check_seed_accounts": {
        "label": "Check seed accounts",
        "blurb": "Verify the accounts the seeder will target exist -- every "
                 "user the source tenant already has, or the 5-user default "
                 "if that lookup cannot run.",
        "argv": [PY, "check_seed.py", "accounts"],
    },
    "check_seed_scopes": {
        "label": "Check seed scopes",
        "blurb": "Verify the seeder's write scopes are authorised "
                 "(incl. admin.directory.user).",
        "argv": [PY, "check_seed.py", "scopes"],
    },
    "init_db": {
        "label": "Create database + load identities",
        "blurb": "Reads identities.csv into identity_map. Safe to re-run.",
        "argv": [PY, "main.py", "init-db", "--identities", "identities.csv"],
    },
    "init_db_auto": {
        "label": "Load identities by matching localparts",
        "blurb": "Derives pairs from both directories. Needs delegation first.",
        "argv": [PY, "main.py", "init-db", "--auto-map"],
    },
    "provision_dry": {
        "label": "Provision users (dry run)",
        "blurb": "Report which target accounts are missing. Creates nothing.",
        "argv": [PY, "main.py", "provision-users", "--tenant", "target", "--dry-run"],
    },
    "provision": {
        "label": "Create the missing target accounts",
        "blurb": "Only ever creates; existing addresses are left untouched.",
        "argv": [PY, "main.py", "provision-users", "--tenant", "target", "--yes"],
        "destructive": True,
        "confirm": "CREATE",
    },
    "migrate_dry": {
        "label": "Migrate (dry run)",
        "blurb": "Log every intended write, perform none.",
        "argv": [PY, "main.py", "--dry-run", "migrate"],
    },
    "migrate": {
        "label": "Migrate",
        "blurb": "The real copy. Resumable — safe to interrupt.",
        "argv": [PY, "main.py", "migrate", "--services", "drive,gmail,calendar"],
        "destructive": True,
        "confirm": "MIGRATE",
    },
    "delta": {
        "label": "Delta pass",
        "blurb": "Catch up anything changed since the bulk copy.",
        "argv": [PY, "main.py", "delta", "--services", "drive,gmail,calendar",
                 "--days", "2"],
    },
    "verify": {
        "label": "Verify",
        "blurb": "Ask the target directly and compare against source.",
        "argv": [PY, "verify.py", "--samples", "25"],
    },
    "report": {
        "label": "Report",
        "blurb": "Per-user counts and outstanding failures.",
        "argv": [PY, "main.py", "report"],
    },
    "resolve_dry": {
        "label": "Resolve failures (dry run)",
        "blurb": "Show what a retry of FAILED items would do.",
        "argv": [PY, "resolve_failures.py", "--dry-run"],
    },
    "undo_dry": {
        "label": "Undo (dry run)",
        "blurb": "Count exactly what a targeted undo would delete.",
        "argv": [PY, "undo_migration.py", "--dry-run"],
    },
    "undo": {
        "label": "Undo migration",
        "blurb": "Delete only what id_mapping records as migrated.",
        "argv": [PY, "undo_migration.py", "--yes"],
        "destructive": True,
        "confirm": "UNDO",
    },
    "resolve": {
        "label": "Resolve failures",
        "blurb": "Retry every FAILED item with the current code.",
        "argv": [PY, "resolve_failures.py"],
    },
    "backfill_drive": {
        "label": "Backfill: Drive done",
        "blurb": "Mark Drive complete on a ledger from before per-service "
                 "tracking existed. Only records what audit_log proves ran.",
        "argv": [PY, "main.py", "backfill-services", "--services", "drive"],
    },

    # -- phased migration: every phase, each reconciled against the tenants
    # directly rather than trusted from the ledger -----------------------
    "phased_count_only": {
        "label": "Reconcile (no migration)",
        "blurb": "Count both tenants and compare, without moving anything.",
        "argv": [PY, "phases.py", "--count-only"],
    },
    "phased_migrate": {
        "label": "Migrate: full scope",
        "blurb": "Drive, shared drives, Gmail, Calendar, Contacts, Tasks, "
                 "Chat — in that order, each reconciled before the next runs. "
                 "Chat/Contacts/Tasks follow the checkboxes above.",
        "argv": [PY, "phases.py", "--continue-on-gap"],
        "destructive": True,
        "confirm": "MIGRATE",
    },

    # -- shared drives: tenant-wide, so not part of the per-user toggles --
    "shared_drives_inventory": {
        "label": "Shared drives: inventory",
        "blurb": "Count files per shared drive. Read-only.",
        "argv": [PY, "shared_drives.py", "--inventory", "--all-drives"],
    },
    "shared_drives_migrate": {
        "label": "Shared drives: migrate",
        "blurb": "Create each shared drive on the target, restore membership "
                 "organizer-first, then copy its contents.",
        "argv": [PY, "shared_drives.py", "--migrate", "--all-drives"],
        "destructive": True,
        "confirm": "MIGRATE",
    },

    # -- per-file share access, verified one by one, not as a total -------
    "acl_audit": {
        "label": "Verify share access",
        "blurb": "Pairs every source file to the target file it became and "
                 "diffs the grant set. A total can reconcile while sharing "
                 "is wrong; this is the check that would catch it.",
        "argv": [PY, "acl_audit.py", "--json", "acl_audit.json"],
    },

    # -- SSO: org-wide login configuration, so this stays manual on purpose.
    # MIGRATE_SSO must already be set in env.sh; this button does not set it,
    # unlike the per-user services above, because writing an SSO profile
    # changes how everyone signs in, including whoever is running this. --
    "sso_inventory": {
        "label": "SSO: inventory",
        "blurb": "What's migratable (inbound SAML), what can only be listed "
                 "('Sign in with Google' grants), what can't (saved "
                 "passwords). Read-only.",
        "argv": [PY, "sso.py", "--inventory"],
    },
    "sso_migrate": {
        "label": "SSO: create profiles (unassigned)",
        "blurb": "Recreates each SAML profile on the target, unassigned. "
                 "Needs MIGRATE_SSO=true already set in env.sh — this button "
                 "will not set it for you.",
        "argv": [PY, "sso.py", "--migrate"],
        "destructive": True,
        "confirm": "SSO",
    },
}

# ----------------------------------------------------------------------
# A seed job's numeric progress.
#
# seed_sandbox.py has no structured progress protocol -- it is a CLI script
# printing to stdout, not an API. The nav bar's progress figure has to come
# from somewhere, though, and this engine's own discipline (see
# webui_spa.py's module docstring) is to say plainly when a number is a
# proxy rather than inventing a false-precision one. This one is real, just
# derived: seed_sandbox.py always prints "Seeding N users in ..." once
# per run, and exactly one "done in" or "FAILED" line per user as it
# finishes -- both attempted-so-far, so a completed run always reaches
# 100%, and a straggler mid-run never overstates itself.
# ----------------------------------------------------------------------
_SEED_TOTAL_RE = re.compile(r"^Seeding (\d+) users? in\b")
_SEED_DONE_RE = re.compile(r"^\s*\[.+?\]\s+done in\b")
_SEED_FAILED_RE = re.compile(r"^\s*!\s+\S+\s+FAILED:")


def _seed_progress_pct(lines: list[str]) -> int | None:
    total = None
    for ln in lines:
        m = _SEED_TOTAL_RE.match(ln)
        if m:
            total = int(m.group(1))
            break
    if not total:
        return None
    attempted = sum(1 for ln in lines
                    if _SEED_DONE_RE.match(ln) or _SEED_FAILED_RE.match(ln))
    return round(min(attempted, total) / total * 100)


# ----------------------------------------------------------------------
# One job at a time, with its output buffered for streaming to the page.
# ----------------------------------------------------------------------
class Job:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.name = ""
        self.lines: list[str] = []
        self.started = 0.0
        self.finished = 0.0
        self.rc: int | None = None
        self._on_finish: Callable[[int | None], None] | None = None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, name: str, argv: list[str],
              env: dict | None = None, cwd: str | None = None,
              on_finish: Callable[[int | None], None] | None = None
              ) -> tuple[bool, str]:
        with self.lock:
            if self.running:
                return False, f"{self.name} is still running"
            self.name, self.lines, self.rc = name, [], None
            self.started, self.finished = time.time(), 0.0
            # Only deploy_remote.py's caller passes this today -- it is how
            # deploy history learns the outcome of a job that runs detached
            # from the request that started it, without polling its own
            # poll loop just to catch the one moment rc stops being None.
            self._on_finish = on_finish
            env = dict(env or os.environ)
            env.setdefault("PYTHONUNBUFFERED", "1")
            try:
                self.proc = subprocess.Popen(
                    argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    # A child must never block on a hidden interactive prompt:
                    # input() against the server's terminal hangs the job with
                    # no output, which reads as a stalled migration.
                    stdin=subprocess.DEVNULL,
                    text=True, bufsize=1, env=env,
                    cwd=cwd or os.path.dirname(os.path.abspath(__file__)),
                )
            except Exception as exc:  # noqa: BLE001
                return False, str(exc)
            threading.Thread(target=self._drain, daemon=True).start()
            return True, "started"

    def _drain(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.rstrip("\n")
            # FutureWarnings from the google libraries drown everything else.
            if "FutureWarning" in line or "warnings.warn" in line:
                continue
            with self.lock:
                self.lines.append(line)
                if len(self.lines) > 4000:
                    del self.lines[:1000]
        self.rc = self.proc.wait()
        self.finished = time.time()
        if self._on_finish is not None:
            try:
                self._on_finish(self.rc)
            except Exception:  # noqa: BLE001
                # A broken history write must never take down the drain
                # thread -- the job itself already finished successfully or
                # not; losing its history entry is a lesser failure than
                # losing the ability to see that the job ran at all.
                pass

    def stop(self) -> str:
        with self.lock:
            if not self.running:
                return "nothing running"
            # SIGINT, not SIGKILL: the engine handles it cooperatively,
            # finishing in-flight items and committing state so a re-run
            # resumes cleanly.
            self.proc.send_signal(2)  # type: ignore[union-attr]
            return f"interrupt sent to {self.name}"

    def snapshot(self, since: int = 0) -> dict:
        # _job_progress() can open a sqlite connection (for migrate/delta/
        # discover's ledger-backed fraction) -- done after releasing the
        # lock below so a slow disk read never blocks start()/stop() for
        # every other request in flight.
        with self.lock:
            name, running, rc = self.name, self.running, self.rc
            elapsed = round((self.finished or time.time()) - self.started, 1) \
                if self.started else 0
            total = len(self.lines)
            lines = self.lines[since:]
            all_lines = self.lines if name == "seed" else None
        # Only seed_sandbox.py's own printed lines and the migrate/delta/
        # discover ledger have a real completion fraction to read --
        # everything else (deploy, reset target, provisioning, ...) has no
        # meaningful percentage at all, so this stays null there rather
        # than guessing. See _job_progress(). Percentage is still worth
        # showing after the job stops (a finished run's final 100%); an ETA
        # is not -- "time left" on a job that already ended is nonsense, so
        # that half is dropped the moment running goes false.
        pct, eta = _job_progress(name, all_lines or [], elapsed)
        if not running:
            eta = None
        return {
            "name": name,
            "running": running,
            "rc": rc,
            # Frozen at completion. Computing this live meant a finished
            # job's "duration" kept climbing with the clock -- an init-db
            # that took under a second was reported as "exit 0 · 105.1s",
            # which reads as a performance problem that does not exist.
            "elapsed": elapsed,
            "total": total,
            "lines": lines,
            "progressPct": pct,
            "etaSeconds": eta,
        }


JOBS: dict[int | None, Job] = {}


def get_job(account_id: int | None) -> Job:
    """One Job instance per account, lazily created -- see the class
    docstring above for what a Job actually is. Only /api/seed,
    /api/reset_target, and the /api/job poller are account-scoped today;
    every other endpoint that launches or reads a Job (Setup Wizard,
    Deploy, stages/report payloads) still reads the plain `JOB` name below,
    which is simply JOBS[None] -- the legacy, single-tenant behaviour this
    had before per-account isolation existed, unchanged for account #1 and
    every caller with no account context to give."""
    if account_id not in JOBS:
        JOBS[account_id] = Job()
    return JOBS[account_id]


JOB = get_job(None)
# Which tenant the in-flight consent belongs to, so the callback knows.
_PENDING: dict[str, str] = {}

# ----------------------------------------------------------------------
# Processes started OUTSIDE this webui (over SSH, from the TUI, from cron...)
#
# The webui's own Job only ever sees the children it spawned itself, so an
# operator who kicked off a migrate from another shell watched "idle" no
# matter how much work was running. A live ps scan is the only honest way to
# notice processes that arrive and leave without ever passing through this
# process -- so it is done here, on every job poll. Migration subcommands of
# main.py, plus the standalone job scripts the webui itself can also launch
# (seed/reset/deploy), are the things worth surfacing as "active".
# ----------------------------------------------------------------------
_EXT_MAIN_CMDS = {"init-db", "preflight", "provision-users", "discover",
                  "migrate", "delta", "syncacls", "report",
                  "backfill-services", "scope"}
_EXT_SCRIPTS = {"seed_sandbox.py": "seed", "reset_target.py": "reset target",
                "deploy_remote.py": "deploy", "verify.py": "verify",
                "resolve_failures.py": "resolve-failures",
                "phases.py": "phases"}
# Last-seen output tail per external pid, for the suffix-diff that turns the
# unbounded migration.log into the same "just the new lines" contract the
# webui-launched Job streams.
_EXT_TAIL: dict[int, list[str]] = {}
_EXT_TAIL_LEN: int = 2000
_EXT_INITIAL_LINES: int = 120


def _detached_log_path() -> str:
    """The job log a detached main.py writes to -- migration.log in the repo
    dir by default (see config.Settings.log_file)."""
    try:
        from config import Settings
        lp = Settings().log_file
    except Exception:  # noqa: BLE001 - default is right if config is unreadable
        lp = "migration.log"
    if os.path.isabs(lp):
        return lp
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), lp)


def _process_output_tail(pid: int) -> list[str]:
    """The last _EXT_TAIL_LEN lines of a process's own output. Where stdout
    is redirected to a file -- the normal way a migrate is run over SSH -- its
    actual target is resolved through /proc/<pid>/fd/1, so the live feed
    streams the real log the operator is used to seeing, not the static
    migration.log a run may have stopped writing to. Falls back to that
    migration.log when stdout is a pipe or the proc target is unreadable."""
    path = None
    try:
        tgt = os.readlink(f"/proc/{pid}/fd/1")
        if os.path.isabs(tgt) and os.path.exists(tgt):
            path = tgt
    except OSError:
        pass
    path = path or _detached_log_path()
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return [ln.rstrip("\n") for ln in fh.readlines()[-_EXT_TAIL_LEN:]]
    except OSError:
        return []


def _external_processes() -> list[dict]:
    """Running migration processes this webui did not start, as
    {pid, elapsed, name}. A migrate always sorts first so a seed running in
    another tab never hides the real migration."""
    try:
        out = subprocess.run(["ps", "-eo", "pid=,etimes=,args="],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:  # noqa: BLE001 - ps missing means nothing external running
        return []
    found: list[dict] = []
    own = str(os.getpid())
    shells = {"bash", "sh", "zsh", "dash", "fish", "-bash", "-sh", "-zsh", "-dash"}
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        pid, etimes, args = parts
        if pid == own:
            continue
        # A `bash -c "… main.py migrate …"` wrapper would otherwise look
        # exactly like a second migration. Only the real program token counts.
        if args.split(None, 1)[0] in shells:
            continue
        try:
            elapsed = int(etimes)
        except ValueError:
            continue
        name = next((label for script, label in _EXT_SCRIPTS.items()
                     if re.search(r"(?:^|/)" + re.escape(script) + r"(?:\s|$)",
                                  args)), None)
        if name is None:
            m = re.search(r"(?:^|[\s/])main\.py\s+(\S+)", args)
            if m and m.group(1) in _EXT_MAIN_CMDS:
                name = m.group(1)
        if name is None:
            continue
        found.append({"pid": int(pid), "elapsed": elapsed, "name": name})
    found.sort(key=lambda x: (x["name"] != "migrate", -x["elapsed"], x["pid"]))
    return found


def _external_job_snapshot() -> dict | None:
    """A Job.snapshot()-shaped view of the primary external process, or None
    when nothing is running outside the webui. progressPct/eta reuse the same
    ledger-backed math a webui-launched migrate gets."""
    jobs = _external_processes()
    live = {j["pid"] for j in jobs}
    for stale in [pid for pid in _EXT_TAIL if pid not in live]:
        del _EXT_TAIL[stale]
    if not jobs:
        return None
    job = jobs[0]
    tail = _process_output_tail(job["pid"])
    prev = _EXT_TAIL.get(job["pid"])
    _EXT_TAIL[job["pid"]] = tail
    if prev is None:
        # Brand-new process: hand over a recent slice as history, so the live
        # feed shows where the run already is -- not just lines added from
        # this exact second forward.
        lines = tail[-_EXT_INITIAL_LINES:]
    else:
        # Suffix-diff: the tail window always holds the newest lines, so the
        # new ones are the trailing slice of tail after the longest prefix of
        # tail that is still a suffix of the previous window. The descending
        # search finds it on the first try in the steady states (no new
        # output, or a log shorter than the window); only a window that rolled
        # entirely over costs more than a couple of tries.
        limit = min(len(prev), len(tail))
        match = next((c for c in range(limit, 0, -1)
                      if tail[:c] == prev[len(prev) - c:]), 0)
        lines = tail[match:] if match < len(tail) else []
    pct, eta = _job_progress(job["name"], tail, job["elapsed"])
    return {
        "name": job["name"],
        "running": True,
        "rc": None,
        "elapsed": job["elapsed"],
        "total": len(tail),
        "lines": lines,
        "progressPct": pct,
        "etaSeconds": eta,
        "detached": True,
        "pid": job["pid"],
        "pids": [j["pid"] for j in jobs],
    }

# ----------------------------------------------------------------------
# OAuth: the flow a non-technical admin can actually complete.
# ----------------------------------------------------------------------
_FLOWS: dict[str, object] = {}


def _oauth_config(settings):
    """The client secrets are the *vendor's*, created once -- not something a
    tenant admin ever has to make."""
    path = settings.oauth_client_secrets
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def oauth_status() -> dict:
    from config import Settings
    import oauth_store

    st = Settings()
    store = oauth_store.TokenStore(st.oauth_token_dir)
    return {
        "configured": _oauth_config(st) is not None,
        "client_secrets_path": st.oauth_client_secrets,
        "auth_mode": st.auth_mode,
        "source": store.describe("source"),
        "target": store.describe("target"),
    }


def oauth_begin(tenant: str, port: int) -> dict:
    from config import Settings, source_scopes, target_scopes
    import oauth_store

    st = Settings()
    cfg = _oauth_config(st)
    if not cfg:
        return {"ok": False, "error":
                f"no OAuth client secrets at {st.oauth_client_secrets} -- "
                f"create one OAuth client ID (Desktop or Web) in any GCP "
                f"project and save the JSON there. This is done once, by you, "
                f"not by each tenant."}
    scopes = (source_scopes(st) if tenant == "source" else target_scopes(st))
    redirect = f"http://localhost:{port}/oauth/callback"
    try:
        flow = oauth_store.build_flow(cfg, scopes, redirect)
        url = oauth_store.authorization_url(flow)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    _FLOWS[tenant] = flow
    return {"ok": True, "url": url}


def oauth_finish(tenant: str, full_url: str) -> dict:
    import oauth_store
    from config import Settings

    flow = _FLOWS.get(tenant)
    if flow is None:
        return {"ok": False, "error": "no sign-in in progress for that tenant"}
    try:
        flow.fetch_token(authorization_response=full_url)
        creds = flow.credentials
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}

    if not creds.refresh_token:
        # Without this the migration dies an hour in, when the access token
        # expires and there is nothing to renew it with.
        return {"ok": False, "error":
                "Google returned no refresh token. Remove this app's access at "
                "myaccount.google.com/permissions and try again."}

    account = domain = ""
    try:
        from googleapiclient.discovery import build as _build
        import google_auth_httplib2, httplib2
        http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=30))
        me = _build("drive", "v3", http=http, cache_discovery=False
                    ).about().get(fields="user").execute()
        account = (me.get("user") or {}).get("emailAddress", "")
        domain = account.split("@")[-1] if account else ""
    except Exception:  # noqa: BLE001 - identity is a nicety, not required
        pass

    st = Settings()
    oauth_store.TokenStore(st.oauth_token_dir).save(
        tenant, oauth_store.credentials_to_dict(creds, account, domain))
    return {"ok": True, "account": account, "domain": domain}


# Which action (if any) actually performs each wizard step. A step with no
# entry here is either manual or has no single command that completes it.
#
# Step 7 (seeding) is absent from ACTIONS but is *not* absent from the UI: it
# runs through /api/seed instead. ACTIONS entries are fixed argv with no
# per-request input, which is what makes them safe to fire from a button --
# and it is exactly the wrong shape for the seeder, which writes fabricated
# data into a live tenant and must be aimed by hand. /api/seed requires the
# source domain typed back before it will build a command.
STEP_ACTIONS: dict[int, list[str]] = {
    4: ["init_db", "init_db_auto"],
    5: ["preflight"],
    6: ["provision_dry", "provision"],
    7: ["check_seed_accounts", "check_seed_scopes"],
    8: ["discover", "migrate_dry", "migrate"],
    9: ["verify", "report", "acl_audit", "resolve"],
}


# ----------------------------------------------------------------------
# Typed input from the browser.
#
# The module contract above says the client can never cause an arbitrary
# command to run. Collecting a domain or a hostname does not weaken that: each
# value is matched against a strict pattern here and then placed into a fixed
# argv list, never concatenated into a shell string. A value that does not
# match is rejected outright rather than escaped -- there is no legitimate
# domain or username that these patterns exclude.
# ----------------------------------------------------------------------
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_DOMAIN_RE = re.compile(rf"^(?:{_LABEL}\.)+[a-z]{{2,63}}$", re.I)
_EMAIL_RE = re.compile(rf"^[a-z0-9._%+-]{{1,64}}@(?:{_LABEL}\.)+[a-z]{{2,63}}$", re.I)
_HOST_RE = re.compile(rf"^(?:{_LABEL}\.)*{_LABEL}$|^\d{{1,3}}(?:\.\d{{1,3}}){{3}}$", re.I)
_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$", re.I)

# The React SPA (Mission Control) builds to here via `npm run build`. Served
# under /app rather than at "/" because "/" is this file's own inline setup
# wizard -- an existing, working flow that must not be displaced by a build
# artifact that may not exist yet on a fresh checkout.
SPA_DIST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "migration-webui", "dist")

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "env.sh")

# ----------------------------------------------------------------------
# Deploy history: every /api/deploy invocation, appended when it starts and
# updated in place when it finishes. Nothing tracked this before -- "did the
# last deploy to this VPS actually work, and what commit is running there
# now" had no answer except SSHing in and checking by hand. A flat JSON
# array (not migration.db) because a deploy can happen before that database
# exists at all, and this has nothing to do with migration state.
# ----------------------------------------------------------------------
DEPLOY_HISTORY_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "deploy_history.json")
_DEPLOY_HISTORY_LOCK = threading.Lock()
_MAX_DEPLOY_HISTORY = 100


def load_deploy_history() -> list[dict]:
    """Most recent first. Never raises -- a corrupt or absent history file
    means an empty list, not a broken Deploy tab."""
    try:
        with open(DEPLOY_HISTORY_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save_deploy_history(records: list[dict]) -> None:
    tmp = DEPLOY_HISTORY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(records, fh, indent=2)
    os.replace(tmp, DEPLOY_HISTORY_PATH)  # atomic on the same filesystem


def record_deploy_start(host: str, user: str, port: str, ui_port: str,
                        include_credentials: bool, commit: str) -> str:
    """Appends a new in-progress record and returns its id, used later to
    find and update it once the job finishes."""
    with _DEPLOY_HISTORY_LOCK:
        records = load_deploy_history()
        rec_id = f"{time.time():.6f}"
        records.insert(0, {
            "id": rec_id,
            "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "finishedAt": None,
            "host": host, "user": user, "port": port, "uiPort": ui_port,
            "includeCredentials": include_credentials,
            "commit": commit,
            "rc": None,
        })
        del records[_MAX_DEPLOY_HISTORY:]
        _save_deploy_history(records)
        return rec_id


def record_deploy_finish(rec_id: str, rc: int | None) -> None:
    with _DEPLOY_HISTORY_LOCK:
        records = load_deploy_history()
        for r in records:
            if r.get("id") == rec_id:
                r["rc"] = rc
                r["finishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                break
        else:
            return  # record vanished (history trimmed mid-run) -- nothing to update
        _save_deploy_history(records)


_CONFIG_FIELDS = (
    ("source_domain", "SOURCE_DOMAIN", _DOMAIN_RE, "a domain like c.example.com"),
    ("target_domain", "TARGET_DOMAIN", _DOMAIN_RE, "a domain like a.example.com"),
    ("source_admin", "SOURCE_ADMIN", _EMAIL_RE, "a super-admin address in the source domain"),
    ("target_admin", "TARGET_ADMIN", _EMAIL_RE, "a super-admin address in the target domain"),
)


_HOST_INFO_CACHE: dict | None = None


def _primary_ip() -> str:
    """
    This machine's outbound-facing IP, without sending any traffic --
    connect() on a UDP socket only picks a local interface/route, no packet
    actually goes anywhere. Loopback fallback keeps this from ever raising.
    """
    import socket

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return "127.0.0.1"


def host_info() -> dict:
    """
    Where this process is actually running -- the answer to "is this the
    VPS, or is this still my laptop?", found the hard way once already: a
    local seed run and a deployed VPS instance can both bind 127.0.0.1:8080
    and look identical in the browser, and nothing on screen said which one
    a given tab was talking to.

    `location` is the one field meant to answer that at a glance: a laptop's
    outbound interface is almost always a private/NAT'd address (RFC1918,
    or loopback), while a VPS's is a routable public IP -- so a public
    address is shown as itself (it names the actual VPS), and a private one
    is shown as "Local machine" (the address itself is not useful there;
    every laptop on a LAN has one and it means nothing to the operator).

    hostname + this file's own directory further identify the machine and
    the deployment; commit is best-effort (this is a git checkout locally,
    but a deploy_remote.py target is a plain rsync copy with no .git at all
    -- see its own module docstring for why, so an absent commit there is
    normal, not an error). Cached for the process's lifetime: none of this
    changes while it is running, so there is no reason to shell out to git
    or probe the network on every poll.
    """
    global _HOST_INFO_CACHE
    if _HOST_INFO_CACHE is not None:
        return _HOST_INFO_CACHE

    import ipaddress
    import socket

    code_dir = os.path.dirname(os.path.abspath(__file__))
    commit = ""
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                          cwd=code_dir, capture_output=True, text=True, timeout=3)
        if r.returncode == 0:
            commit = r.stdout.strip()
    except Exception:  # noqa: BLE001 - not a git checkout is a normal state
        pass

    ip = _primary_ip()
    try:
        is_private = ipaddress.ip_address(ip).is_private
    except ValueError:
        is_private = True
    location = "Local machine" if is_private else ip

    _HOST_INFO_CACHE = {
        "hostname": socket.gethostname(),
        "ip": ip,
        "location": location,
        "code_path": code_dir,
        "commit": commit,
        "pid": os.getpid(),
    }
    return _HOST_INFO_CACHE


def read_config() -> dict:
    from wizard import load_env

    env = load_env(ENV_PATH)
    return {field: env.get(key, "") for field, key, _, _ in _CONFIG_FIELDS}


def validate_config(body: dict) -> tuple[dict, str]:
    """Returns (clean, error). Empty error means every field is acceptable."""
    clean = {}
    for field, key, pattern, hint in _CONFIG_FIELDS:
        val = (body.get(field) or "").strip().lower()
        if not val:
            return {}, f"{field.replace('_', ' ')} is required — {hint}"
        if not pattern.match(val):
            return {}, f"{val!r} is not {hint}"
        clean[key] = val
    # An admin outside the domain it administers is the single most common
    # entry error here, and it fails much later with a confusing 401.
    for admin_key, domain_key, side in (("SOURCE_ADMIN", "SOURCE_DOMAIN", "source"),
                                        ("TARGET_ADMIN", "TARGET_DOMAIN", "target")):
        if not clean[admin_key].endswith("@" + clean[domain_key]):
            return {}, (f"{clean[admin_key]} is not in {clean[domain_key]} — the "
                        f"{side} admin must be an account in the {side} domain")
    if clean["SOURCE_DOMAIN"] == clean["TARGET_DOMAIN"]:
        return {}, "source and target domains must differ"
    return clean, ""


_DEPLOY_ENV_KEYS = {"host": "DEPLOY_HOST", "user": "DEPLOY_USER",
                    "port": "DEPLOY_PORT", "key": "DEPLOY_KEY",
                    "ui_port": "DEPLOY_UI_PORT"}


def read_deploy_config() -> dict:
    """
    The VPS connection details Deploy last used, if any.

    Previously these lived only in the browser's in-memory JS state (`dep`)
    -- gone on every page reload, and never available at all from the SPA,
    which had no deploy UI. Persisted the same way source/target domain
    config already is: KEY=VALUE pairs in env.sh, so "add my VPS once" and
    "the UI already knows it next time" are the same file.
    """
    from wizard import load_env

    env = load_env(ENV_PATH)
    return {
        "host": env.get("DEPLOY_HOST", ""),
        "user": env.get("DEPLOY_USER", "root"),
        "port": env.get("DEPLOY_PORT", "22"),
        "key": env.get("DEPLOY_KEY", ""),
        "ui_port": env.get("DEPLOY_UI_PORT", "8080"),
    }


def validate_deploy_config(body: dict) -> tuple[dict, str]:
    """
    Returns (env-var pairs to persist, error). Empty error means acceptable.

    Reuses deploy_remote.validate() rather than re-deriving the host/user
    patterns here -- the same check /api/deploy itself relies on before
    ever shelling out to rsync/ssh.
    """
    import deploy_remote

    host = (body.get("host") or "").strip()
    user = (body.get("user") or "root").strip()
    key = (body.get("key") or "").strip()
    try:
        port = int(body.get("port") or 22)
        ui_port = int(body.get("ui_port") or 8080)
    except (TypeError, ValueError):
        return {}, "port must be a number"
    err = deploy_remote.validate(host, user, port, key)
    if err:
        return {}, err
    return {"DEPLOY_HOST": host, "DEPLOY_USER": user, "DEPLOY_PORT": str(port),
            "DEPLOY_KEY": key, "DEPLOY_UI_PORT": str(ui_port)}, ""


def write_config_raw(pairs: dict) -> None:
    """Merge arbitrary KEY=VALUE pairs into env.sh and the live environment."""
    from wizard import load_env

    env = load_env(ENV_PATH)
    env.update(pairs)
    lines = [f"export {k}={v}" for k, v in sorted(env.items())]
    with open(ENV_PATH, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    for k, v in pairs.items():
        os.environ[k] = v


def write_config(clean: dict) -> None:
    """Merge into env.sh, preserving anything setup.sh wrote (SA emails, key
    paths, tunables) so saving the form never silently discards them."""
    from wizard import load_env

    env = load_env(ENV_PATH)
    env.update(clean)
    env.setdefault("MIGRATION_DB", os.path.join(os.path.dirname(ENV_PATH), "migration.db"))
    env.setdefault("SCRATCH_DIR", os.path.join(os.path.dirname(ENV_PATH), "scratch"))
    lines = [f"export {k}={v}" for k, v in sorted(env.items())]
    with open(ENV_PATH, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    # So the running process picks the values up without a restart.
    for k, v in clean.items():
        os.environ[k] = v
    invalidate_status()


# ----------------------------------------------------------------------
# Credential upload
#
# The files themselves cannot be created by any API (see UPLOADS below), so
# the least this can do is take the file, check it is the right one, and put
# it in the right place with the right permissions. Getting those three things
# wrong by hand is most of the setup pain.
# ----------------------------------------------------------------------
MAX_UPLOAD = 256 * 1024          # these files are ~2-5 KB; anything larger is a mistake


def _check_oauth_client(data: dict) -> str:
    root = data.get("installed") or data.get("web")
    if not root:
        if data.get("type") == "service_account":
            return ("that is a service-account key, not an OAuth client. The "
                    "OAuth client JSON has an \"installed\" or \"web\" section "
                    "and comes from APIs & Services -> Credentials -> Create "
                    "credentials -> OAuth client ID.")
        return ('not an OAuth client file — expected an "installed" or "web" key')
    for field in ("client_id", "client_secret", "auth_uri", "token_uri"):
        if not root.get(field):
            return f"OAuth client JSON is missing {field}"
    return ""


def _check_sa_key(data: dict) -> str:
    if data.get("type") != "service_account":
        if data.get("installed") or data.get("web"):
            return ("that is an OAuth client file, not a service-account key. "
                    "The key comes from IAM & Admin -> Service Accounts -> "
                    "Keys -> Add key -> Create new key -> JSON.")
        return 'not a service-account key — expected "type": "service_account"'
    for field in ("client_email", "private_key", "client_id", "project_id"):
        if not data.get(field):
            return f"service-account key is missing {field}"
    if "PRIVATE KEY" not in data.get("private_key", ""):
        return "the private_key field does not contain a PEM private key"
    return ""


UPLOADS: dict[str, dict] = {
    "oauth_client": {
        "path": lambda st: st.oauth_client_secrets,
        "check": _check_oauth_client,
        "label": "OAuth client ID",
    },
    "source_key": {
        "path": lambda st: st.source_sa_key,
        "check": _check_sa_key,
        "label": "source service-account key",
    },
    "target_key": {
        "path": lambda st: st.target_sa_key,
        "check": _check_sa_key,
        "label": "target service-account key",
    },
}


def upload_credential(kind: str, content: str) -> dict:
    """Validate an uploaded credential file and store it mode 0600."""
    import stat as _stat

    from config import Settings

    spec = UPLOADS.get(kind)
    if not spec:
        return {"ok": False, "error": f"unknown upload kind {kind!r}"}
    if not content or not content.strip():
        return {"ok": False, "error": "the file is empty"}
    if len(content) > MAX_UPLOAD:
        return {"ok": False, "error": "that file is far too large to be a credential"}
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"not valid JSON ({exc.msg} at line {exc.lineno})"}
    if not isinstance(data, dict):
        return {"ok": False, "error": "expected a JSON object"}

    # Uploading the wrong one of these two files is easy and the resulting
    # runtime error is unrecognisable, so name the mistake here instead.
    err = spec["check"](data)
    if err:
        return {"ok": False, "error": err}

    path = spec["path"](Settings())
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    os.chmod(parent, _stat.S_IRWXU)                     # 0700

    # Keep the previous credential before replacing it. Uploading into the
    # wrong slot is a one-click mistake and the file being replaced may be the
    # only copy on this machine; a stamped backup makes that recoverable
    # instead of final. (Learned the hard way -- an overwrite during testing
    # destroyed the only copy of a real key.)
    backup = ""
    if os.path.exists(path) and os.path.getsize(path) > 0:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = f"{path}.{stamp}.bak"
        try:
            with open(path, "rb") as src, open(backup, "wb") as dst:
                dst.write(src.read())
            os.chmod(backup, _stat.S_IRUSR | _stat.S_IWUSR)
        except OSError:
            backup = ""

    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.chmod(path, _stat.S_IRUSR | _stat.S_IWUSR)       # 0600

    detail = ""
    if kind == "oauth_client":
        root = data.get("installed") or data.get("web")
        detail = root.get("client_id", "")[:32]
    else:
        detail = data.get("client_email", "")
    invalidate_status()
    msg = f"saved {spec['label']} to {path} (mode 0600)"
    if backup:
        msg += f"; previous file kept as {os.path.basename(backup)}"
    return {"ok": True, "path": path, "detail": detail,
            "backup": backup, "msg": msg}


AUTH_MODES = {
    "key": {
        "label": "Service-account key",
        "blurb": "Two JSON key files. Migrates every user. The default.",
        "needs": ["source_key", "target_key"],
    },
    "impersonate": {
        "label": "Keyless (impersonation)",
        "blurb": "No files at all. Use when org policy blocks key downloads.",
        "needs": [],
    },
    "oauth": {
        "label": "Browser sign-in (OAuth)",
        "blurb": "An admin clicks Allow. Migrates only that admin's own account.",
        "needs": ["oauth_client"],
    },
}


def set_run_mode(mode: str) -> dict:
    """Switch what this run is for; persisted the same way as AUTH_MODE."""
    from wizard import RUN_MODES

    if mode not in RUN_MODES:
        return {"ok": False, "error": f"unknown run mode {mode!r}"}
    write_config_raw({"RUN_MODE": mode})
    invalidate_status()
    return {"ok": True, "run_mode": mode,
            "msg": f"this run is now: {RUN_MODES[mode]['label']}"}


def set_auth_mode(mode: str) -> dict:
    """
    Switch credential mode and persist it.

    Written to env.sh *and* os.environ: Settings reads AUTH_MODE at
    construction, and every request builds a fresh Settings, so the change
    takes effect on the next poll rather than needing a restart.
    """
    if mode not in AUTH_MODES:
        return {"ok": False, "error": f"unknown auth mode {mode!r}"}
    write_config_raw({"AUTH_MODE": mode})
    invalidate_status()
    return {"ok": True, "auth_mode": mode,
            "msg": f"credential mode set to {AUTH_MODES[mode]['label']}"}


def inspect_credential(kind: str) -> dict:
    """
    What is actually in the file on disk.

    "Present" is not the same as "usable". A file can arrive by scp, rsync or
    setup.sh without ever passing through the upload validator, and the two
    credential JSONs are easy to confuse. This re-reads and re-checks whatever
    is there now, and surfaces the fields that matter -- in particular the
    numeric **Client ID**, which is what step 5 needs pasted into the Admin
    Console and which is otherwise buried in the file.
    """
    from config import Settings

    spec = UPLOADS.get(kind)
    if not spec:
        return {"present": False, "valid": False, "error": f"unknown kind {kind!r}"}

    path = spec["path"](Settings())
    info = {"path": path, "present": False, "valid": False, "error": "", "detail": {}}
    if not (os.path.exists(path) and os.path.getsize(path) > 0):
        return info
    info["present"] = True

    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        info["error"] = f"unreadable: {exc}"
        return info
    if not isinstance(data, dict):
        info["error"] = "expected a JSON object"
        return info

    err = spec["check"](data)
    if err:
        info["error"] = err
        return info

    info["valid"] = True
    if kind == "oauth_client":
        root = data.get("installed") or data.get("web")
        info["detail"] = {
            "client_id": root.get("client_id", ""),
            "type": "installed" if data.get("installed") else "web",
            "project_id": root.get("project_id", ""),
        }
    else:
        info["detail"] = {
            "client_email": data.get("client_email", ""),
            # The 21-digit uniqueId, NOT the email -- this is the string the
            # Admin Console's domain-wide delegation screen asks for.
            "client_id": data.get("client_id", ""),
            "project_id": data.get("project_id", ""),
        }
    return info


def uploads_status() -> dict:
    out = {kind: inspect_credential(kind) for kind in UPLOADS}

    # One service account in both slots is *allowed*, not broken: delegation is
    # authorised per domain, so the same Client ID can be granted the source
    # scopes in one Admin Console and the target scopes in the other. Say what
    # it implies rather than calling it an error -- but do flag it, because it
    # is also exactly what an accidental upload into the wrong slot looks like.
    src, tgt = out.get("source_key", {}), out.get("target_key", {})
    if src.get("valid") and tgt.get("valid"):
        a = (src.get("detail") or {}).get("client_email", "")
        b = (tgt.get("detail") or {}).get("client_email", "")
        if a and a == b:
            for side in (src, tgt):
                side["warning"] = (
                    f"Both slots hold the same service account ({a}). That works "
                    f"if you authorise this one Client ID in BOTH Admin Consoles, "
                    f"each with its own scope line — but it means a single "
                    f"credential can read the source and write the target. If you "
                    f"meant to use a separate account per tenant, one of these is "
                    f"in the wrong slot."
                )
    return out


def live_check(kind: str) -> dict:
    """
    Does this credential actually work?

    Structural validity says the file is well formed; it says nothing about
    whether Google will accept it. This mints a real token and makes one API
    call as the tenant's admin, which is the only thing that distinguishes
    "valid JSON" from "delegation is granted and the account is reachable".

    Deliberately on demand: it takes seconds and must never run on a poll.
    """
    if kind == "oauth_client":
        return {"ok": False,
                "error": "an OAuth client cannot be tested without a sign-in — "
                         "use 'Connect tenant' below, which is the real test"}

    info = inspect_credential(kind)
    if not info["present"]:
        return {"ok": False, "error": "no file uploaded yet"}
    if not info["valid"]:
        return {"ok": False, "error": info["error"]}

    tenant = "source" if kind == "source_key" else "target"

    from config import Settings

    st = Settings()
    admin = st.source_admin if tenant == "source" else st.target_admin
    if not admin:
        return {"ok": False,
                "error": f"set the {tenant} admin address in step 2 first — a "
                         f"delegated call has to be made as a real user"}
    try:
        from auth import AuthManager

        ok, msg = AuthManager(st).verify_delegation(tenant, admin)
    except (ImportError, AttributeError) as exc:
        # Not a credential problem. Seen live as "module
        # 'googleapiclient.discovery_cache' has no attribute 'get_static_doc'"
        # after a virtualenv under /tmp was partially deleted by the OS's
        # temp-file cleaner. Reporting that as a failed key sends someone to
        # regenerate credentials that were never at fault.
        return {"ok": False, "environment": True,
                "error": f"the Google client library in this environment is "
                         f"broken, so the key could not be tested: {exc}. "
                         f"Reinstall it (pip install -r requirements.txt) and "
                         f"try again — this says nothing about the key itself."}
    except Exception as exc:  # noqa: BLE001 - shown to the operator verbatim
        return {"ok": False, "error": str(exc)[:400]}

    if ok:
        return {"ok": True,
                "msg": f"minted a token and called Drive as {admin} — this key "
                       f"works and delegation is granted"}

    # The three failures here need opposite fixes, so name them apart.
    low = msg.lower()
    if "unauthorized_client" in low:
        hint = ("the key is fine but its Client ID is not authorised in the "
                f"{tenant} Admin Console, or the scope line was pasted "
                "incompletely — see step 5")
    elif "invalid_grant" in low or "not found" in low:
        hint = f"{admin} does not exist in that domain"
    elif "active session is invalid" in low:
        hint = (f"{admin} is unreachable — suspended, awaiting a login "
                f"challenge, or the tenant's trial has expired")
    else:
        hint = msg[:220]
    return {"ok": False, "error": hint}


# ----------------------------------------------------------------------
# Seeding
#
# This writes fabricated data into a live tenant, so it is the one action
# where the friction *is* the feature. The CLI protects itself three ways --
# SANDBOX_MODE=true, --confirm-domain matching SOURCE_DOMAIN exactly, and a
# PROTECTED_DOMAINS deny list. None of those are bypassed here: the browser
# has to supply the domain by typing it, and the server re-checks everything
# before building the command. The seeder then checks again for itself.
# ----------------------------------------------------------------------
SEED_SCALES = ("tiny", "small", "medium", "large", "huge")


def seed_argv(body: dict, account_id: int | None = None) -> tuple[list[str], dict, str]:
    """Build the seeder command, or return why it must not run.

    account_id=None (every caller before this parameter existed) reads the
    domain/env exactly as before, from Settings()/env.sh. Set, it reads
    that account's own tenant_configs row instead, and overlays the
    matching SOURCE_*/MIGRATION_DB env vars onto the child process -- seed_
    sandbox.py itself reads some of these straight from os.environ rather
    than through a Settings object (see data-generator/seed_sandbox.py),
    so the override has to happen at the environment, not just the domain
    check below.
    """
    from config import Settings

    st = Settings(account_id=account_id)
    domain = (st.source_domain or "").strip().lower()
    typed = (body.get("confirm_domain") or "").strip().lower()

    if not domain:
        return [], {}, "set the source domain in step 2 first"
    if not typed:
        return [], {}, f"type the source domain ({domain}) to confirm"
    if typed != domain:
        # The single most dangerous slip is aiming this at the target tenant,
        # or at a real one. An exact match is the only accepted answer.
        target = (st.target_domain or "").strip().lower()
        extra = (" — that is the TARGET domain, which must never be seeded"
                 if typed and typed == target else "")
        return [], {}, f"{typed!r} does not match the source domain {domain!r}{extra}"

    protected = [d.strip().lower()
                 for d in os.getenv("PROTECTED_DOMAINS", "").split(",") if d.strip()]
    if domain in protected:
        return [], {}, f"{domain} is listed in PROTECTED_DOMAINS"

    scale = (body.get("scale") or "medium").strip().lower()
    if scale not in SEED_SCALES:
        return [], {}, f"unknown scale {scale!r}"

    argv = [PY, "seed_sandbox.py", "--confirm-domain", domain, "--scale", scale]
    # --yes skips the seeder's interactive "long run?" prompt. In this subprocess
    # stdin is inherited (or DEVNULL, see Job.start), so input() would block
    # forever and the run would seed nothing. The typed-domain gate above is the
    # safety this replaces; the CLI keeps its prompt for terminal use.
    argv.append("--yes")
    if body.get("create_users"):
        argv.append("--create-users")
    if body.get("all_users"):
        # Seeds every account that already exists in the tenant -- the real
        # headcount via the Directory API, not a fixed 5. seed_sandbox.py
        # itself refuses to combine this with --users/--fit-to-licenses;
        # neither is exposed through this endpoint, so nothing more to guard.
        argv.append("--all-users")
    if body.get("reset"):
        argv.append("--reset")
    target_gb = body.get("target_gb_per_user")
    if target_gb:
        try:
            argv += ["--target-gb-per-user", str(float(target_gb))]
        except (TypeError, ValueError):
            return [], {}, "target_gb_per_user must be a number"

    env = gcloud_env()
    # Set here rather than asking the operator to export it: the value carries
    # no judgement, the typed domain above is what actually gates this.
    env["SANDBOX_MODE"] = "true"
    if account_id is not None:
        env.update(SOURCE_DOMAIN=st.source_domain, SOURCE_ADMIN=st.source_admin,
                   SOURCE_SA_KEY=st.source_sa_key, MIGRATION_DB=st.db_path)
    return argv, env, ""


def reset_target_argv(body: dict, account_id: int | None = None) -> tuple[list[str], dict, str]:
    """
    Build the reset_target command, or return why it must not run.

    Mirrors seed_argv exactly, pointed at the other tenant: reset_target.py's
    own guard (assert_sandbox) needs SANDBOX_MODE=true and an exact,
    case-insensitive match on TARGET_DOMAIN, and refuses outright if target
    and source are ever the same domain. The typed-domain check here is a
    convenience that fails fast with a specific message; the guard that
    actually matters runs again inside reset_target.py itself regardless.
    """
    from config import Settings

    st = Settings(account_id=account_id)
    domain = (st.target_domain or "").strip().lower()
    typed = (body.get("confirm_domain") or "").strip().lower()

    if not domain:
        return [], {}, "set the target domain in step 2 first"
    if not typed:
        return [], {}, f"type the target domain ({domain}) to confirm"
    if typed != domain:
        source = (st.source_domain or "").strip().lower()
        extra = (" — that is the SOURCE domain, which this would never touch, "
                "but it is also not the target" if typed and typed == source else "")
        return [], {}, f"{typed!r} does not match the target domain {domain!r}{extra}"

    protected = [d.strip().lower()
                for d in os.getenv("PROTECTED_DOMAINS", "").split(",") if d.strip()]
    if domain in protected:
        return [], {}, f"{domain} is listed in PROTECTED_DOMAINS"

    argv = [PY, "reset_target.py", "--confirm-domain", domain, "--yes"]
    # Optional and additive: omitting it keeps today's full-wipe default
    # (reset_target.py's own --services default is all four services), so
    # this never changes behavior for a caller that does not know about it.
    services = body.get("services")
    if services:
        argv += ["--services", services if isinstance(services, str) else ",".join(services)]
    env = gcloud_env()
    env["SANDBOX_MODE"] = "true"
    if account_id is not None:
        env.update(TARGET_DOMAIN=st.target_domain, TARGET_ADMIN=st.target_admin,
                   TARGET_SA_KEY=st.target_sa_key, MIGRATION_DB=st.db_path)
    return argv, env, ""


def gcloud_env() -> dict:
    """
    Child-process environment with gcloud reachable.

    setup.sh calls `command -v gcloud`. This server is typically launched
    without an interactive shell, so it does not inherit the PATH entry the
    SDK installer added to the user's profile -- setup.sh would fail with
    "gcloud not installed" on a machine where gcloud plainly works.
    """
    env = dict(os.environ)
    try:
        from wizard import find_gcloud

        path, _how = find_gcloud()
        if path:
            env["PATH"] = os.path.dirname(path) + os.pathsep + env.get("PATH", "")
            env["GCLOUD_BIN"] = path
    except Exception:  # noqa: BLE001 - never let discovery break a run
        pass
    return env


def dwd_payload() -> dict:
    """
    The exact Client ID and scope line to paste into each Admin Console.

    This is the one step no tool can perform, so the least a tool can do is
    remove every opportunity to get it wrong: the strings are produced from
    the same `config.py` functions the engine authenticates with, so they
    cannot drift from what is actually requested at runtime.
    """
    from config import Settings, TRANSFER_MODES, source_scopes, target_scopes

    def _full_union(base: "Settings", fn) -> list[str]:
        """Every scope `fn` could ever ask for, across every toggle this
        operator might flip on later -- the "one line that never needs
        re-pasting" answer, not just what today's settings happen to need.
        The Admin Console replaces the whole scope line on every edit, and
        each edit re-triggers propagation delay for the entire grant (seen
        live: ~2 min typical, up to 30), so a line that already covers a
        feature you turn on next week is strictly better than one you have
        to keep re-pasting."""
        import dataclasses

        scopes: set[str] = set()
        for mode in TRANSFER_MODES:
            for gmail in (False, True):
                for chat in (False, True):
                    for chat_mode in ("direct", "import"):
                        for contacts in (False, True):
                            for tasks in (False, True):
                                for sso in (False, True):
                                    for cal_acls in (False, True):
                                        s = dataclasses.replace(
                                            base, transfer_mode=mode,
                                            migrate_gmail_settings=gmail,
                                            migrate_chat=chat,
                                            chat_space_mode=chat_mode,
                                            migrate_contacts=contacts,
                                            migrate_tasks=tasks,
                                            migrate_sso=sso,
                                            migrate_calendar_acls=cal_acls,
                                        )
                                        scopes.update(fn(s))
        return sorted(scopes)

    st = Settings()
    out = {"tenants": []}
    for side, key_path, scopes, admin, domain in (
        ("source", st.source_sa_key, source_scopes(st), st.source_admin, st.source_domain),
        ("target", st.target_sa_key, target_scopes(st), st.target_admin, st.target_domain),
    ):
        client_id = ""
        try:
            with open(key_path, encoding="utf-8") as fh:
                client_id = json.load(fh).get("client_id", "")
        except Exception:  # noqa: BLE001 - absent key is a normal early state
            pass
        out["tenants"].append({
            "side": side, "domain": domain, "admin": admin,
            "client_id": client_id,
            "scopes": ",".join(scopes),
            "scope_list": scopes,
        })

    # "MIGRATE SOURCE" / "MIGRATE TARGET" -- the full union across every
    # transfer mode and optional-feature toggle, not just whichever ones are
    # on right now. This is the exact line to paste once and never revisit,
    # matched to the copy-paste blocks already worked out by hand for this
    # tenant pair; the per-tenant `scopes` field above stays as the narrower
    # "what today's settings actually need" answer.
    try:
        out["migrate_source_full"] = _full_union(st, source_scopes)
        out["migrate_target_full"] = _full_union(st, target_scopes)
    except Exception:  # noqa: BLE001 - never break /api/dwd over this extra
        out["migrate_source_full"] = []
        out["migrate_target_full"] = []

    # Provisioning a target account needs admin.directory.user (write), which
    # the migration target set deliberately does not carry. The Admin Console
    # REPLACES the scope line, so a target you intend to provision needs one
    # line carrying both -- paste the migration set alone and provisioning
    # fails with unauthorized_client, exactly as it just did.
    try:
        from config import DIRECTORY_WRITE_SCOPE
        combined = sorted(set(target_scopes(st)) | {DIRECTORY_WRITE_SCOPE})
        out["target_provision"] = {
            "scopes": ",".join(combined),
            "scope_list": combined,
        }
    except Exception:  # noqa: BLE001 - provisioning is optional; never break /api/dwd
        out["target_provision"] = {}

    # Seeding writes; migrating (on the source) reads. The Admin Console's
    # delegation editor REPLACES the scope line rather than adding to it, so a
    # source tenant you intend to seed as well as migrate needs one line
    # carrying both -- paste the migration set alone and seeding fails with
    # unauthorized_client, paste the seed set alone and the migration does.
    try:
        import provision

        data_gen = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "data-generator")
        if data_gen not in sys.path:
            sys.path.insert(0, data_gen)
        from seed_sandbox import (
            DIRECTORY_READONLY_SCOPE, REPORTS_SCOPE, SEED_SCOPES,
            _resolve_key_path,
        )

        # Every optional seed feature's scope, unioned -- --create-users,
        # --fit-to-licenses, and the default/--all-users live Directory
        # discovery all need a scope beyond the base SEED_SCOPES write set.
        # One line covering all of them means never having to work out
        # afterward which feature's unauthorized_client came from a scope
        # that simply was not asked for yet.
        full = sorted(set(SEED_SCOPES) | {
            provision.DIRECTORY_WRITE_SCOPE, REPORTS_SCOPE,
            DIRECTORY_READONLY_SCOPE,
        })
        combined = sorted(set(full) | set(source_scopes(st)))

        # Same client-id resolution seed_sandbox.py itself uses (SEED_SA_KEY,
        # falling back to the source key) -- so this names the exact Admin
        # Console entry the scopes above actually need to land on, the same
        # way the tenants loop above does for source/target.
        seed_client_id = ""
        try:
            with open(_resolve_key_path(st), encoding="utf-8") as fh:
                seed_client_id = json.load(fh).get("client_id", "")
        except Exception:  # noqa: BLE001 - absent key is a normal early state
            pass
        # True only when no dedicated seed-sa.json exists yet and this
        # would-be seed key is actually the read-only source key -- pasting
        # the line below onto it grants it write access, which is exactly
        # the guarantee a separate SEED_SA_KEY exists to avoid.
        shares_source_key = (seed_client_id
                             and seed_client_id == out["tenants"][0]["client_id"])

        out["seed"] = {
            "client_id": seed_client_id,
            "shares_source_key": bool(shares_source_key),
            "scopes": ",".join(full),
            "scope_list": full,
            "combined": ",".join(combined),
            "combined_list": combined,
        }
    except Exception:  # noqa: BLE001 - the seeder is optional; never break /api/dwd
        out["seed"] = {}
    return out


def scope_diagnosis(tenant: str) -> dict:
    """
    Which scope, exactly, is not authorised -- for a tenant whose combined
    request fails.

    A single unauthorised (or not-yet-propagated) scope fails the *entire*
    combined token request with the same generic unauthorized_client error,
    whatever else in that request is fine. Diagnosing which one requires
    minting a separate token per scope and checking each in isolation --
    this was done live, more than once, over SSH by hand before this
    existed. This is that same bisection, as a button instead of a manual
    session.

    Deliberately explicit-trigger only (POST /api/scope_diagnosis), never
    on a poll path: bisecting N scopes is N+1 live token mints against
    Google, the same "no live API call on a poll loop" rule
    webui_spa.py's module docstring states for exactly this reason.
    """
    from config import Settings, source_scopes, target_scopes

    st = Settings()
    if tenant == "source":
        key, admin = st.source_sa_key, st.source_admin
        scopes, domain = source_scopes(st), st.source_domain
    elif tenant == "target":
        key, admin = st.target_sa_key, st.target_admin
        scopes, domain = target_scopes(st), st.target_domain
    else:
        return {"tenant": tenant, "error": "tenant must be 'source' or 'target'"}

    result: dict = {"tenant": tenant, "domain": domain, "admin": admin,
                    "combined_ok": False, "error": "", "scopes": []}
    if not admin:
        result["error"] = f"no admin configured for {tenant} (set it in step 2)"
        return result
    if not (key and os.path.isfile(key)):
        result["error"] = f"no key file for {tenant} yet"
        return result

    def _try(scope_list: list[str]) -> tuple[bool, str]:
        try:
            from google.auth.transport.requests import Request
            from google.oauth2 import service_account

            creds = service_account.Credentials.from_service_account_file(
                key, scopes=scope_list).with_subject(admin)
            creds.refresh(Request())
            return True, ""
        except Exception as exc:  # noqa: BLE001 - reporting the failure is the point
            return False, str(exc)[:200]

    ok, err = _try(scopes)
    result["combined_ok"] = ok
    if ok:
        result["scopes"] = [{"scope": s, "ok": True} for s in scopes]
        return result

    # Only bisect on failure -- a passing combined check already answers
    # the question for every scope in it, at 1/N the cost.
    result["error"] = err
    for s in scopes:
        s_ok, s_err = _try([s])
        result["scopes"].append({"scope": s, "ok": s_ok, "error": "" if s_ok else s_err})
    return result


# ----------------------------------------------------------------------
# Status snapshots
#
# Detecting step 5 means running a real preflight -- minting a token per user
# against both tenants. On a five-user pair that measured **9.5 seconds**, and
# the page was polling every 6, so requests piled up faster than they
# completed: concurrent preflight subprocesses on a 2-core VPS, and any poll
# that errored under the load replaced the whole panel with an error box,
# taking whatever was half-typed in the form with it.
#
# So the poll never computes. A background thread recomputes on a slow cycle
# and every request returns the latest finished snapshot immediately.
# Delegation does not change second to second; a stale-by-30s answer is worth
# far more than a UI that stalls.
# ----------------------------------------------------------------------
STATUS_TTL = 30.0

_snap: dict = {"data": None, "at": 0.0}
_snap_lock = threading.Lock()
_snap_busy = threading.Event()


def _compute_status() -> dict:
    if State is None or build_steps is None:
        return {"error": "wizard.py could not be imported; run from the repo root"}
    return _status_uncached()


def _refresh_snapshot() -> None:
    try:
        data = _compute_status()
        with _snap_lock:
            _snap["data"], _snap["at"] = data, time.time()
    finally:
        _snap_busy.clear()


def check_step(n: int) -> dict:
    """
    Re-evaluate one step, now, and say whether it is satisfied.

    Deliberately synchronous and uncached: this is the operator asking "did
    what I just did work?", and the answer has to reflect the last thirty
    seconds, not a snapshot taken before they went to the Admin Console.
    """
    invalidate_status()
    try:
        data = _compute_status()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"could not evaluate: {exc}"}
    if data.get("error"):
        return {"ok": False, "error": data["error"]}

    step = next((s for s in data["steps"] if s["n"] == n), None)
    if not step:
        return {"ok": False, "error": f"no step {n}"}

    state = step["state"]
    satisfied = state in ("done", "skip")
    return {
        "ok": satisfied,
        "state": state,
        "title": step["title"],
        "detail": step.get("note") or "",
        "msg": ("satisfied" if state == "done" else
                "not needed for this run" if state == "skip" else
                "still outstanding"),
    }


def invalidate_status() -> None:
    """
    Drop the cached snapshot so the next poll recomputes.

    Anything that changes what the steps *mean* has to call this. Without it a
    configuration change is invisible for up to STATUS_TTL seconds -- switching
    run mode appeared to do nothing at all, because the answer had already been
    computed under the old setting and was simply replayed.
    """
    with _snap_lock:
        _snap["at"] = 0.0


def status_payload() -> dict:
    """The cached snapshot. Never blocks on a preflight."""
    with _snap_lock:
        data, age = _snap["data"], time.time() - _snap["at"]

    if data is None:
        # First call only: nothing to show yet, so pay for it once.
        _snap_busy.set()
        _refresh_snapshot()
        with _snap_lock:
            return dict(_snap["data"], stale=False)

    if age > STATUS_TTL and not _snap_busy.is_set():
        _snap_busy.set()
        threading.Thread(target=_refresh_snapshot, daemon=True).start()

    return dict(data, stale=age > STATUS_TTL, age=round(age, 1))


def _RUN_MODES() -> dict:
    from wizard import RUN_MODES

    return {k: {"label": v["label"], "blurb": v["blurb"],
                "setup": v.get("setup", []),
                "requires": v.get("requires", []),
                "runs": v.get("runs", [])}
            for k, v in RUN_MODES.items()}


def _status_uncached() -> dict:
    st = State()
    steps = build_steps(st)
    ok, failed, users_done = st.migration_progress()
    return {
        "env": {k: st.env.get(k, "") for k in
                ("SOURCE_DOMAIN", "TARGET_DOMAIN", "AUTH_MODE", "TRANSFER_MODE")},
        "steps": [{"n": s["n"], "title": s["title"], "state": s["state"],
                   "note": s.get("note", ""),
                   # The wizard UI shows one step at a time, so it needs the
                   # explanatory text too -- not just the status line.
                   "help": s.get("help", []),
                   "auto": s.get("auto", ""),
                   "manual": bool(s.get("manual")),
                   "skipped": bool(s.get("skipped")),
                   "actions": [a for a in STEP_ACTIONS.get(s["n"], []) if a in ACTIONS]}
                  for s in steps],
        # Skipped steps are neither done nor outstanding, so counting them in
        # the total would leave every run stuck below 100%.
        "done": sum(1 for s in steps if s["state"] == "done"),
        "total": sum(1 for s in steps if s["state"] != "skip"),
        "migrated": ok, "failed": failed,
        "users_done": users_done, "users_total": st.identities_loaded(),
    }


# ----------------------------------------------------------------------
# Operator dashboard — the same data the TUI reads, served to the page.
# migration.db is opened read-only (mode=ro), exactly like tui.py does.
# The only mutation state here is the launch toggles below.
# ----------------------------------------------------------------------

# dry-run + which services "Migrate"/"Delta" run. Mirrors the TUI's
# d/t/s keys so the web buttons behave identically to the keyboard.
#
# contacts/tasks off by default like chat: each widens the OAuth grant, and a
# scope the Admin Console has not authorised fails every call outright, so
# enabling one must be a deliberate click, never a default.
_RUN_STATE: dict = {
    "dry_run": False,
    "services": {"drive": True, "gmail": True, "calendar": True,
                "chat": False, "contacts": False, "tasks": False},
}

# Actions whose argv follow the launch toggles (everything else uses its
# fixed ACTIONS argv verbatim).
_LAUNCH_KEYS = ("migrate", "delta")

# Actions that read the toggles through the environment rather than argv.
# main.py's migrate/delta infer MIGRATE_CHAT/CONTACTS/TASKS from --services,
# so a checkbox reaches them through _action_argv above. phases.py does not:
# its per-phase gate reads those settings straight from the environment
# regardless of --phase, so a toggle only reaches it if this run explicitly
# sets or clears the variable -- otherwise a MIGRATE_CHAT=true left in env.sh
# from an earlier session would run Chat with no visible checkbox for it.
_PHASE_GATED_ACTIONS = ("phased_migrate", "phased_count_only")


def _service_env() -> dict:
    """gcloud_env(), plus the per-user service toggles made explicit."""
    env = gcloud_env()
    for key, flag in (("chat", "MIGRATE_CHAT"), ("contacts", "MIGRATE_CONTACTS"),
                      ("tasks", "MIGRATE_TASKS")):
        env[flag] = "true" if _RUN_STATE["services"].get(key) else "false"
    return env


def _db_conn():
    """A read-only connection to the resume ledger, or None."""
    import sqlite3

    from config import Settings

    path = Settings().db_path
    if not os.path.exists(path):
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _serialize_snapshot(snap) -> dict:
    return {
        "users": [{
            "source": u.source, "target": u.target, "status": u.status,
            "drive_done": u.drive_done, "drive_failed": u.drive_failed,
            "drive_skipped": u.drive_skipped,
            "mail_done": u.mail_done, "mail_failed": u.mail_failed,
            "mail_skipped": u.mail_skipped,
            "cal_done": u.cal_done, "cal_failed": u.cal_failed,
            "acl_failed": u.acl_failed, "bytes_moved": u.bytes_moved,
            "exp_drive": u.exp_drive, "exp_mail": u.exp_mail,
            "gb_today": u.gb_today,
            "done": u.done, "failed": u.failed,
            "expected": u.expected, "fraction": u.fraction,
        } for u in snap.users],
        "failures": snap.failures,
        "totals": snap.totals,
        "collected_at": snap.collected_at,
        "error": snap.error,
    }


def snapshot_payload() -> dict:
    """The TUI snapshot plus the launch toggles, for the page's poll loop."""
    import sqlite3

    import tui
    from config import Settings

    conn = _db_conn()
    if conn is None:
        return {"error": "no database yet — run init-db or create identities.csv",
                "toggles": dict(_RUN_STATE), "snapshot": None}
    try:
        snap = tui.collect_snapshot(conn, Settings().effective_upload_cap())
    except sqlite3.Error as exc:
        return {"error": f"db read error: {exc}",
                "toggles": dict(_RUN_STATE), "snapshot": None}
    finally:
        conn.close()
    return {"error": "", "toggles": dict(_RUN_STATE),
            "snapshot": _serialize_snapshot(snap)}


def spa_users_payload() -> dict:
    """User[] for migration-webui, read-only from the ledger. See webui_spa.py."""
    import sqlite3

    import webui_spa
    from config import Settings

    conn = _db_conn()
    if conn is None:
        return {"error": "no database yet — run init-db or create identities.csv",
               "users": []}
    try:
        return {"error": "", "users": webui_spa.users_payload(
            conn, Settings().effective_upload_cap())}
    except sqlite3.Error as exc:
        return {"error": f"db read error: {exc}", "users": []}
    finally:
        conn.close()


def _ledger_progress_fraction() -> float | None:
    """The same items_done/items_expected fraction the header progress bar
    and snapshot_payload() already compute from the ledger -- reused here
    rather than re-derived, since tui.collect_snapshot() is the one place
    that already resolves partial per-service completion into one number."""
    import sqlite3

    from config import Settings

    conn = _db_conn()
    if conn is None:
        return None
    try:
        import tui

        totals = tui.collect_snapshot(conn, Settings().effective_upload_cap()).totals
        return totals.get("fraction")
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _job_progress(name: str, lines: list[str], elapsed: float
                  ) -> tuple[int | None, int | None]:
    """(progressPct, etaSeconds) for the running job, or (None, None) when
    there is no reliable source for either -- guessing a percentage from
    nothing but log lines is worse than showing none at all.

    ETA is extrapolated linearly from elapsed time and fraction complete:
    it assumes the remaining work costs the same, per item, as the work
    already done. That is wrong the instant one user has 10x another's
    mailbox, but it is the same assumption every "time remaining" bar
    anyone has ever used makes, and it gets more accurate as fraction
    grows -- which is exactly when an operator starts actually watching it.
    """
    if name == "seed":
        pct = _seed_progress_pct(lines)
    elif name in ("migrate", "delta", "discover"):
        frac = _ledger_progress_fraction()
        pct = round(frac * 100) if frac is not None else None
    else:
        pct = None
    if pct is None or pct <= 0 or elapsed <= 0:
        return pct, None
    eta = round(elapsed * (100 - pct) / pct)
    return pct, eta


def _job_activity_entry() -> dict | None:
    """
    A synthetic ActivityEvent for the one background job, if any.

    audit_log-backed activity is structurally blind to seed_sandbox.py,
    reset_target.py, and deploy_remote.py: none of them ever write a row
    to it (seed_sandbox.py calls Google's APIs directly; see
    webui_spa.py's module docstring on why nothing here makes a live API
    call of its own). Without this, "what is happening right now" lived in
    two places depending on which kind of work it was -- the ledger-backed
    feed for a migration, and only JobProgress's own panel for everything
    else -- and a seed run looked like nothing was happening at all here.

    Reshapes the same live Job state JobProgress already streams (see
    Job.snapshot()) into one row at the front of the list, so it is not a
    second thing to check.
    """
    snap = JOB.snapshot()
    if not snap["running"]:
        ext = _external_job_snapshot()
        if ext is not None:
            snap = ext
    if not snap["name"]:
        return None
    last = next((ln for ln in reversed(snap["lines"]) if ln.strip()), "")
    if snap["running"]:
        status, action = "in_progress", f"{snap['name']} running"
    elif snap["rc"] == 0:
        status, action = "completed", f"{snap['name']} finished"
    else:
        status, action = "needs_attention", f"{snap['name']} stopped (exit {snap['rc']})"
    return {
        "id": f"job:{snap['name']}:{snap['total']}",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "user": "System",
        "action": action,
        "status": status,
        "details": last[:200] or None,
        # See Job.snapshot()/_job_progress(): percentage can still be
        # meaningful after a job stops (its final figure), an ETA cannot.
        "progressPct": snap["progressPct"],
        "etaSeconds": snap["etaSeconds"],
    }


def spa_activity_payload() -> dict:
    import sqlite3

    import webui_spa

    job_entry = _job_activity_entry()
    conn = _db_conn()
    if conn is None:
        # Still surfaced: a job can run before init-db has ever been done
        # (a seed-only session, say), and the operator should see it -- but
        # the "no database yet" note is real diagnostic information too
        # (see ActivityFeed.tsx's error banner), so it is kept, not dropped
        # just because there also happens to be a job to show.
        return {"error": "no database yet", "activity": [job_entry] if job_entry else []}
    try:
        activity = webui_spa.activity_payload(conn)
        return {"error": "", "activity": ([job_entry] if job_entry else []) + activity}
    except sqlite3.Error as exc:
        return {"error": f"db read error: {exc}",
               "activity": [job_entry] if job_entry else []}
    finally:
        conn.close()


def spa_metrics_payload() -> dict:
    import sqlite3

    import tui
    import webui_spa
    from config import Settings

    settings = Settings()
    conn = _db_conn()
    totals: dict = {}
    if conn is not None:
        try:
            totals = tui.collect_snapshot(conn, settings.effective_upload_cap()).totals
        except sqlite3.Error:
            totals = {}
        finally:
            conn.close()
    return webui_spa.metrics_payload(settings, settings.effective_upload_cap(), totals)


def spa_stages_payload() -> dict:
    import sqlite3

    import webui_spa
    from config import Settings

    conn = _db_conn()
    if conn is None:
        return {"error": "no database yet", "stages": []}
    try:
        return {"error": "", "stages": webui_spa.stages_payload(
            conn, Settings(), JOB.finished)}
    except sqlite3.Error as exc:
        return {"error": f"db read error: {exc}", "stages": []}
    finally:
        conn.close()


def spa_verification_payload() -> dict:
    import sqlite3

    import webui_spa
    from config import Settings

    conn = _db_conn()
    if conn is None:
        return {"error": "no database yet", "verification": []}
    try:
        return {"error": "", "verification": webui_spa.verification_payload(
            conn, Settings())}
    except sqlite3.Error as exc:
        return {"error": f"db read error: {exc}", "verification": []}
    finally:
        conn.close()


def spa_report_payload() -> dict:
    import sqlite3

    import webui_spa
    from config import Settings

    conn = _db_conn()
    if conn is None:
        return {"error": "no database yet", "report": None}
    try:
        # The most recently run job's own timing, not a scan over audit_log --
        # see webui_spa.report_payload's docstring for why.
        report = webui_spa.report_payload(conn, Settings(), JOB.started, JOB.finished)
        return {"error": "", "report": report}
    except sqlite3.Error as exc:
        return {"error": f"db read error: {exc}", "report": None}
    finally:
        conn.close()


def scope_payload() -> dict:
    """The scope matrix plus discovered volume, rendered server-side."""
    import scope as scope_mod

    conn = _db_conn()
    volume = {}
    if conn is not None:
        try:

            class _Shim:
                conn = conn

            volume = scope_mod.planned_volume(_Shim())
        except Exception:  # noqa: BLE001
            volume = {}
        finally:
            conn.close()

    tally = scope_mod.counts()
    lines = [
        "MIGRATION SCOPE — what this engine moves",
        "",
        "  [+] FULL     high fidelity",
        "  [~] PARTIAL  migrated with a named fidelity loss",
        "  [-] NONE     not migrated by this engine",
        "",
    ]
    for svc in scope_mod.SERVICES:
        t = tally.get(svc, {})
        lines.append(f"  {svc:<10} full {t.get('FULL', 0):>2}   "
                     f"partial {t.get('PARTIAL', 0):>2}   none {t.get('NONE', 0):>2}")
    if volume.get("users"):
        lines += ["",
                  f"  Discovered volume: {volume['files']:,} files, "
                  f"{volume['folders']:,} folders, {volume['native']:,} native docs, "
                  f"{_human_bytes(volume['bytes'])}, {volume['messages']:,} messages, "
                  f"max depth {volume['max_depth']}"]
    else:
        lines += ["", "  Discovered volume: run 'discover' to populate."]
    lines += scope_mod.as_text(width=100)
    return {"lines": lines, "totals": tally, "volume": volume}


def _human_bytes(n: int) -> str:
    n = int(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1024 or unit == "PB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} PB"


def logs_payload() -> dict:
    """Tail of the engine's own log file (what main.py writes)."""
    from config import Settings

    path = Settings().log_file
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return {"path": path, "lines": ["(no log file yet)"]}
    return {"path": path, "lines": lines[-600:]}


# ----------------------------------------------------------------------
# Groq "active log" — a live diagnostic panel for benchmarking and error
# reporting.
#
# The migration's own log tail answers "what happened", but reading 600
# lines of it to answer "is it going OK, and if not why" is exactly the
# chore an LLM is good at. This exposes a narrow seam for that: the key is
# stored in env.sh like every other setting (case-preserved — the API key
# is not a domain to lowercase), and the panel sends the current log tail
# plus the run's headline metrics to Groq and renders the summary back.
# Nothing here runs commands and nothing the browser sends is executed.
# ----------------------------------------------------------------------
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")


def groq_api_key() -> str:
    from wizard import load_env

    return load_env(ENV_PATH).get("GROQ_API_KEY", "")


def save_groq_key(key: str) -> str:
    """Persist the Groq API key to env.sh, case-preserved."""
    key = (key or "").strip()
    if not key:
        return "Groq API key is required"
    write_config_raw({"GROQ_API_KEY": key})
    return ""


def _groq_analyze_log(tail: str, prompt: str, key: str) -> tuple[str, str]:
    """
    Send the log tail to Groq and return (markdown_summary, error).
    Uses urllib only — this file is deliberately stdlib-only.
    """
    url = "https://api.groq.com/openai/v1/chat/completions"
    system = (
        "You are a migration engineer's diagnostic assistant. The user runs a "
        "Google Workspace data migration. You are given the tail of its log "
        "and the run's headline metrics. Produce a concise, actionable "
        "summary in Markdown with three sections if any content matches: "
        "**Status** (one or two lines: healthy or not, and why), **Errors** "
        "(every distinct FAILED/WARNING line, deduplicated, with the count "
        "and the one-line cause), and **Benchmark** (rates, latencies, "
        "progress). If there is nothing notable, say so in one line. Do not "
        "invent failures. Quote the actual log lines when you cite something."
    )
    body = json.dumps({
        "model": GROQ_MODEL,
        "temperature": 0,
        "max_tokens": 700,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": f"{prompt}\n\n---- LOG TAIL ----\n{tail}"},
        ],
    }).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        return "", f"Groq API error {exc.code}: {detail or exc.reason}"
    except Exception as exc:  # noqa: BLE001 - network/urllib failures
        return "", f"could not reach Groq: {exc}"
    try:
        return data["choices"][0]["message"]["content"], ""
    except (KeyError, IndexError, TypeError):
        return "", f"unexpected Groq response: {str(data)[:200]}"


def _groq_run_summary() -> str:
    """The headline numbers the panel always sends alongside the log tail."""
    parts = []
    try:
        payload = snapshot_payload()
        t = (payload.get("snapshot") or {}).get("totals") or {}
        parts.append(
            f"- Progress: {t.get('items_done', 0)}/{t.get('items_expected', '?')} "
            f"items, {t.get('users_done', 0)}/{t.get('users', 0)} users, "
            f"{t.get('items_failed', 0)} failed, {t.get('bytes_moved', 0)} bytes moved"
        )
    except Exception:  # noqa: BLE001 - a metrics hiccup must not kill the panel
        pass
    try:
        import metrics

        s = metrics.METRICS.snapshot()
        if s.get("calls"):
            parts.append(
                f"- API: {s['calls']:,} calls, {s['requests_per_sec']:.2f} req/s "
                f"across {s['workers']} workers, p50 {s['p50'] * 1000:.0f}ms "
                f"p95 {s['p95'] * 1000:.0f}ms p99 {s['p99'] * 1000:.0f}ms, "
                f"{s['retries']:,} retries, {s['failures']:,} failures"
            )
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(parts)



def set_toggles(body: dict) -> dict:
    """Apply the launch toggles sent by the toolbar."""
    dry = body.get("dry_run")
    if dry is not None:
        _RUN_STATE["dry_run"] = bool(dry)
    svcs = body.get("services")
    if isinstance(svcs, dict):
        for k in _RUN_STATE["services"]:
            if k in svcs:
                _RUN_STATE["services"][k] = bool(svcs[k])
    return {"ok": True, "toggles": dict(_RUN_STATE)}


def _action_argv(name: str) -> list:
    """Fixed argv for most actions; migrate/delta follow the launch toggles
    (dry-run + selected services), exactly like the TUI's m/x keys."""
    spec = ACTIONS[name]
    if name not in _LAUNCH_KEYS:
        return list(spec["argv"])
    chosen = [k for k, v in _RUN_STATE["services"].items() if v]
    argv = [PY, "main.py", "migrate" if name == "migrate" else "delta"]
    if chosen:
        argv += ["--services", ",".join(chosen)]
    if name == "delta":
        argv += ["--days", "2"]
    if _RUN_STATE["dry_run"]:
        argv.insert(2, "--dry-run")
    return argv


# ----------------------------------------------------------------------
PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Workspace Migration</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0f1115;--panel:#171a21;--line:#262b36;--fg:#e6e9ef;--dim:#8b93a7;
--ok:#3fb950;--warn:#d29922;--bad:#f85149;--accent:#58a6ff;--code:#0a0c10;}
@media(prefers-color-scheme:light){:root{--bg:#f6f7f9;--panel:#fff;--line:#e3e6ea;
--fg:#1c2128;--dim:#6a737d;--accent:#0969da;--code:#f0f2f5;}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.6 ui-sans-serif,-apple-system,Segoe UI,Roboto,sans-serif}
header{padding:14px 22px;border-bottom:1px solid var(--line);display:flex;
gap:14px;align-items:center;flex-wrap:wrap}
h1{font-size:15px;margin:0;font-weight:600}
.muted{color:var(--dim);font-size:12px}
.wrap{display:grid;grid-template-columns:270px 1fr;gap:0;min-height:calc(100vh - 51px)}
@media(max-width:900px){.wrap{grid-template-columns:1fr}}

/* ---- left rail: every step, always visible, so you know where you are ---- */
.rail{border-right:1px solid var(--line);padding:16px 0;background:var(--panel)}
@media(max-width:900px){.rail{border-right:0;border-bottom:1px solid var(--line)}}
.rstep{display:flex;gap:10px;padding:8px 16px;cursor:pointer;border-left:3px solid transparent;
align-items:flex-start}
.rstep:hover{background:var(--bg)}
.rstep.cur{border-left-color:var(--accent);background:var(--bg)}
.rstep .m{width:18px;height:18px;flex:none;border-radius:99px;font-size:11px;
display:grid;place-items:center;border:1px solid var(--line);margin-top:2px}
.rstep.done .m{background:var(--ok);border-color:var(--ok);color:#04260d;font-weight:700}
.rstep.manual .m{border-color:var(--warn);color:var(--warn);font-weight:700}
.rstep.active .m{border-color:var(--accent);color:var(--accent)}
.rstep.skip{opacity:.5} .rstep.skip .m{border-style:dashed}
.rstep .t{font-size:13px;line-height:1.35}
.rstep .sub{color:var(--dim);font-size:11px;margin-top:1px}
.rail .bar{margin:4px 16px 14px}

/* ---- main panel: exactly one step ---- */
.main{padding:22px 26px;max-width:900px}
.eyebrow{color:var(--dim);font-size:12px;letter-spacing:.06em;text-transform:uppercase}
h2.title{font-size:22px;margin:4px 0 6px;font-weight:600}
.pill{display:inline-block;font-size:11px;padding:2px 9px;border-radius:99px;
border:1px solid var(--line);color:var(--dim)}
.pill.done{color:var(--ok);border-color:var(--ok)}
.pill.manual{color:var(--warn);border-color:var(--warn)}
.pill.active{color:var(--accent);border-color:var(--accent)}
.note{margin:10px 0 0;color:var(--dim)}
.help{margin:16px 0;white-space:pre-wrap;line-height:1.65}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
padding:16px;margin:16px 0}
.card h3{font-size:12px;text-transform:uppercase;letter-spacing:.08em;
color:var(--dim);margin:0 0 10px;font-weight:600}
button{background:var(--panel);color:var(--fg);border:1px solid var(--line);
border-radius:7px;padding:9px 12px;cursor:pointer;font-size:13px;text-align:left}
button:hover:not(:disabled){border-color:var(--accent)}
button:disabled{opacity:.45;cursor:not-allowed}
button.primary{background:var(--accent);border-color:var(--accent);color:#04182e;font-weight:600}
@media(prefers-color-scheme:dark){button.primary{color:#04182e}}
button.danger{border-color:#5c2626}
button.danger:hover:not(:disabled){border-color:var(--bad)}
button small{display:block;color:var(--dim);font-size:11px;margin-top:2px;font-weight:400}
button.primary small{color:#04182e;opacity:.75}
.acts{display:grid;gap:8px;grid-template-columns:repeat(auto-fit,minmax(230px,1fr))}
.grid2{display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(220px,1fr))}
label{display:block;font-size:12px;color:var(--dim)}
input,select{display:block;width:100%;margin-top:4px;background:var(--bg);color:var(--fg);
border:1px solid var(--line);border-radius:6px;padding:8px 10px;font-size:13px}
input:focus,select:focus{outline:none;border-color:var(--accent)}
input[type=checkbox]{width:auto;display:inline-block;margin:0 6px 0 0}
code,pre.copy{background:var(--code);border:1px solid var(--line);border-radius:6px;
font:12px/1.5 ui-monospace,Menlo,monospace}
code{padding:2px 6px}
pre.copy{padding:10px 12px;margin:6px 0;overflow-x:auto;white-space:pre-wrap;
word-break:break-all;position:relative}
.cprow{display:flex;gap:8px;align-items:center;margin-top:10px}
.cprow b{font-size:12px}
.nav{display:flex;gap:10px;margin:22px 0 0;align-items:center;flex-wrap:wrap}
pre.out{background:var(--code);color:#d6deeb;border:1px solid var(--line);border-radius:8px;
padding:12px;margin:0;height:340px;overflow:auto;
font:12px/1.55 ui-monospace,Menlo,monospace;white-space:pre-wrap;word-break:break-word}
.run{display:flex;gap:9px;align-items:center;margin-bottom:10px;flex-wrap:wrap}
.dot{width:8px;height:8px;border-radius:99px;background:var(--dim)}
.dot.on{background:var(--ok);animation:p 1.2s infinite}
@keyframes p{50%{opacity:.35}}
.bar{height:5px;background:var(--line);border-radius:99px;overflow:hidden}
.bar>i{display:block;height:100%;background:var(--accent);transition:width .4s}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:12px}
.stat{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px}
.stat b{display:block;font-size:19px} .stat span{color:var(--dim);font-size:11px}
.warnbox{border-left:3px solid var(--warn);padding:10px 14px;background:var(--panel);
border-radius:0 8px 8px 0;margin:14px 0}
.okbox{border-left:3px solid var(--ok);padding:10px 14px;background:var(--panel);
border-radius:0 8px 8px 0;margin:14px 0}

/* ---- command toolbar: every ACTIONS command, always one click away ---- */
.toolbar{display:flex;gap:6px;align-items:center;flex-wrap:wrap;
padding:10px 22px;border-bottom:1px solid var(--line);background:var(--panel)}
.toolbar button{font-size:12px;padding:6px 10px}
.toolbar .sep{width:1px;height:22px;background:var(--line);margin:0 4px}
.chk{display:inline-flex;align-items:center;gap:4px;font-size:12px;color:var(--dim)}
.chk input{width:auto;margin:0}

/* ---- operator tabs: dashboard / users / failures / scope / logs ---- */
.tabs{display:flex;gap:2px;padding:0 22px;border-bottom:1px solid var(--line);
background:var(--panel);overflow-x:auto}
.tabs button{border:none;border-bottom:2px solid transparent;border-radius:0;
background:none;padding:10px 14px;font-size:13px;color:var(--dim);cursor:pointer}
.tabs button.on{border-bottom-color:var(--accent);color:var(--fg)}
.tabs button:hover{color:var(--fg)}
.view{display:none;padding:18px 22px;max-width:1200px}
.view.on{display:block}
.view h2{font-size:16px;margin:0 0 12px;font-weight:600}
.view table{width:100%;border-collapse:collapse;font-size:13px}
.view th,.view td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}
.view th{color:var(--dim);font-size:11px;text-transform:uppercase;
letter-spacing:.05em;position:sticky;top:0;background:var(--panel)}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.mono{font:12px/1.55 ui-monospace,Menlo,monospace;white-space:pre-wrap;word-break:break-word}
.scroll{max-height:60vh;overflow:auto}
.groq{border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin:10px 0;
  background:var(--panel)}
.groqout{margin-top:8px;padding:10px;border:1px dashed var(--line);border-radius:6px;
  font:12px/1.55 ui-monospace,Menlo,monospace;white-space:pre-wrap;word-break:break-word;
  max-height:40vh;overflow:auto}
.gbar{height:6px;background:var(--line);border-radius:99px;overflow:hidden}
.gbar>i{display:block;height:100%;background:var(--accent);transition:width .4s ease}
.hdprog{display:flex;align-items:center;gap:12px;padding:7px 22px;
border-bottom:1px solid var(--line);background:var(--panel)}
.hdprog .gbar{flex:1;height:8px}
.hdprog b{min-width:44px;text-align:right}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
gap:10px;margin:0 0 14px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px}
.stat b{display:block;font-size:18px} .stat span{color:var(--dim);font-size:11px}
.outwrap{border-top:1px solid var(--line);background:var(--panel);padding:10px 22px}
pre.out{height:230px}
.feedbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:6px}
.feedbar .sp{margin-left:auto}
.feedbar button{padding:5px 9px;font-size:12px}
</style></head><body>
<header>
  <h1>Workspace Migration <span style="font-size:10px;background:var(--accent);color:#04182e;padding:2px 7px;border-radius:99px;font-weight:700">v2.0</span></h1>
  <span class="muted" id="route"></span>
  <span class="muted" id="hostbadge" style="font-family:ui-monospace,monospace;cursor:default"></span>
    <span class="muted" style="margin-left:auto;display:flex;gap:10px;align-items:center"
    title="127.0.0.1 only · SSH tunnel">
    <button onclick="toggleTheme()" style="padding:3px 8px;font-size:11px">☀️ Theme</button>
    <button onclick="toggleVerbose()" style="padding:3px 8px;font-size:11px">💡 Verbose</button>
    <span class="dot" id="dot"></span><b id="jname">idle</b>
    <span id="jmeta"></span></span>
</header>

<div id="connbar" style="display:none;position:sticky;top:0;z-index:50;
  background:var(--bad);color:#fff;padding:7px 14px;font-size:13px">
  Connection to the migration server lost &mdash; the SSH tunnel or webui may
  be down. The page keeps the last known state; nothing new is being read.
  <button onclick="reconnect()" style="margin-left:8px;background:#fff;color:var(--bad);
    border:0;padding:3px 10px;border-radius:4px;font-weight:600">Reconnect</button>
  <span id="connmsg" style="opacity:.85"></span>
</div>

<div class="hdprog">
  <div class="gbar"><i id="progi" style="width:0%"></i></div>
  <b class="muted" id="progpct">0%</b>
  <span class="muted" id="progtxt">no migration yet</span>
</div>

<div style="padding:8px 22px;background:var(--panel);border-bottom:1px solid var(--line);display:flex;gap:10px;align-items:center">
  <input type="text" id="action-search" placeholder="🔍 Search commands & tools (e.g. migrate, preflight, inventory, seed, undo, verify...)" oninput="filterToolbar(this.value)" style="margin:0;flex:1">
</div>

<div class="toolbar" id="tb">Loading commands&hellip;</div>

<div class="tabs" id="tabs">
  <button data-tab="setup" class="on">Setup</button>
  <button data-tab="seed">Seed sandbox</button>
  <button data-tab="deploy">Deploy</button>
  <button data-tab="dashboard">Dashboard</button>
  <button data-tab="users">Users</button>
  <button data-tab="identities">Identities</button>
  <button data-tab="audit">Audit & Inventory</button>
  <button data-tab="maintenance">Maintenance</button>
  <button data-tab="failures">Failures</button>
  <button data-tab="scope">Scope</button>
  <button data-tab="logs">Logs</button>
  <button data-tab="output">Output</button>
</div>

<div class="wrap" id="setup">
  <div class="rail">
    <div class="bar" style="margin-top:0"><i id="pbar" style="width:0"></i></div>
    <div class="muted" style="margin:0 16px 12px" id="pnum">&ndash;</div>
    <div id="rail"></div>
  </div>
  <div class="main" id="main"><div class="muted">Loading&hellip;</div></div>
</div>

<div class="view" id="view-seed"></div>
<div class="view" id="view-deploy"></div>
<div class="view" id="view-dashboard"><div class="muted">Loading&hellip;</div></div>
<div class="view" id="view-users"></div>
<div class="view" id="view-identities"></div>
<div class="view" id="view-audit"></div>
<div class="view" id="view-maintenance"></div>
<div class="view" id="view-failures"></div>
<div class="view" id="view-scope"></div>
<div class="view" id="view-logs"></div>
<div class="view" id="view-output"></div>

<div class="outwrap">
  <div class="feedbar">
    <b>Live feed</b>
    <span class="dot" id="jdot"></span>
    <b id="jobname">idle</b>
    <span class="pill" id="jstatus">idle</span>
    <span class="muted" id="jobmeta"></span>
    <span class="sp"></span>
    <label class="chk"><input type="checkbox" id="follow-out" checked
      onchange="followOut=this.checked"> follow</label>
    <button onclick="clearOut()">Clear</button>
    <button id="stop" disabled>Interrupt</button>
  </div>
  <pre class="out" id="out">Nothing has run yet in this session.

Long jobs keep running if you close the tab &mdash; reopen and the output
picks up where it left off.</pre>
</div>
<script>
let seen=0, acts={}, S=null, cur=null, dwd=null, oauth=null, cfg=null, follow=true;
let lastSig='', ups=null, authMode=null, authModes=null, upMsg={},
    seedScales=null, seedMsg=null, resetTargetMsg=null, deployCfgMsg=null,
    runMode=null, runModes=null, stepChk=null, hostShown=false,
    view='path', seedOpen=false,
    dep={user:'root',port:'22',open:false};
let tab='setup', snap=null, scopeLines=[], logLines=[], logPath='', deployHistory=[];
let groqConfigured=false, groqKeyMask='';
let followOut=true;
const $=i=>document.getElementById(i);
/* Connection loss detection. Every poll loop reports ok/fail here; three
   consecutive failures (a dropped SSH tunnel, a restarted webui, a bad
   network blip) turns on the banner. The page keeps the last known state
   throughout, and self-heals with one repaint the moment a poll succeeds. */
let connFails=0, connLost=false;
function connOk(){
  const was=connLost;
  connFails=0; connLost=false;
  const b=$('connbar'); if(b) b.style.display='none';
  if(was) refresh(true);           // resync step state after the outage
}
function connFail(){
  connFails++;
  if(connLost||connFails<3) return;
  connLost=true;
  const b=$('connbar'); if(b){ b.style.display='block';
    const m=$('connmsg'); if(m) m.textContent=''; }
}
async function reconnect(){
  const m=$('connmsg'); if(m) m.textContent='trying\u2026';
  try{
    const r=await fetch('/api/status',{cache:'no-store'});
    if(r.ok){ location.reload(); return; }
  }catch(e){}
  if(m) m.textContent='still unreachable \u2014 restore the SSH tunnel, then Reconnect';
}
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const MARK={done:'\u2713',manual:'!',active:'\u25b8',todo:'',skip:'\u2013'};
const hb=n=>{ n=+n||0; const u=['B','KB','MB','GB','TB','PB']; let i=0;
  while(n>=1024&&i<u.length-1){n/=1024;i++} return (i===0?n.toFixed(0):n.toFixed(1))+' '+u[i]; };
const hc=n=>(+n||0).toLocaleString();
const JSTATUS={DONE:'var(--ok)',RUNNING:'var(--accent)',FAILED:'var(--bad)',
  PAUSED_QUOTA:'var(--warn)',INTERRUPTED:'var(--warn)'};
const jc=s=>JSTATUS[s]||'';

function copy(txt,btn){
  navigator.clipboard.writeText(txt).then(()=>{
    const o=btn.textContent; btn.textContent='Copied'; setTimeout(()=>btn.textContent=o,1200);
  },()=>alert('Copy failed \\u2014 select the text manually.'));
}

/* ---------------- left rail ---------------- */
function drawRail(){
  $('pbar').style.width=(S.total?100*S.done/S.total:0)+'%';
  $('pnum').textContent=S.done+' of '+S.total+' steps complete';
  $('rail').innerHTML=S.steps.map(x=>
    `<div class="rstep ${x.state} ${x.n===cur?'cur':''}" data-n="${x.n}">
       <span class="m">${MARK[x.state]||x.n}</span>
       <span><span class="t">${esc(x.title)}</span>
       <div class="sub">${esc(x.note||'')}</div></span></div>`).join('');
  document.querySelectorAll('.rstep').forEach(r=>
    r.onclick=()=>{cur=+r.dataset.n; follow=false; draw();});
}

/* ---------------- the one visible step ---------------- */

/* Values currently typed into the step-2 forms. The poll below must never
   swallow a keystroke, so anything on screen is captured before a re-render
   and put back afterwards. */
function captureForm(){
  const grab=(id,into,key)=>{const e=$(id); if(e) into[key]=e.value;};
  if($('f-sd')){ cfg=cfg||{};
    grab('f-sd',cfg,'source_domain'); grab('f-td',cfg,'target_domain');
    grab('f-sa',cfg,'source_admin');  grab('f-ta',cfg,'target_admin'); }
  if($('d-host')){
    grab('d-host',dep,'host'); grab('d-user',dep,'user');
    grab('d-port',dep,'port'); grab('d-key',dep,'key');
    const c=$('d-creds'); if(c) dep.creds=c.checked;
    const v=$('vps'); if(v) dep.open=v.style.display!=='none';
  }
}

/* Everything that changes on a poll but must not cost a re-render. */
function paintLive(){
  if(!S||S.error) return;
  const set=(id,v)=>{const e=$(id); if(e&&e.textContent!==v) e.textContent=v;};
  set('s-mig',(S.migrated||0).toLocaleString());
  set('s-fail',String(S.failed||0));
  set('s-users',(S.users_done||0)+'/'+(S.users_total||0));
  const f=$('s-fail'); if(f) f.style.color=S.failed?'var(--bad)':'';
  drawRail();
  paintJob();
}

/* A re-render is only justified when what is on screen would actually
   differ. Rebuilding the panel every poll destroyed the form mid-typing --
   the field cleared and focus was lost every few seconds. */
function signature(){
  if(!S||S.error) return 'err';
  return JSON.stringify([cur, S.total,
    S.steps.map(s=>[s.n,s.state,s.note,s.actions]),
    oauth&&[oauth.auth_mode,oauth.configured,
            oauth.source&&oauth.source.connected,oauth.source&&oauth.source.account,
            oauth.target&&oauth.target.connected,oauth.target&&oauth.target.account],
    dwd&&dwd.tenants.map(t=>[t.domain,t.client_id,t.scopes]),
    Object.keys(acts).length, follow]);
}

function draw(force){
  if(!S||S.error){
    if(!lastSig){ $('main').innerHTML=
      `<div class="warnbox">${esc(S?S.error:'no status')}</div>`; }
    return; }
  captureForm();
  const sig=signature()+'|'+view;
  if(!force && sig===lastSig){ paintLive(); return; }
  lastSig=sig;

  if(view==='path')          $('main').innerHTML=screenPath();
  else if(view==='require')  $('main').innerHTML=screenRequire();
  else                       $('main').innerHTML=screenRun();

  wireCommon();
  restoreForm();
  drawRail();
  paintJob();
}

/* ---------- screen 1: which of the three jobs is this? ---------- */
function screenPath(){
  const m=runModes||{};
  return `
    <div class="eyebrow">Step 1 of 3</div>
    <h2 class="title">What do you want to do?</h2>
    <div class="note">Each path asks only for what it needs, then runs and
      shows you the log. Nothing else is in your way.</div>
    <div class="acts" style="margin-top:20px;grid-template-columns:1fr">
      ${Object.keys(m).map(k=>{
        const x=m[k], on=(k===runMode);
        return `<button onclick="pickPath('${k}')" class="${on?'primary':''}"
            style="padding:16px">
          <span style="font-size:15px">${on?'\u25cf ':'\u25cb '}${esc(x.label)}</span>
          <small style="font-size:12px;margin-top:4px">${esc(x.blurb)}</small>
          <small style="font-size:11px;margin-top:6px;opacity:.8">
            needs ${(x.requires||[]).length} thing(s) &middot;
            runs: ${(x.runs||[]).join(' \u2192 ')}</small>
        </button>`;}).join('')}
    </div>
    <div class="nav">
      <button class="primary" onclick="go('require')"
        ${runMode?'':'disabled'}>Continue \u2192</button>
      <span class="muted">${runMode?('selected: '+esc((m[runMode]||{}).label||runMode)):'pick one to continue'}</span>
    </div>`;
}

/* ---------- screen 2: everything this path requires ---------- */
function screenRequire(){
  const m=(runModes||{})[runMode]||{};
  const need=(m.requires||[]);
  const steps=(S.steps||[]).filter(s=>need.indexOf(s.n)>=0);
  const done=steps.filter(s=>s.state==='done').length;
  const ready=(done===steps.length && steps.length>0);

  let h=`
    <div class="eyebrow">Step 2 of 3 &middot; ${esc(m.label||'')}</div>
    <h2 class="title">What this needs</h2>
    <div class="bar" style="margin:12px 0"><i style="width:${
      steps.length?100*done/steps.length:0}%"></i></div>
    <div class="note">${done} of ${steps.length} ready. Each re-checks itself
      against the live tenants.</div>`;

  steps.forEach(st=>{
    const good=st.state==='done';
    h+=`<div class="card" style="border-left:3px solid ${
        good?'var(--ok)':st.state==='manual'?'var(--warn)':'var(--line)'}">
      <div style="display:flex;gap:10px;align-items:baseline">
        <span style="color:${good?'var(--ok)':'var(--dim)'};font-weight:700">
          ${good?'\u2713':'\u25cb'}</span>
        <div style="flex:1">
          <b>${esc(st.title)}</b>
          <div class="muted">${esc(st.note||'')}</div>
        </div>
        <button onclick="checkStep(${st.n},this)">Re-check</button>
      </div>
      ${good?'':requirementBody(st)}
    </div>`;
  });

  h+=`<div class="nav">
      <button onclick="go('path')">&larr; Back</button>
      <button class="primary" onclick="go('run')" ${ready?'':'disabled'}>
        ${ready?'Everything ready \u2014 continue \u2192':'Finish the items above'}</button>
      <span id="stepchk" class="muted"></span>
    </div>`;
  return h;
}

/* The controls that satisfy one requirement, inline where it is asked for. */
function requirementBody(st){
  let h='';
  if(st.n===2) h+=configForm();
  if(st.n===3) h+=credentialsBody();
  if(st.n===5) h+=delegationBody();
  if(st.n===4 && st.actions && st.actions.length) h+=actionButtons(st.actions);
  if(st.help && st.help.length && st.n!==2)
    h+=`<div class="help" style="margin-top:10px">${esc(st.help.join('\\n'))}</div>`;
  return h;
}

/* ---------- screen 3: run it, and watch ---------- */
function screenRun(){
  const m=(runModes||{})[runMode]||{};
  const runs=(m.runs||[]);
  let h=`
    <div class="eyebrow">Step 3 of 3 &middot; ${esc(m.label||'')}</div>
    <h2 class="title">Run it</h2>
    <div class="note">${esc(m.blurb||'')}</div>
    <div class="card"><h3>Steps in this path</h3><div class="acts">`;
  runs.forEach(k=>{
    if(k==='seed'){
      h+=`<button class="danger" onclick="go('seedform')">Seed the source tenant
        <small>Writes fabricated data \u2014 asks you to type the domain</small></button>`;
      return;
    }
    const a=acts[k]; if(!a) return;
    h+=`<button class="${a.destructive?'danger':''}" onclick="run('${k}')">
      ${esc(a.label)}<small>${esc(a.blurb)}</small></button>`;
  });
  h+=`</div></div>`;
  if(view==='seedform' || seedOpen) h+=seedForm();
  h+=logPanel();
  h+=`<div class="nav">
      <button onclick="go('require')">&larr; Back</button>
      <label class="muted"><input type="checkbox" id="fol" ${
        follow?'checked':''}> follow current step</label>
    </div>`;
  return h;
}

function logPanel(){
  return `<div class="card"><h3>Live output</h3>
    <div class="run"><span class="dot" id="dot"></span>
      <b id="jname">idle</b><span class="muted" id="jmeta"></span>
      <button id="stop" style="margin-left:auto" disabled>Interrupt</button></div>
    <pre class="out" id="out">Nothing running yet.

Long jobs keep going if you close this tab \u2014 reopen and the output picks
up where it left off.</pre>
    </div>
    <div class="stats">
      <div class="stat"><b id="s-mig">${(S.migrated||0).toLocaleString()}</b><span>items migrated</span></div>
      <div class="stat"><b id="s-fail" style="${S.failed?'color:var(--bad)':''}">${S.failed||0}</b><span>failed</span></div>
      <div class="stat"><b id="s-users">${S.users_done||0}/${S.users_total||0}</b><span>users done</span></div>
    </div>`;
}

function wireCommon(){
  const f=$('fol'); if(f) f.onchange=e=>{follow=e.target.checked;};
  const st=$('stop'); if(st) st.onclick=async()=>{await fetch('/api/stop',{method:'POST'});};
  if(window._out && $('out')){ $('out').textContent=window._out;
    $('out').scrollTop=$('out').scrollHeight; }
}

function go(v){ if(v==='seedform'){ seedOpen=true; view='run'; }
                else { seedOpen=(v==='run')?seedOpen:false; view=v; }
                draw(true); }

async function pickPath(k){
  const r=await (await fetch('/api/runmode',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({mode:k})})).json();
  if(r.ok) runMode=r.run_mode;
  await refresh(true);
}


/* Put back whatever was typed before a re-render, caret included, so a
   redraw is invisible to someone mid-sentence. */
function restoreForm(){
  const a=document.activeElement, aid=a?a.id:'';
  const start=a&&a.selectionStart, end=a&&a.selectionEnd;
  const put=(id,v)=>{const e=$(id); if(e&&v!=null&&e.value!==v) e.value=v;};
  if(cfg){
    put('f-sd',cfg.source_domain); put('f-td',cfg.target_domain);
    put('f-sa',cfg.source_admin);  put('f-ta',cfg.target_admin);
  }
  if($('d-host')){
    put('d-host',dep.host); put('d-user',dep.user);
    put('d-port',dep.port); put('d-key',dep.key);
    const c=$('d-creds'); if(c&&dep.creds!=null) c.checked=dep.creds;
  }
  if(aid&&$(aid)){
    const e=$(aid); e.focus();
    try{ e.setSelectionRange(start,end); }catch(_){}
  }
}

/* ---------- reusable requirement bodies ---------- */
function configForm(){
  const c=(cfg||{});
  return `<div class="grid2" style="margin-top:12px">
      <label>Source domain<input id="f-sd" placeholder="c.example.com"
        value="${esc(c.source_domain||'')}"></label>
      <label>Target domain<input id="f-td" placeholder="a.example.com"
        value="${esc(c.target_domain||'')}"></label>
      <label>Source admin<input id="f-sa" placeholder="info@c.example.com"
        value="${esc(c.source_admin||'')}"></label>
      <label>Target admin<input id="f-ta" placeholder="info@a.example.com"
        value="${esc(c.target_admin||'')}"></label>
    </div>
    <div class="muted" style="margin-top:8px">Each admin must be a super admin
      <i>in the domain it administers</i>.</div>
    <button class="primary" style="margin-top:10px" onclick="saveCfg()">Save</button>
    <span id="cfgmsg" class="muted" style="margin-left:8px"></span>`;
}

function credentialsBody(){
  const u=(ups||{}), modes=authModes||{}, mode=authMode||'key';
  let h=`<div style="margin-top:12px"><b style="font-size:12px">How to authenticate</b>
    <div class="acts" style="margin-top:6px">`;
  Object.keys(modes).forEach(k=>{
    const m=modes[k], on=(k===mode);
    h+=`<button onclick="setMode('${k}')" class="${on?'primary':''}">
      ${on?'\u25cf ':'\u25cb '}${esc(m.label)}<small>${esc(m.blurb)}</small></button>`;
  });
  h+=`</div><div id="modemsg" class="muted" style="margin-top:6px"></div>`;
  const need=(modes[mode]&&modes[mode].needs)||[];
  const WHERE={
    oauth_client:['OAuth client ID','APIs &amp; Services \u2192 Credentials \u2192 OAuth client ID \u2192 Desktop app'],
    source_key:['Source service-account key','IAM &amp; Admin \u2192 Service Accounts \u2192 Keys \u2192 JSON, in the SOURCE project'],
    target_key:['Target service-account key','The same, in the TARGET project'],
  };
  if(!need.length){
    h+=`<div class="okbox">This mode needs no credential file at all.</div>`;
  }else{
    need.forEach(kind=>{
      const st=u[kind]||{}, w=WHERE[kind]||[kind,''], d=st.detail||{};
      const mark = !st.present ? ['\u25cb','var(--dim)','not uploaded']
                 : st.valid    ? ['\u2713','var(--ok)','looks good']
                               : ['\u2715','var(--bad)','present but not usable'];
      h+=`<div style="margin-top:12px">
        <div><span style="color:${mark[1]}">${mark[0]}</span> <b>${w[0]}</b>
          <span class="muted">${mark[2]}</span></div>
        <div class="muted">${w[1]}</div>`;
      if(st.present && !st.valid)
        h+=`<div class="warnbox" style="margin:6px 0">${esc(st.error||'')}</div>`;
      if(st.warning) h+=`<div class="warnbox" style="margin:6px 0">${esc(st.warning)}</div>`;
      if(st.valid && d.client_email)
        h+=`<div class="muted">${esc(d.client_email)} &middot; project
            <code>${esc(d.project_id||'?')}</code></div>`;
      h+=`<div style="margin-top:6px;display:flex;gap:8px;flex-wrap:wrap">
          <input type="file" accept=".json,application/json"
            onchange="upload('${kind}',this)" style="flex:1;min-width:200px">
          ${kind!=='oauth_client'?`<button onclick="checkCred('${kind}',this)"
            ${st.valid?'':'disabled'}>Test<small>Mints a real token</small></button>`:''}
        </div>
        <div class="muted" id="chk-${kind}" style="margin-top:6px">${
          upMsg[kind]?(upMsg[kind].ok?'\u2713 ':'\u2715 ')+esc(upMsg[kind].text):''
        }</div></div>`;
    });
  }
  return h+`</div>`;
}

function delegationBody(){
  if(!dwd || !dwd.tenants) return '';
  const seedPath = (runMode==='seed_only'||runMode==='seed_and_migrate');
  let h=`<div class="warnbox" style="margin-top:12px">
      <b>This step needs a person.</b> Google provides no API for domain-wide
      delegation \u2014 a super admin must grant it in a browser.</div>
    <div class="muted" style="margin:8px 0">admin.google.com \u2192 Security
      \u2192 Access and data control \u2192 API controls \u2192 Manage Domain
      Wide Delegation \u2192 Add new</div>`;
  dwd.tenants.forEach(t=>{
    const isSource = t.side==='source';
    // A seeding path needs the WRITE scopes on the source; the editor replaces
    // rather than appends, so the line shown must be the whole grant.
    const line = (isSource && seedPath && dwd.seed && dwd.seed.combined)
                 ? dwd.seed.combined : t.scopes;
    const count = (isSource && seedPath && dwd.seed && dwd.seed.combined_list)
                 ? dwd.seed.combined_list.length : (t.scope_list||[]).length;
    h+=`<div class="card" style="background:var(--bg);margin-top:10px">
      <b style="text-transform:capitalize">${t.side}</b>
      <span class="muted">${esc(t.domain||'')} \u2014 sign in as
        ${esc(t.admin||'a super admin')}</span>
      <div class="cprow"><b>Client ID</b>
        <button onclick="copy('${esc(t.client_id||'')}',this)">Copy</button></div>
      <pre class="copy">${esc(t.client_id||'(upload the key first)')}</pre>
      <div class="cprow"><b>OAuth scopes</b>
        <button onclick="copy(${esc(JSON.stringify(line||''))},this)">Copy</button>
        <span class="muted">${count} scopes \u2014 paste the whole line, that
          editor replaces rather than appends</span></div>
      <pre class="copy">${esc(line||'(set the domains first)')}</pre>
      <button onclick="diagnoseScopes('${t.side}',this)">Diagnose scopes</button>
      <button onclick="dwdAutomate('${t.side}',this)">Automate</button>
      <div id="diag-${t.side}" style="margin-top:6px"></div>
      <div id="dwdauto-${t.side}" class="muted" style="margin-top:6px"></div>
      ${fullUnionBlock(t.side)}
    </div>`;
  });
  return h+`<div class="muted">Grants take ~2 minutes to propagate, sometimes 30.
    Use <b>Re-check</b> above; it goes green when a real token mint succeeds.</div>`;
}

/* The "paste once, never revisit" line: every scope this tenant could ever
   need across every transfer mode and optional-feature toggle, not just
   whichever ones happen to be on right now. The Admin Console editor
   replaces the whole grant on every edit and re-triggers propagation delay
   for the ENTIRE grant (seen live: ~2 min typical, up to 30) -- so turning
   on a new feature next month by re-pasting a narrower line risks breaking
   everything that already worked, exactly as happened live this session. */
function fullUnionBlock(side){
  const key = side==='source' ? 'migrate_source_full' : 'migrate_target_full';
  const full = (dwd && dwd[key]) || [];
  if(!full.length) return '';
  const line = full.join(',');
  return `<details style="margin-top:8px">
    <summary class="muted" style="cursor:pointer">MIGRATE ${side.toUpperCase()} — full key (${full.length} scopes, every feature toggle, paste once)</summary>
    <div class="cprow"><button onclick="copy(${esc(JSON.stringify(line))},this)">Copy</button></div>
    <pre class="copy">${esc(line)}</pre>
  </details>`;
}

/* A single unauthorised (or not-yet-propagated) scope fails the *whole*
   combined token request with the same generic unauthorized_client error,
   whatever else in it is fine -- diagnosed live, more than once, by
   manually minting one token per scope over SSH before this existed.
   Mints one token per scope against Google, so this is a click, never
   something polled. */
async function diagnoseScopes(tenant, btn){
  const box=$('diag-'+tenant);
  const label=btn.innerHTML;
  btn.disabled=true; btn.innerHTML='Checking each scope…';
  try{
    const r=await (await fetch('/api/scope_diagnosis',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({tenant:tenant})})).json();
    if(!r.ok){ box.innerHTML=`<div class="muted" style="color:var(--bad)">${esc(r.error||'failed')}</div>`; return; }
    const d=r.diagnosis;
    if(d.error && !d.combined_ok){
      box.innerHTML=`<div class="muted" style="color:var(--bad)">Combined request failed: ${esc(d.error)}</div>`;
    } else { box.innerHTML=''; }
    if(d.combined_ok){
      box.innerHTML+=`<div class="muted" style="color:var(--ok)">All ${d.scopes.length} scope(s) authorised.</div>`;
    } else {
      box.innerHTML+=d.scopes.map(s=>
        `<div style="display:flex;gap:8px;align-items:center;margin-top:2px">
           <span class="pill" style="background:${s.ok?'var(--ok)':'var(--bad)'};color:#fff">${s.ok?'OK':'FAIL'}</span>
           <code style="font-size:11px">${esc(s.scope)}</code>
         </div>`).join('');
    }
  } finally {
    btn.disabled=false; btn.innerHTML=label;
  }
}

function actionButtons(keys){
  let h=`<div class="acts" style="margin-top:12px">`;
  keys.forEach(k=>{const a=acts[k]; if(!a) return;
    h+=`<button class="${a.destructive?'danger':''}" onclick="run('${k}')">
      ${esc(a.label)}<small>${esc(a.blurb)}</small></button>`;});
  return h+`</div>`;
}

function seedForm(){
  const src=(cfg&&cfg.source_domain)||'';
  return `<div class="card" style="border-color:var(--bad)">
    <h3>Seed the source tenant</h3>
    <div class="warnbox">This writes fabricated data into a live tenant. It
      only ever targets the <b>source</b> domain, and only after you type it.</div>
    <div class="grid2" style="margin-top:12px">
      <label>Scale<select id="seed-scale">${(seedScales||['medium']).map(v=>
        `<option${v==='medium'?' selected':''}>${v}</option>`).join('')}</select></label>
      <label>Type the source domain to confirm
        <input id="seed-confirm" placeholder="${esc(src||'set the domains first')}"></label>
    </div>
    <label style="display:block;margin-top:10px">
      <input type="checkbox" id="seed-create"> also create the five accounts</label>
    <label style="display:block;margin-top:6px">
      <input type="checkbox" id="seed-all-users"> seed every user the tenant
      already has (real headcount, not a fixed five)</label>
    <label style="display:block;margin-top:6px">
      <input type="checkbox" id="seed-reset"> <b style="color:var(--bad)">reset
      first</b> \u2014 DELETES existing data for those users</label>
    <button class="danger" style="margin-top:12px" onclick="runSeed()">
      Run the seeder<small>Targets ${esc(src||'(no source domain)')}</small></button>
    <div id="seedmsg" class="muted" style="margin-top:8px">${
      seedMsg?(seedMsg.ok?'\u2713 ':'\u2715 ')+esc(seedMsg.text):''}</div>
  </div>`;
}

function resetTargetForm(){
  const tgt=(cfg&&cfg.target_domain)||'';
  return `<div class="card" style="border-color:var(--bad)">
    <h3>Reset the target tenant</h3>
    <div class="warnbox">Empties the TARGET tenant's seeded Drive, Gmail,
      Calendar and Chat data \u2014 not the ledger, and never the source. Do
      this before a clean re-test, not after a real migration you want to
      keep.</div>
    <div class="grid2" style="margin-top:12px">
      <label>Type the target domain to confirm
        <input id="reset-target-confirm" placeholder="${esc(tgt||'set the domains first')}"></label>
    </div>
    <button class="danger" style="margin-top:12px" onclick="doResetTarget()">
      Empty the target tenant<small>Targets ${esc(tgt||'(no target domain)')}</small></button>
    <div id="resettargetmsg" class="muted" style="margin-top:8px">${
      resetTargetMsg?(resetTargetMsg.ok?'\u2713 ':'\u2715 ')+esc(resetTargetMsg.text):''}</div>
  </div>`;
}

/* The seed tab: its own destination, not a step buried inside the linear
   setup wizard -- seeding and resetting a sandbox tenant are rehearsal
   tools for test tenants only, orthogonal to the real migration setup
   steps 1-8 track (see wizard.py's build_steps() docstring). */
function seedTabHTML(){
  return `<h2>Seed sandbox</h2>
    <div class="note">Build test data in a throwaway SOURCE tenant, run a
      rehearsal migration against it, then reset the TARGET tenant here for
      a clean re-test. Separate from the Setup wizard on purpose: none of
      this applies to a real migration, where the data is already there.</div>
    ${seedForm()}${resetTargetForm()}`;
}

/* The VPS connection form. `deploy()` and the /api/deploy endpoint that
   drives it have existed since this page's captureForm()/restoreForm()
   were written -- what was missing was this markup itself, so d-host and
   friends had no element to bind to and "Deploy" was unreachable from the
   UI at all. Prefilled from dep, which refresh() adopts once from the
   server's saved DEPLOY_* env.sh entries (read_deploy_config()) the same
   way the setup form adopts SOURCE_DOMAIN etc. */
function deployTabHTML(){
  return `<h2>Deploy to a VPS</h2>
    <div class="note">Copy this tool to a server that stays up through a
      multi-hour migration, then reach it over an SSH tunnel. Save your VPS's
      connection details once here; every future Deploy (and any other
      session of this UI) reuses them.</div>
    <div class="card">
      <h3>VPS connection</h3>
      <div class="grid2" style="margin-top:12px">
        <label>Host<input id="d-host" placeholder="203.0.113.10 or vps.example.com"
          value="${esc(dep.host||'')}"></label>
        <label>SSH user<input id="d-user" placeholder="root"
          value="${esc(dep.user||'root')}"></label>
        <label>SSH port<input id="d-port" placeholder="22"
          value="${esc(dep.port||'22')}"></label>
        <label>SSH key path (optional)<input id="d-key"
          placeholder="~/.ssh/id_ed25519" value="${esc(dep.key||'')}"></label>
      </div>
      <label style="display:block;margin-top:10px">
        <input type="checkbox" id="d-creds" ${dep.creds?'checked':''}> also copy
        service-account keys and OAuth tokens — lets this host read every
        mailbox in both tenants</label>
      <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">
        <button onclick="saveDeployConfig()">Save VPS credentials</button>
        <button class="danger" onclick="deploy()">Deploy now
          <small>Targets ${esc(dep.host||'(no host set)')}</small></button>
      </div>
      <div id="deploycfgmsg" class="muted" style="margin-top:8px">${
        deployCfgMsg?(deployCfgMsg.ok?'✓ ':'✕ ')+esc(deployCfgMsg.text):''}</div>
    </div>
    ${deployHistoryTable()}`;
}

/* Every past /api/deploy call, most recent first -- previously the only
   answer to "did the last deploy actually work, and what commit is running
   on that VPS right now" was SSHing in and checking by hand. A still-running
   entry (rc still null with no finishedAt) reads as "in progress", not a
   silent gap, since the callback that would fill it in only fires once the
   detached deploy_remote.py process actually exits. */
function deployHistoryTable(){
  if(!deployHistory.length) return `<div class="card" style="margin-top:16px">
    <h3>Deploy history</h3>
    <div class="muted">No deploys recorded yet in this checkout.</div></div>`;
  const rows=deployHistory.map(h=>{
    const status=h.rc===null
      ? (h.finishedAt?'<span class="pill" style="background:var(--bad);color:#fff">never started</span>'
                     :'<span class="pill" style="background:var(--accent);color:#fff">in progress</span>')
      : (h.rc===0?'<span class="pill" style="background:var(--ok);color:#fff">ok</span>'
                 :`<span class="pill" style="background:var(--bad);color:#fff">exit ${h.rc}</span>`);
    return `<tr>
      <td>${esc(h.startedAt||'')}</td>
      <td>${esc(h.host||'')}</td>
      <td><code>${esc(h.commit||'(no git history)')}</code></td>
      <td>${h.includeCredentials?'yes':'no'}</td>
      <td>${status}</td>
    </tr>`;
  }).join('');
  return `<div class="card" style="margin-top:16px">
    <h3>Deploy history</h3>
    <table class="mono" style="width:100%;border-collapse:collapse;font-size:12px">
      <thead><tr style="text-align:left">
        <th>Started</th><th>Host</th><th>Commit</th><th>Creds</th><th>Result</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>
  </div>`;
}

async function saveDeployConfig(){
  captureForm();
  const r=await (await fetch('/api/deploy_config',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({host:dep.host,user:dep.user,port:dep.port,key:dep.key})})).json();
  deployCfgMsg={ok:!!r.ok, text: r.ok ? 'saved' : r.error};
  if(tab==='deploy') drawView();
}

/* ---------------- actions ---------------- */
async function run(name){
  const a=acts[name];
  if(a.destructive){
    const t=prompt(`${a.label}\\n\\nThis changes a tenant.\\nType ${a.confirm} to proceed:`);
    if(t!==a.confirm) return;
  }
  const r=await (await fetch('/api/run',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action:name,confirm:a.confirm||''})})).json();
  if(!r.ok){ alert(r.error); return; }
  clearOut();
}

async function recheckDWD(btn){
  btn.disabled=true;
  const o=btn.innerHTML;
  btn.innerHTML='Checking delegation...';
  if($('dwdmsg')) $('dwdmsg').textContent='Minting tokens...';
  try{
    const r=await fetch('/api/check_dwd',{method:'POST'});
    const d=await r.json();
    btn.disabled=false;
    btn.innerHTML=o;
    if(d.ok){
      if($('dwdmsg')) $('dwdmsg').textContent='Check complete!';
      poll();
    }else{
      if($('dwdmsg')) $('dwdmsg').textContent=d.error||'Check failed';
    }
  }catch(e){
    btn.disabled=false;
    btn.innerHTML=o;
    if($('dwdmsg')) $('dwdmsg').textContent='Error: '+e;
  }
}

async function dwdAutomate(tenant,btn){
  const el=$('dwdauto-'+tenant);
  if(!el) return;
  btn.disabled=true;
  el.textContent='Building automation command...';
  try{
    const r=await (await fetch('/api/dwd/automate',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({tenant})})).json();
    btn.disabled=false;
    if(!r.ok){ el.textContent=r.error||'failed'; return; }
    // This webui runs headless (VPS), so the browser must open on the
    // operator's machine. Show the command, pre-copied.
    copy(r.command, btn);
    el.innerHTML='<b>Run this on your machine (browser will open, sign in '
      +'by hand, the tool clicks through DWD):</b><pre class="copy">'
      +esc(r.command)+'</pre>';
  }catch(e){
    btn.disabled=false;
    el.textContent='Error: '+e;
  }
}

function cfgFields(){
  return {source_domain:$('f-sd').value, target_domain:$('f-td').value,
          source_admin:$('f-sa').value, target_admin:$('f-ta').value};
}
async function saveCfg(){
  const r=await (await fetch('/api/config',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify(cfgFields())})).json();
  const m=$('cfgmsg');
  m.textContent=r.ok?('Saved \u2014 '+r.msg):('\u26a0 '+r.error);
  m.style.color=r.ok?'var(--ok)':'var(--bad)';
  if(r.ok){ cfg=r.config; refresh(true); }
  return r.ok;
}
async function runSetup(keyless){
  if(!await saveCfg()) return;
  const r=await (await fetch('/api/setup',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({keyless:!!keyless})})).json();
  if(!r.ok){ alert(r.error); return; }
  clearOut();
}
async function deploy(){
  const creds=$('d-creds').checked;
  let confirmPhrase='';
  if(creds){
    confirmPhrase=prompt('This copies service-account keys and OAuth tokens to '+
      $('d-host').value+'.\\n\\nThose files can read every mailbox in both '+
      'tenants.\\n\\nType DEPLOY to proceed:')||'';
    if(confirmPhrase!=='DEPLOY') return;
  }
  const r=await (await fetch('/api/deploy',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({host:$('d-host').value,user:$('d-user').value,
      port:$('d-port').value,key:$('d-key').value,
      include_credentials:creds,confirm:confirmPhrase})})).json();
  if(!r.ok){ alert(r.error); return; }
  clearOut();
  fetchDeployHistory();
}

async function runSeed(){
  const typed=$('seed-confirm').value.trim();
  const src=(cfg&&cfg.source_domain)||'';
  const reset=$('seed-reset').checked;
  if(reset && !confirm('RESET deletes all existing Drive, Gmail and Calendar '+
      'data for the seeded users in '+src+'.\\n\\nThis cannot be undone. Continue?'))
    return;
  const r=await (await fetch('/api/seed',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({confirm_domain:typed, scale:$('seed-scale').value,
      create_users:$('seed-create').checked, all_users:$('seed-all-users').checked,
      reset:reset})})).json();
  seedMsg={ok:!!r.ok, text: r.ok ? ('seeding '+src+' \\u2014 output below') : r.error};
  if(r.ok){ clearOut(); }
  if(tab==='seed') drawView();
  await refresh(true);
}

async function doResetTarget(){
  const typed=$('reset-target-confirm').value.trim();
  const tgt=(cfg&&cfg.target_domain)||'';
  if(!confirm('This empties '+(typed||tgt)+
      '.\\n\\nThis cannot be undone. Continue?'))
    return;
  const r=await (await fetch('/api/reset_target',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({confirm_domain:typed})})).json();
  resetTargetMsg={ok:!!r.ok, text: r.ok ? ('resetting '+tgt+' \\u2014 output below') : r.error};
  if(r.ok){ clearOut(); }
  if(tab==='seed') drawView();
  await refresh(true);
}

async function checkCred(kind, btn){
  const box=$('chk-'+kind); const label=btn.innerHTML;
  btn.disabled=true; btn.textContent='Testing\u2026';
  if(box){ box.textContent='Minting a token\u2026'; box.style.color=''; }
  try{
    const r=await (await fetch('/api/check',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({kind:kind})})).json();
    if(box){ box.textContent=(r.ok?'\u2713 ':'\u2715 ')+(r.msg||r.error);
             box.style.color=r.ok?'var(--ok)':'var(--bad)'; }
  }catch(e){ if(box){ box.textContent='check failed: '+e; } }
  btn.disabled=false; btn.innerHTML=label;
}

async function checkStep(n, btn){
  const box=$('stepchk'); const label=btn.textContent;
  btn.disabled=true; btn.textContent='Checking\u2026';
  if(box){ box.textContent='re-reading live state\u2026'; box.style.color=''; }
  try{
    const r=await (await fetch('/api/checkstep',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({step:n})})).json();
    stepChk={n:n, ok:!!r.ok, text:(r.msg||r.error)+(r.detail?' \u2014 '+r.detail:'')};
    if(box){ box.textContent=(r.ok?'\u2713 ':'\u2715 ')+stepChk.text.slice(2)||'';
             box.textContent=(r.ok?'\u2713 ':'\u2715 ')+(r.msg||r.error)+
               (r.detail?' \u2014 '+r.detail:'');
             box.style.color=r.ok?'var(--ok)':'var(--bad)'; }
  }catch(e){ if(box) box.textContent='check failed: '+e; }
  btn.disabled=false; btn.textContent=label;
  refresh(true);
}

async function setRunMode(mode){
  const r=await (await fetch('/api/runmode',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({mode:mode})})).json();
  const m=$('runmsg');
  if(m){ m.textContent=r.ok?r.msg:('\u26a0 '+r.error);
         m.style.color=r.ok?'var(--ok)':'var(--bad)'; }
  if(r.ok) runMode=r.run_mode;
  refresh(true);
}

async function setMode(mode){
  const r=await (await fetch('/api/authmode',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({mode:mode})})).json();
  const m=$('modemsg');
  if(m){ m.textContent=r.ok?r.msg:('\u26a0 '+r.error);
         m.style.color=r.ok?'var(--ok)':'var(--bad)'; }
  if(r.ok) authMode=r.auth_mode;
  refresh(true);
}

async function upload(kind, input){
  const f=input.files&&input.files[0]; if(!f) return;
  const box=$('chk-'+kind);
  if(box){ box.textContent='Reading '+f.name+String.fromCharCode(8230);
           box.style.color=''; }
  const text=await f.text();
  const r=await (await fetch('/api/upload',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({kind:kind,content:text})})).json();
  /* Held in state, not written into the DOM: the refresh below rebuilds this
     panel, and anything written straight to an element goes with it. */
  upMsg[kind] = {ok:!!r.ok, text:(r.ok? (f.name+' accepted') : r.error)};
  input.value='';
  await refresh(true);
}

async function oauthGo(t){
  const r=await (await fetch('/api/oauth/begin',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({tenant:t})})).json();
  if(!r.ok){ alert(r.error); return; }
  window.open(r.url,'_blank');
}
async function oauthDrop(t){
  if(!confirm(`Forget the stored ${t} token?\\n\\nThis does NOT revoke access at Google.`))return;
  const r=await (await fetch('/api/oauth/disconnect',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({tenant:t})})).json();
  if(r.msg) alert(r.msg);
  refresh();
}

let job={};
function fmtEta(s){
  if(s<60) return s+'s left';
  const m=Math.round(s/60); if(m<60) return m+'m left';
  const h=Math.floor(m/60); return h+'h '+(m%60)+'m left';
}
function paintJob(){
  const r=job.running;
  const set=(id,v)=>{const e=$(id); if(e&&e.textContent!==v) e.textContent=v;};
  if($('dot')) $('dot').className='dot'+(r?' on':'');
  if($('jdot')) $('jdot').className='dot'+(r?' on':'');
  const prog=(r&&job.progressPct!=null)?' \u00b7 '+job.progressPct+'%'
    +(job.etaSeconds!=null?' \u00b7 '+fmtEta(job.etaSeconds):''):'';
  const meta=r?job.elapsed+'s elapsed'
    +(job.detached?' \u00b7 external pid '+job.pid:'')+prog
    :(job.rc===null||job.rc===undefined?'':'exit '+job.rc+' \u00b7 '+job.elapsed+'s');
  const statusTxt=r?'running'
    :(job.rc===null||job.rc===undefined?'idle'
      :(job.rc===0?'done':'exit '+job.rc));
  set('jname',job.name||'idle'); set('jmeta',meta);
  set('jobname',job.name||'idle'); set('jobmeta',meta);
  set('of-name',job.name||'idle'); set('of-meta',meta);
  const st=$('jstatus'); if(st){ st.textContent=statusTxt;
    st.style.color=r?'var(--accent)':(job.rc===0?'var(--ok)':(job.rc?'var(--bad)':'')); }
  const ofs=$('of-status'); if(ofs){ ofs.textContent=statusTxt;
    ofs.style.color=r?'var(--accent)':(job.rc===0?'var(--ok)':(job.rc?'var(--bad)':'')); }
  const ofd=$('of-dot'); if(ofd) ofd.className='dot'+(r?' on':'');
  if($('stop')) $('stop').disabled=!r;
  document.querySelectorAll('#tb button').forEach(b=>b.disabled=r);
  document.querySelectorAll('.acts button').forEach(b=>b.disabled=r);
}

async function refresh(force){
  let ok=false;
  try{
    const [s,o,d,c]=await Promise.all([
      fetch('/api/status').then(r=>r.json()).catch(()=>null),
      fetch('/api/oauth/status').then(r=>r.json()).catch(()=>null),
      fetch('/api/dwd').then(r=>r.json()).catch(()=>null),
      fetch('/api/config').then(r=>r.json()).catch(()=>null)]);
    ok=!!s;
    if(s&&!s.error) S=s; else if(!S) S=s;      // keep the last good status
    if(o) oauth=o;
    if(d) dwd=d;
    /* Adopt the server's copy only before anything is typed. Otherwise a
       poll would overwrite half-entered fields with what is on disk. */
    if(c&&c.config&&cfg===null) cfg=c.config;
    if(c&&c.uploads) ups=c.uploads;
    if(c&&c.auth_modes){ authModes=c.auth_modes; authMode=c.auth_mode; }
    if(c&&c.seed_scales) seedScales=c.seed_scales;
    if(c&&c.run_modes){ runModes=c.run_modes; runMode=c.run_mode; }
    /* Same "only before anything is typed" rule as cfg above -- dep.host
       is null until either a save/deploy has run or this adopts the saved
       value, so this only ever fires once, on first load. */
    if(c&&c.deploy&&dep.host==null) Object.assign(dep, c.deploy);
    // Set once and never touched again: a process's hostname/code path/pid
    // cannot change while it keeps running (see webui.py's host_info(),
    // cached server-side for the same reason). This exists because a local
    // seed run and a deployed VPS instance can both bind 127.0.0.1:8080 and
    // look identical in the browser -- nothing said which one a tab was
    // actually talking to until now.
    if(c&&c.host&&!hostShown){
      hostShown=true;
      const hb=$('hostbadge');
      if(hb){
        hb.textContent=c.host.hostname;
        hb.title='Code: '+c.host.code_path+'\\nPID: '+c.host.pid+
          (c.host.commit?' \\u00b7 commit '+c.host.commit:' \\u00b7 no git history (deployed copy)');
      }
    }
    if(!s.error) $('route').textContent=
      (s.env.SOURCE_DOMAIN||'?')+' \u2192 '+(s.env.TARGET_DOMAIN||'?')
      +(s.env.AUTH_MODE?'  \u00b7  '+s.env.AUTH_MODE:'');
    draw(force);
  }catch(e){}
  ok?connOk():connFail();
}

async function pollJob(){
  let ok=false;
  try{
    const j=await (await fetch('/api/job?since='+seen)).json();
    ok=true;
    if(j.pid&&j.pid!==job.pid) seen=0;   // a different process started: refetch
    if(j.lines.length){
      window._out=(window._out||'')+j.lines.join('\\n')+'\\n';
      seen=j.total;
      const pre=$('out'); if(pre){
        pre.textContent=window._out;
        if(followOut) pre.scrollTop=pre.scrollHeight; }
      const pre2=$('out2'); if(pre2){
        pre2.textContent=window._out;
        if(followOut) pre2.scrollTop=pre2.scrollHeight; }
    }
    const was=job.running; job=j; paintJob();
    if(was&&!j.running){
      refresh(true);   // a finished job usually changes step state
      if(j.name==='deploy') fetchDeployHistory();
    }
  }catch(e){}
  ok?connOk():connFail();
  setTimeout(pollJob,1200);
}

function clearOut(){
  window._out=''; seen=0;
  const pre=$('out'); if(pre) pre.textContent='';
  const pre2=$('out2'); if(pre2) pre2.textContent='';
}

/* ---------------- command toolbar + operator tabs ---------------- */
function drawToolbar(){
  const tb=$('tb'); if(!tb||!Object.keys(acts).length) return;
  tb.innerHTML=Object.keys(acts).map(k=>{
    const a=acts[k];
    return `<button class="${a.destructive?'danger':''}" onclick="run('${k}')"
      title="${esc(a.blurb)}">${esc(a.label)}</button>`;
  }).join('')+`<span class="sep"></span>
    <button onclick="goSeed()" title="Seed a sandbox tenant with test data">Seed</button>
    <span class="sep"></span>
    <label class="chk"><input type="checkbox" id="tog-dry" class="tb-toggle"
      onchange="toggleChange()"> dry-run</label>
    <label class="chk"><input type="checkbox" id="tog-drive" class="tb-toggle" checked
      onchange="toggleChange()"> drive</label>
    <label class="chk"><input type="checkbox" id="tog-gmail" class="tb-toggle" checked
      onchange="toggleChange()"> gmail</label>
    <label class="chk"><input type="checkbox" id="tog-calendar" class="tb-toggle" checked
      onchange="toggleChange()"> calendar</label>
    <label class="chk"><input type="checkbox" id="tog-chat" class="tb-toggle"
      onchange="toggleChange()"> chat</label>
    <label class="chk"><input type="checkbox" id="tog-contacts" class="tb-toggle"
      onchange="toggleChange()"> contacts</label>
    <label class="chk"><input type="checkbox" id="tog-tasks" class="tb-toggle"
      onchange="toggleChange()"> tasks</label>`;
  paintJob();
}

const SERVICE_KEYS=['drive','gmail','calendar','chat','contacts','tasks'];

async function toggleChange(){
  const svcs={};
  SERVICE_KEYS.forEach(k=>{const c=$('tog-'+k); if(c) svcs[k]=c.checked;});
  const dry=$('tog-dry');
  await fetch('/api/toggles',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({dry_run:dry&&dry.checked,services:svcs})});
}

function applyToggles(tg){
  if(!tg) return;
  const a=document.activeElement;
  if(a&&a.classList&&a.classList.contains('tb-toggle')) return;
  const dry=$('tog-dry'); if(dry) dry.checked=!!tg.dry_run;
  SERVICE_KEYS.forEach(k=>{
    const c=$('tog-'+k); if(c&&tg.services) c.checked=!!tg.services[k]; });
}

function toggleTheme() {
  document.body.classList.toggle('light-theme');
}

function toggleVerbose() {
  document.body.classList.toggle('hide-verbose');
}

function filterToolbar(q) {
  const query = (q || '').toLowerCase();
  document.querySelectorAll('#tb button').forEach(b => {
    b.style.display = b.textContent.toLowerCase().includes(query) ? 'inline-block' : 'none';
  });
}

function goSeed(){ setTab('seed'); }

let identitiesList = [];
async function fetchIdentities() {
  try {
    const r = await (await fetch('/api/identities')).json();
    if (r.ok) {
      identitiesList = r.csv_identities || [];
      if (tab === 'identities') drawView();
    }
  } catch(e){}
}

async function saveIdentitiesCSV() {
  try {
    const r = await (await fetch('/api/identities/save', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({identities: identitiesList})
    })).json();
    alert(r.msg || r.error);
  } catch(e){ alert(e); }
}

function showAddIdentityPair() {
  const src = prompt("Source Email (user@src.com):");
  if (!src) return;
  const tgt = prompt("Target Email (user@dst.com):");
  if (!tgt) return;
  identitiesList.push({source_email: src, target_email: tgt, entity_type: 'user'});
  drawView();
}

function identitiesHTML() {
  return `<h2>👥 Directory Identity Mapping</h2>
    <div class="warnbox">Map users, groups, and aliases between source domain (${esc((cfg||{}).source_domain || 'source')}) and target domain (${esc((cfg||{}).target_domain || 'target')}).</div>
    <div style="display:flex;gap:10px;margin-bottom:12px;flex-wrap:wrap">
      <button class="primary" onclick="run('init_db_auto')">⚡ Auto-Map Mappings by Directory</button>
      <button onclick="run('init_db')">📥 Reload identities.csv into DB</button>
      <button onclick="showAddIdentityPair()">➕ Add Single Identity Pair</button>
      <button onclick="saveIdentitiesCSV()">💾 Save to identities.csv</button>
    </div>
    <table>
      <thead><tr><th>Source Email</th><th>Target Email</th><th>Entity Type</th><th>Status</th></tr></thead>
      <tbody>
        ${identitiesList.map(i => `
          <tr>
            <td><code>${esc(i.source_email)}</code></td>
            <td><code>${esc(i.target_email)}</code></td>
            <td><span class="pill">${esc(i.entity_type)}</span></td>
            <td><span class="pill done">MAPPED</span></td>
          </tr>
        `).join('') || '<tr><td colspan="4" class="muted">No mappings loaded yet. Click Auto-Map above.</td></tr>'}
      </tbody>
    </table>`;
}

function auditHTML() {
  return `<h2>🔍 Pre-Migration Inventory & Audit Suite</h2>
    <div class="note">Deep data integrity check tools comparing source vs target corpora.</div>
    <div class="acts" style="margin-top:14px">
      <button class="primary" onclick="run('inventory')">📦 Tenant Inventory<small>Breakdown of native Docs, Sheets, Gmail threads, Calendar events per user</small></button>
      <button class="primary" onclick="run('acl_audit')">🔒 Share Access ACL Audit<small>File-by-file grant comparison across domains</small></button>
      <button onclick="run('verify')">🔍 Sample Verification<small>Spot-check 25 random samples</small></button>
      <button onclick="run('verify_scopes')">🛡️ Verify API Scopes<small>Explicit scope check on source & target keys</small></button>
    </div>`;
}

function maintenanceHTML() {
  return `<h2>🛠️ Maintenance, Retry & Targeted Rollback</h2>
    <div class="note">Resolve failure ledgers, repair Drive file dates, or delete migrated items.</div>
    <div class="acts" style="margin-top:14px">
      <button onclick="run('resolve_dry')">🔍 Resolve Failures (Dry Run)<small>Preview items that would be retried</small></button>
      <button class="primary" onclick="run('resolve')">⚡ Retry Failed Items<small>Retry FAILED items with current code</small></button>
      <button onclick="run('repair_modified_times')">🕒 Repair Drive Timestamps<small>Restore original file creation and modified dates</small></button>
      <button onclick="run('backfill_drive')">📥 Backfill Drive Ledger<small>Mark Drive complete on pre-existing ledgers</small></button>
      <button class="danger" onclick="run('undo_dry')">🔍 Undo Rollback (Dry Run)<small>Count items target undo would delete</small></button>
      <button class="danger" onclick="run('undo')">🗑️ Execute Targeted Rollback<small>Delete migrated items recorded in id_mapping</small></button>
      <button class="danger" onclick="run('reset_drive_ledger')">🔄 Reset Drive Ledger<small>Clear Drive audit records to force re-migration</small></button>
    </div>`;
}

function setTab(t){
  tab=t;
  document.querySelectorAll('.tabs button').forEach(b=>
    b.classList.toggle('on',b.dataset.tab===t));
  const views={setup:$('setup'),seed:$('view-seed'),deploy:$('view-deploy'),
    dashboard:$('view-dashboard'),users:$('view-users'),identities:$('view-identities'),
    audit:$('view-audit'),maintenance:$('view-maintenance'),failures:$('view-failures'),
    scope:$('view-scope'),logs:$('view-logs'),output:$('view-output')};
  Object.keys(views).forEach(k=>{ const el=views[k]; if(!el) return;
    el.style.display=(k===t)?(k==='setup'?'grid':'block'):'none'; });
  if(t!=='setup'){
    if(!S) refresh();
    if(t==='identities') fetchIdentities();
    if(t==='scope'&&!scopeLines.length) fetchScope();
    if(t==='logs'&&!logLines.length) fetchLogs();
    if(t==='deploy') fetchDeployHistory();
    drawView();
  }else{
    draw(true);
  }
}

function drawView(){
  if(tab==='setup') return;
  const el=$('view-'+tab); if(!el) return;
  let h='';
  if(tab==='seed') h=seedTabHTML();
  else if(tab==='deploy') h=deployTabHTML();
  else if(tab==='dashboard') h=dashboardHTML();
  else if(tab==='users') h=usersHTML();
  else if(tab==='identities') h=identitiesHTML();
  else if(tab==='audit') h=auditHTML();
  else if(tab==='maintenance') h=maintenanceHTML();
  else if(tab==='failures') h=failuresHTML();
  else if(tab==='scope') h=scopeHTML();
  else if(tab==='logs') h=logsHTML();
  else if(tab==='output') h=outputHTML();
  if(h!==el.innerHTML){
    el.innerHTML=h;
    if(tab==='logs'){ const p=el.querySelector('pre'); if(p) p.scrollTop=p.scrollHeight; }
    if(tab==='output'){ const p=el.querySelector('pre');
      if(p){ p.textContent=window._out||''; if(followOut) p.scrollTop=p.scrollHeight; } }
  }
}

/* The full, large live feed of whatever job is running. */
function outputHTML(){
  return `<h2>Live feed</h2>
    <div class="feedbar">
      <span class="dot" id="of-dot"></span>
      <b id="of-name">idle</b>
      <span class="pill" id="of-status">idle</span>
      <span class="muted" id="of-meta"></span>
      <span class="sp"></span>
      <label class="chk"><input type="checkbox" id="follow-out2" checked
        onchange="followOut=this.checked"> follow</label>
      <button onclick="clearOut()">Clear</button>
    </div>
    <pre class="out" id="out2" style="height:calc(100vh - 250px)"></pre>`;
}

/* The snapshot the TUI shows: totals, per-service bars, active users and
   the most recent failures, all read from migration.db on the server. */
function dashboardHTML(){
  if(!snap||!snap.snapshot)
    return `<h2>Dashboard</h2><div class="warnbox">${
      esc(snap&&snap.error||'no data yet')}</div>`;
  const t=snap.snapshot.totals, us=snap.snapshot.users||[], fs=snap.snapshot.failures||[];
  const frac=t.fraction;
  const svc=[['Drive',us.reduce((a,u)=>a+u.drive_done,0),
      us.reduce((a,u)=>a+u.drive_failed,0),us.reduce((a,u)=>a+u.drive_skipped,0),
      us.reduce((a,u)=>a+u.exp_drive,0)],
    ['Gmail',us.reduce((a,u)=>a+u.mail_done,0),
      us.reduce((a,u)=>a+u.mail_failed,0),us.reduce((a,u)=>a+u.mail_skipped,0),
      us.reduce((a,u)=>a+u.exp_mail,0)],
    ['Calendar',us.reduce((a,u)=>a+u.cal_done,0),
      us.reduce((a,u)=>a+u.cal_failed,0),0,0]];
  const svcRows=svc.map(r=>{
    const [name,done,failed,skipped,exp]=r;
    const w=exp?Math.round(100*Math.min(1,done/exp)):0;
    return `<tr><td><b>${name}</b></td><td style="min-width:170px">
      <div class="gbar"><i style="width:${w}%"></i></div></td>
      <td class="num">${hc(done)}</td><td class="num">${hc(skipped)}</td>
      <td class="num" style="${failed?'color:var(--bad)':''}">${hc(failed)}</td></tr>`;
  }).join('');
  const active=us.filter(u=>u.status==='RUNNING'||u.status==='PAUSED_QUOTA')
    .sort((a,b)=>b.done-a.done).slice(0,8);
  const activeRows=active.map(u=>`<tr><td>${esc(u.source)}</td>
    <td style="min-width:150px"><div class="gbar"><i style="width:${Math.round(100*(u.fraction||0))}%"></i></div></td>
    <td class="num">${hc(u.done)}</td><td class="num">${hb(u.bytes_moved)}</td>
    <td style="${jc(u.status)?'color:'+jc(u.status):''}">${u.status}</td></tr>`).join('');
  const failRows=fs.slice(0,6).map(f=>`<tr>
    <td class="muted">${esc(f.timestamp||'')}</td><td>${esc(f.item_type||'')}</td>
    <td>${esc(f.source_user||'')}</td>
    <td style="color:var(--bad)">${esc(f.error_message||'')}</td></tr>`).join('');
  return `<h2>Dashboard</h2>
    <div class="stats">
      <div class="stat"><b>${hc(t.items_done)} / ${t.items_expected?hc(t.items_expected):'?'}</b><span>items moved</span></div>
      <div class="stat"><b style="${t.items_failed?'color:var(--bad)':''}">${hc(t.items_failed)}</b><span>failed</span></div>
      <div class="stat"><b>${hc(t.items_skipped)}</b><span>skipped</span></div>
      <div class="stat"><b>${hb(t.bytes_moved)}</b><span>moved</span></div>
      <div class="stat"><b>${t.users_done}/${t.users}</b><span>users done</span></div>
      <div class="stat"><b>${t.users_running}</b><span>running</span></div>
      <div class="stat"><b>${(t.gb_today||0).toFixed(0)}/${(t.cap_gb_total||0).toFixed(0)}GB</b><span>24h quota</span></div>
    </div>
    <div class="card" style="margin-top:0"><h3>Overall progress</h3>
      <div class="gbar" style="height:10px"><i style="width:${frac==null?0:Math.round(frac*100)}%"></i></div>
      <div class="muted" style="margin-top:4px">${frac==null?'n/a':(frac*100).toFixed(1)+'%'}
        &middot; ${t.users_done} done / ${t.users_running} running /
        ${t.users_failed} failed / ${t.users_paused} quota-paused of ${t.users}</div></div>
    <div class="card"><h3>Service progress</h3>
      <table><tr><th>Service</th><th></th><th class="num">OK</th>
      <th class="num">Skipped</th><th class="num">Failed</th></tr>${svcRows}</table></div>
    <div class="card"><h3>Active users</h3>
      ${active.length?`<table><tr><th>User</th><th>Progress</th><th class="num">Items</th>
      <th class="num">Moved</th><th>Status</th></tr>${activeRows}</table>`
      :'<div class="muted">none &mdash; press Migrate in the toolbar to start</div>'}</div>
    <div class="card"><h3>Recent failures</h3>
      ${failRows?`<table><tr><th>Time</th><th>Type</th><th>User</th>
      <th>Error</th></tr>${failRows}</table>`
      :'<div class="muted">none</div>'}</div>`;
}

function usersHTML(){
  if(!snap||!snap.snapshot)
    return `<h2>Users</h2><div class="warnbox">${
      esc(snap&&snap.error||'no data yet')}</div>`;
  const us=snap.snapshot.users||[];
  if(!us.length) return `<h2>Users</h2>
    <div class="warnbox">No identities loaded yet &mdash; run init-db on the Setup tab.</div>`;
  const rows=us.map(u=>`<tr>
    <td>${esc(u.source)}</td>
    <td style="${jc(u.status)?'color:'+jc(u.status):''}">${u.status}</td>
    <td class="num">${hc(u.drive_done)}</td><td class="num">${hc(u.mail_done)}</td>
    <td class="num">${hc(u.cal_done)}</td>
    <td class="num" style="${u.failed?'color:var(--bad)':''}">${hc(u.failed)}</td>
    <td class="num">${hb(u.bytes_moved)}</td>
    <td><div class="gbar" style="width:140px"><i style="width:${Math.round(100*(u.fraction||0))}%"></i></div></td>
    </tr>`).join('');
  return `<h2>Users (${us.length})</h2><div class="scroll"><table>
    <tr><th>Source</th><th>Status</th><th class="num">Drive</th>
    <th class="num">Mail</th><th class="num">Cal</th><th class="num">Failed</th>
    <th class="num">Moved</th><th>Progress</th></tr>${rows}</table></div>`;
}

function failuresHTML(){
  if(!snap||!snap.snapshot)
    return `<h2>Failures</h2><div class="warnbox">${
      esc(snap&&snap.error||'no data yet')}</div>`;
  const fs=snap.snapshot.failures||[];
  if(!fs.length) return `<h2>Failures</h2><div class="okbox">No failures recorded.</div>`;
  const rows=fs.map(f=>`<tr>
    <td class="muted">${esc(f.timestamp||'')}</td><td>${esc(f.item_type||'')}</td>
    <td>${esc(f.source_user||'')}</td><td class="mono">${esc(f.item_id||'')}</td>
    <td style="color:var(--bad)">${esc(f.error_message||'')}</td></tr>`).join('');
  return `<h2>Failures (${hc(fs.length)})</h2><div class="scroll"><table>
    <tr><th>Time</th><th>Type</th><th>User</th><th>Item</th><th>Error</th></tr>
    ${rows}</table></div>`;
}

function scopeHTML(){
  return `<h2>Migration scope</h2>
    <div style="display:flex;gap:8px;margin-bottom:10px">
      <button onclick="fetchScope()">Refresh</button>
      <button onclick="run('export_scope')">Export SCOPE.md</button></div>
    <pre class="mono scroll" style="margin:0">${esc(scopeLines.join('\\n'))}</pre>`;
}

function logsHTML(){
  return `<h2>Logs</h2>
    <div class="muted" style="margin-bottom:6px">${esc(logPath)}</div>
    <div class="groq">
      <b style="font-size:12px">Active log \u2014 Groq diagnosis</b>
      <div class="muted" style="margin-top:2px">Sends the current log tail and
        headline metrics to Groq for a live benchmark + error summary.
        ${groqConfigured ? 'Key saved (${esc(groqKeyMask)}).' :
          'Add a Groq API key below.'}</div>
      <div style="margin-top:6px;display:flex;gap:6px;align-items:center">
        <input id="groq-key" type="password" placeholder="gsk_..."
          style="flex:1" ${groqConfigured?'':''}>
        <button class="primary" onclick="saveGroq()">Save key</button>
        <button onclick="groqAnalyze()" id="groq-btn">Analyze log</button>
      </div>
      <div id="groqmsg" class="muted" style="margin-top:4px"></div>
      <div id="groqout" class="groqout" style="display:none"></div>
    </div>
    <pre class="mono scroll" style="margin:0">${esc(logLines.join('\\n'))}</pre>`;
}

async function saveGroq(){
  const key=$('groq-key').value.trim();
  if(!key) return;
  const r=await (await fetch('/api/groq',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({key:key})})).json();
  const m=$('groqmsg');
  m.textContent=r.ok?('Saved \u2014 '+r.msg):('\u26a0 '+r.error);
  m.style.color=r.ok?'var(--ok)':'var(--bad)';
  if(r.ok){ groqConfigured=true; $('groq-key').value=''; fetchGroqStatus(); drawView(); }
}
async function groqAnalyze(){
  const btn=$('groq-btn'), out=$('groqout'), m=$('groqmsg');
  btn.disabled=true; btn.textContent='Analyzing...';
  out.style.display='block'; out.textContent='';
  m.textContent=''; 
  try{
    const r=await (await fetch('/api/groq_log',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({prompt:$('groq-prompt')?$('groq-prompt').value:''})})).json();
    if(r.ok){
      out.style.whiteSpace='pre-wrap'; out.textContent=r.text;
    }else{
      m.textContent='\u26a0 '+r.error; m.style.color='var(--bad)';
    }
  }catch(e){
    m.textContent='Error: '+e; m.style.color='var(--bad)';
  }finally{
    btn.disabled=false; btn.textContent='Analyze log';
  }
}
async function fetchGroqStatus(){
  try{
    const r=await (await fetch('/api/groq')).json();
    groqConfigured=!!r.configured; groqKeyMask=r.key||'';
    if(tab==='logs') drawView();
  }catch(e){}
}

async function fetchScope(){
  try{
    const r=await (await fetch('/api/scope')).json();
    if(r.lines){ scopeLines=r.lines; drawView(); }
  }catch(e){}
}
async function fetchLogs(){
  try{
    const r=await (await fetch('/api/logs')).json();
    if(r.lines){ logLines=r.lines; logPath=r.path; drawView(); }
  }catch(e){}
}
async function fetchDeployHistory(){
  try{
    const r=await (await fetch('/api/deploy_history')).json();
    if(r.history){ deployHistory=r.history; if(tab==='deploy') drawView(); }
  }catch(e){}
}

async function pollSnap(){
  let ok=false;
  try{
    const r=await (await fetch('/api/snapshot')).json();
    ok=true;
    snap=r;
    applyToggles(r.toggles);
    paintProg();
    drawView();
  }catch(e){}
  ok?connOk():connFail();
  setTimeout(pollSnap,2000);
}

/* The slim always-visible strip under the header: overall migration
   progress + the headline numbers, updated with every snapshot. */
function paintProg(){
  const bar=$('progi'); if(!bar) return;
  const pct=$('progpct'), txt=$('progtxt');
  const t=snap&&snap.snapshot&&snap.snapshot.totals;
  if(!t){ bar.style.width='0%'; if(pct) pct.textContent='0%';
    if(txt) txt.textContent='no migration yet'; return; }
  const frac=Math.max(0,Math.min(1,t.fraction||0));
  bar.style.width=Math.round(frac*100)+'%';
  if(pct) pct.textContent=Math.round(frac*100)+'%';
  if(txt) txt.textContent=`${hc(t.items_done)} / ${t.items_expected?hc(t.items_expected):'?'} items`
    +` \u00b7 ${t.users_done}/${t.users} users \u00b7 ${hb(t.bytes_moved)} moved`;
}

fetch('/api/actions').then(r=>r.json()).then(a=>{
  acts=a; drawToolbar(); setTab('setup'); refresh(); pollJob(); pollSnap();
  fetchGroqStatus();
});
document.querySelectorAll('.tabs button').forEach(b=>
  b.onclick=()=>setTab(b.dataset.tab));
/* Chained, not setInterval: a slow /api/status used to let polls overlap,
   stacking concurrent work on the server and the page. */
(function loop(){ setTimeout(async()=>{ await refresh(); loop(); }, 6000); })();
(function sco(){ setTimeout(async()=>{ if(tab==='scope') await fetchScope(); sco(); },15000); })();
(function logp(){ setTimeout(async()=>{ if(tab==='logs') await fetchLogs(); logp(); },3000); })();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # This UI drives a migration; nothing about it should be cached.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _account_id(self) -> int | None:
        """None for the legacy path (no cookie, or one that doesn't resolve
        -- same handling api_server.py's operator() dependency gives an
        absent/invalid bp_session). Stdlib http.cookies rather than a hand
        rolled split on ';', for the same reason api_server.py leans on
        FastAPI's Cookie() instead of parsing the header itself: cookie
        syntax has enough edge cases (quoting, extra attributes) that a
        one-line split silently mis-parses some of them."""
        import http.cookies

        raw = self.headers.get("Cookie")
        if not raw:
            return None
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(raw)
        except http.cookies.CookieError:
            return None
        morsel = jar.get("bp_session")
        if morsel is None:
            return None
        return accounts_auth.resolve_session(morsel.value)

    _ASSET_CTYPES = {
        ".js": "application/javascript", ".css": "text/css",
        ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
        ".json": "application/json", ".woff2": "font/woff2",
    }

    def _serve_spa_index(self) -> None:
        index = os.path.join(SPA_DIST_DIR, "index.html")
        if not os.path.isfile(index):
            self._send(404, b"migration-webui not built -- run "
                             b"'npm run build' in migration-webui/ first",
                       "text/plain; charset=utf-8")
            return
        with open(index, "rb") as f:
            self._send(200, f.read(), "text/html; charset=utf-8")

    def _serve_spa_asset(self, path: str) -> None:
        # path looks like "/app/assets/index-XXXX.js" -- strip the "/app"
        # prefix the SPA is mounted under, then resolve strictly inside
        # SPA_DIST_DIR so ".." can never escape it onto the rest of the
        # filesystem this process can read (it holds tenant credentials).
        rel = path[len("/app/"):]
        target = os.path.normpath(os.path.join(SPA_DIST_DIR, rel))
        if os.path.commonpath([target, SPA_DIST_DIR]) != SPA_DIST_DIR or not os.path.isfile(target):
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        ext = os.path.splitext(target)[1]
        ctype = self._ASSET_CTYPES.get(ext, "application/octet-stream")
        with open(target, "rb") as f:
            self._send(200, f.read(), ctype)

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/":
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif path == "/api/status":
            self._json(status_payload())
        elif path == "/api/actions":
            self._json({k: {"label": v["label"], "blurb": v["blurb"],
                            "destructive": v.get("destructive", False),
                            "confirm": v.get("confirm", "")}
                        for k, v in ACTIONS.items()})
        elif path == "/api/snapshot":
            self._json(snapshot_payload())
        elif path == "/api/spa/users":
            self._json(spa_users_payload())
        elif path == "/api/spa/activity":
            self._json(spa_activity_payload())
        elif path == "/api/spa/metrics":
            self._json(spa_metrics_payload())
        elif path == "/api/spa/stages":
            self._json(spa_stages_payload())
        elif path == "/api/spa/verification":
            self._json(spa_verification_payload())
        elif path == "/api/spa/report":
            self._json(spa_report_payload())
        elif path == "/api/scope":
            self._json(scope_payload())
        elif path == "/api/logs":
            self._json(logs_payload())
        elif path == "/api/groq":
            # The panel needs to know whether a key is saved (masked) so it
            # can prompt to enter one, without ever round-tripping the real
            # key back to the browser.
            key = groq_api_key()
            self._json({
                "configured": bool(key),
                "key": (key[:4] + "\u2022" * 12) if key else "",
                "model": GROQ_MODEL,
            })
        elif path == "/api/oauth/status":
            self._json(oauth_status())
        elif path == "/api/dwd":
            self._json(dwd_payload())
        elif path == "/api/config":
            from config import Settings as _S
            self._json({"config": read_config(), "env_path": ENV_PATH,
                        "uploads": uploads_status(),
                        "auth_modes": AUTH_MODES,
                        "auth_mode": _S().auth_mode,
                        "run_mode": _S().run_mode,
                        "run_modes": _RUN_MODES(),
                        "seed_scales": list(SEED_SCALES),
                        "deploy": read_deploy_config(),
                        "host": host_info()})
        elif path == "/oauth/callback":
            # Google redirects the admin's browser back here after consent.
            tenant = "source" if "source" in (self.headers.get("Referer") or "") else None
            tenant = tenant or _PENDING.get("tenant", "source")
            full = f"http://localhost:{self.server.server_address[1]}{self.path}"
            res = oauth_finish(tenant, full)
            msg = (f"<h2>{'Connected' if res.get('ok') else 'Could not connect'}</h2>"
                   f"<p>{html.escape(res.get('account') or res.get('error',''))}</p>"
                   f"<p>You can close this tab and return to the migration UI.</p>")
            self._send(200, f"<html><body style='font:15px sans-serif;padding:40px'>"
                            f"{msg}</body></html>".encode(), "text/html; charset=utf-8")
        elif path == "/api/job":
            since = 0
            if "since=" in self.path:
                try:
                    since = int(self.path.split("since=")[1].split("&")[0])
                except ValueError:
                    since = 0
            snap = get_job(self._account_id()).snapshot(since)
            if not snap["running"]:
                ext = _external_job_snapshot()
                if ext is not None:
                    snap = ext
            self._json(snap)
        elif path == "/api/deploy_history":
            self._json({"history": load_deploy_history()})
        elif path.startswith("/app/assets/"):
            self._serve_spa_asset(path)
        elif path == "/app" or path == "/app/" or path.startswith("/app/"):
            # Client-side routes (e.g. /app/mission-control) all resolve to
            # the same index.html -- the SPA's own router takes it from
            # there, same as any other client-routed single-page app.
            self._serve_spa_index()
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 2 * MAX_UPLOAD:
            self._json({"ok": False, "error": "request body too large"}, 413)
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json({"ok": False, "error": "bad json"}, 400)
            return

        if self.path == "/api/oauth/begin":
            tenant = body.get("tenant", "")
            if tenant not in ("source", "target"):
                self._json({"ok": False, "error": "tenant must be source or target"}, 400)
                return
            _PENDING["tenant"] = tenant
            self._json(oauth_begin(tenant, self.server.server_address[1]))
            return

        if self.path == "/api/oauth/disconnect":
            from config import Settings
            import oauth_store

            tenant = body.get("tenant", "")
            if tenant not in ("source", "target"):
                self._json({"ok": False, "error": "tenant must be source or target"}, 400)
                return
            oauth_store.TokenStore(Settings().oauth_token_dir).clear(tenant)
            # Deliberately worded: the local token is gone, but the grant still
            # exists at Google until an admin revokes it in their own console.
            self._json({"ok": True,
                        "msg": f"{tenant} token removed locally; access is still "
                               f"granted at Google until revoked in the admin console"})
            return

        if self.path == "/api/seed":
            account_id = self._account_id()
            argv, env, err = seed_argv(body, account_id)
            if err:
                self._json({"ok": False, "error": err}, 400)
                return
            ok, msg = get_job(account_id).start(
                "seed", argv, env=env,
                cwd=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "data-generator"))
            self._json({"ok": ok, "error": "" if ok else msg})
            return

        if self.path == "/api/reset_target":
            account_id = self._account_id()
            argv, env, err = reset_target_argv(body, account_id)
            if err:
                self._json({"ok": False, "error": err}, 400)
                return
            # reset_target.py lives at the repo root, unlike the seeder.
            ok, msg = get_job(account_id).start("reset target", argv, env=env)
            self._json({"ok": ok, "error": "" if ok else msg})
            return

        if self.path == "/api/check":
            self._json(live_check(body.get("kind", "")))
            return

        if self.path == "/api/check_dwd":
            invalidate_status()
            _refresh_snapshot()
            with _snap_lock:
                data = _snap["data"] or {}
            self._json({"ok": True, "status": data})
            return

        if self.path == "/api/scope_diagnosis":
            tenant = (body.get("tenant") or "").strip().lower()
            if tenant not in ("source", "target"):
                self._json({"ok": False, "error": "tenant must be 'source' or 'target'"}, 400)
                return
            self._json({"ok": True, "diagnosis": scope_diagnosis(tenant)})
            return

        if self.path == "/api/checkstep":
            try:
                n = int(body.get("step", 0))
            except (TypeError, ValueError):
                self._json({"ok": False, "error": "step must be a number"}, 400)
                return
            self._json(check_step(n))
            return

        if self.path == "/api/runmode":
            self._json(set_run_mode(body.get("mode", "")))
            return

        if self.path == "/api/toggles":
            self._json(set_toggles(body))
            return

        if self.path == "/api/dwd/automate":
            tenant = body.get("tenant", "")
            if tenant not in ("source", "target"):
                self._json({"ok": False,
                            "error": "tenant must be source or target"}, 400)
                return
            from config import Settings
            import dwd_helper

            st = Settings()
            base = os.path.dirname(os.path.abspath(__file__))
            key = st.source_sa_key if tenant == "source" else st.target_sa_key
            client_id = ""
            try:
                with open(key, encoding="utf-8") as fh:
                    client_id = json.load(fh).get("client_id", "")
            except Exception:  # noqa: BLE001 - absent key is an early state
                pass
            scopes = ""
            try:
                data = dwd_helper._load_payload(tenant)
                scopes = data.get("scopes", "")
            except Exception:  # noqa: BLE001 - never break the panel
                pass
            if not client_id:
                self._json({"ok": False,
                            "error": f"upload the {tenant} service-account "
                                     f"key first (no client ID yet)"}, 400)
                return
            cmd = (f"cd {base} && "
                   f"{sys.executable} dwd_helper.py --client-id {client_id} "
                   f"--scopes {scopes!r}")
            # This server is headless (VPS), so it cannot open the browser
            # itself: hand the operator the exact command to run on a machine
            # with a display. --headful is the default inside the helper.
            self._json({"ok": True, "command": cmd, "client_id": client_id,
                        "scopes": scopes, "tenant": tenant,
                        "note": "run this on a machine with a browser"})
            return

        if self.path == "/api/authmode":
            self._json(set_auth_mode(body.get("mode", "")))
            return

        if self.path == "/api/upload":
            res = upload_credential(body.get("kind", ""), body.get("content", ""))
            self._json(res, 200 if res.get("ok") else 400)
            return

        if self.path == "/api/config":
            clean, err = validate_config(body)
            if err:
                self._json({"ok": False, "error": err}, 400)
                return
            write_config(clean)
            self._json({"ok": True, "msg": f"saved to {ENV_PATH}",
                        "config": read_config()})
            return

        if self.path == "/api/groq":
            # Save the Groq API key (case-preserved -- it is not a domain) or
            # clear it with an empty string.
            if "key" in body:
                err = save_groq_key(body.get("key", ""))
                if err:
                    self._json({"ok": False, "error": err}, 400)
                    return
                self._json({"ok": True,
                            "msg": "saved to " + ENV_PATH,
                            "configured": bool(groq_api_key())})
                return
            self._json({"ok": False, "error": "no key field"}, 400)
            return

        if self.path == "/api/groq_log":
            # Ask Groq to summarise the current log tail + headline metrics.
            key = groq_api_key()
            if not key:
                self._json({"ok": False,
                            "error": "no Groq API key — add one in the Logs panel"},
                           400)
                return
            prompt = (body.get("prompt") or "").strip()[:2000]
            if not prompt:
                prompt = ("Summarise this migration's current state for "
                          "benchmarking and error reporting.")
            tail = "\n".join(logs_payload()["lines"][-500:])
            summary = _groq_run_summary()
            text, err = _groq_analyze_log(
                f"{summary}\n\n{prompt}" if summary else prompt, prompt, key)
            if err:
                self._json({"ok": False, "error": err}, 502)
                return
            self._json({"ok": True, "text": text})
            return

        if self.path == "/api/setup":
            cfg = read_config()
            missing = [k for k, v in cfg.items() if not v]
            if missing:
                self._json({"ok": False,
                            "error": "save the domains and admins first (missing: "
                                     + ", ".join(missing) + ")"}, 400)
                return
            argv = ["bash", "setup.sh",
                    "--source-domain", cfg["source_domain"],
                    "--target-domain", cfg["target_domain"],
                    "--source-admin", cfg["source_admin"],
                    "--target-admin", cfg["target_admin"]]
            if body.get("keyless"):
                argv.append("--keyless")
            ok, msg = JOB.start("setup", argv, env=gcloud_env())
            self._json({"ok": ok, "error": "" if ok else msg})
            return

        if self.path == "/api/deploy_config":
            # Save-only: lets the VPS connection be entered once, from either
            # UI, before a Deploy is ever run -- see read_deploy_config().
            clean, err = validate_deploy_config(body)
            if err:
                self._json({"ok": False, "error": err}, 400)
                return
            write_config_raw(clean)
            self._json({"ok": True, "msg": f"saved to {ENV_PATH}"})
            return

        if self.path == "/api/deploy":
            clean, err = validate_deploy_config(body)
            if err:
                self._json({"ok": False, "error": err}, 400)
                return
            host, user = clean["DEPLOY_HOST"], clean["DEPLOY_USER"]
            port, ui_port = clean["DEPLOY_PORT"], clean["DEPLOY_UI_PORT"]
            key = clean["DEPLOY_KEY"]
            # Copying credentials to another machine is outward-facing and not
            # undoable, so it takes the same typed confirmation as a migration.
            creds = bool(body.get("include_credentials"))
            if creds and body.get("confirm") != "DEPLOY":
                self._json({"ok": False,
                            "error": "sending credentials to a host needs the "
                                     "confirmation phrase DEPLOY"}, 400)
                return
            # Remembered for next time regardless of how this run turns out --
            # a failed deploy (bad password prompt, network blip) still means
            # the operator typed a real host worth keeping.
            write_config_raw(clean)
            argv = [PY, "deploy_remote.py", "--host", host, "--user", user,
                    "--port", port, "--ui-port", ui_port]
            if key:
                argv += ["--key", key]
            if creds:
                argv.append("--include-credentials")
            rec_id = record_deploy_start(host, user, port, ui_port, creds,
                                         host_info().get("commit", ""))
            ok, msg = JOB.start("deploy", argv,
                                on_finish=lambda rc: record_deploy_finish(rec_id, rc))
            if not ok:
                # Never started -- e.g. another job already running -- so
                # there is no process to ever call on_finish. Recording it
                # as its own immediate failure keeps the history honest
                # instead of leaving a permanently "in progress" ghost.
                record_deploy_finish(rec_id, None)
            self._json({"ok": ok, "error": "" if ok else msg})
            return

        if self.path == "/api/stop":
            if JOB.running:
                self._json({"ok": True, "msg": JOB.stop()})
            else:
                jobs = _external_processes()
                if not jobs:
                    self._json({"ok": True, "msg": "nothing running"})
                else:
                    sent = []
                    for j in jobs:
                        try:
                            # Same cooperative SIGINT the webui's own Stop uses:
                            # the engine finishes in-flight items, then resumes.
                            os.kill(j["pid"], signal.SIGINT)
                            sent.append(str(j["pid"]))
                        except (ProcessLookupError, PermissionError):
                            pass
                    msg = (f"interrupt sent to {len(sent)} external "
                           f"process(es): {', '.join(sent)}") if sent \
                        else "external process(es) already gone"
                    self._json({"ok": True, "msg": msg})
            return

        if self.path != "/api/run":
            self._json({"ok": False, "error": "not found"}, 404)
            return

        name = body.get("action", "")
        spec = ACTIONS.get(name)
        if not spec:
            # The whitelist is the security boundary: an unknown name is
            # simply not runnable, whatever it contains.
            self._json({"ok": False, "error": f"unknown action {name!r}"}, 400)
            return
        if spec.get("destructive") and body.get("confirm") != spec.get("confirm"):
            self._json({"ok": False,
                        "error": f"{spec['label']} needs confirmation"}, 400)
            return

        env = _service_env() if name in _PHASE_GATED_ACTIONS else gcloud_env()
        ok, msg = JOB.start(spec["label"], _action_argv(name), env=env)
        self._json({"ok": ok, "error": None if ok else msg})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Local web UI for the migration.")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1",
                    help="leave this alone unless you know exactly why")
    args = ap.parse_args(argv)

    # Load env.sh before serving anything.
    #
    # Settings reads os.environ at construction. Previously the environment
    # only got populated as a side effect of the first status poll (State()
    # does it), so any request arriving before that -- a credential test, a
    # seed -- saw defaults instead of the configured domains and admins, and
    # reported the admin address as unset while it sat in env.sh all along.
    # Configuration must not depend on which endpoint happened to be hit first.
    try:
        from wizard import load_env

        loaded = load_env(ENV_PATH)
        for key, value in loaded.items():
            os.environ.setdefault(key, value)
        if loaded:
            print(f"loaded {len(loaded)} setting(s) from {ENV_PATH}")
    except Exception as exc:  # noqa: BLE001 - the UI should still start
        print(f"could not read {ENV_PATH}: {exc}")

    if args.host not in ("127.0.0.1", "localhost"):
        print("\n*** WARNING ***")
        print(f"Binding to {args.host} exposes command execution on this host to")
        print("anyone who can reach that address. There is no authentication here")
        print("by design -- the loopback bind IS the authentication. Use an SSH")
        print("tunnel instead:\n")
        print(f"    ssh -L {args.port}:localhost:{args.port} root@this-host\n")

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Migration UI on http://{args.host}:{args.port}")
    print(f"\nFrom your laptop:\n    ssh -L {args.port}:localhost:{args.port} "
          f"root@<this-host>\nthen open http://localhost:{args.port}\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
