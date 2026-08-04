"""
wizard.py
=========
Guided end-to-end walkthrough: two Workspace domains in, a verified migration
out.

The design point is that every step **detects its own state** rather than
trusting a checklist. Re-run this at any time and it works out where you
actually are -- which matters because most of the friction in this process is
not doing the steps, it is knowing which ones already happened. A half-finished
setup that you come back to tomorrow should not need you to remember anything.

Steps that a human must do (there is exactly one that genuinely cannot be
automated -- delegation) are shown with the precise strings to paste, and the
wizard then polls until it sees the result and continues on its own.

    python3 wizard.py                 # walk through / resume
    python3 wizard.py --status        # just show where things stand
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

# ----------------------------------------------------------------------
# Presentation
# ----------------------------------------------------------------------
_TTY = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


BOLD = lambda s: _c("1", s)          # noqa: E731
DIM = lambda s: _c("2", s)           # noqa: E731
GREEN = lambda s: _c("32", s)        # noqa: E731
YELLOW = lambda s: _c("33", s)       # noqa: E731
RED = lambda s: _c("31", s)          # noqa: E731
CYAN = lambda s: _c("36", s)         # noqa: E731

DONE, TODO, MANUAL, ACTIVE, SKIP = ("done", "todo", "manual",
                                   "active", "skip")
MARK = {DONE: GREEN("✓"), TODO: DIM("○"), MANUAL: YELLOW("!"),
        ACTIVE: CYAN("▸"), SKIP: DIM("–")}


def rule(title: str = "") -> None:
    width = min(shutil.get_terminal_size((80, 24)).columns, 78)
    print(DIM("─" * width))
    if title:
        print(BOLD(title))
        print(DIM("─" * width))


def bar(done: int, total: int, width: int = 30) -> str:
    if total <= 0:
        return DIM("─" * width + "   n/a")
    frac = min(1.0, done / total)
    filled = int(round(frac * width))
    return f"[{'#' * filled}{'.' * (width - filled)}] {frac * 100:5.1f}%"


# ----------------------------------------------------------------------
# Environment
# ----------------------------------------------------------------------
def load_env(path: str = "env.sh") -> dict:
    """Read the KEY=VALUE pairs out of env.sh without executing it."""
    out: dict[str, str] = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line.startswith("export ") or "=" not in line:
                continue
            k, v = line[len("export "):].split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


# ----------------------------------------------------------------------
# Finding gcloud
# ----------------------------------------------------------------------
# `shutil.which` only sees the PATH of the *current process*. That is a bad
# proxy for "is gcloud installed", because the SDK's installer does not put
# anything on a system-wide PATH -- it appends a `source .../path.zsh.inc`
# line to the user's shell profile. So gcloud works in the terminal and is
# invisible to anything not launched from an interactive login shell: the web
# UI under nohup, a GUI launch, a systemd unit, cron.
#
# Reading that profile line is what actually answers the question, and it is
# the only method that copes with the SDK being unpacked somewhere arbitrary
# (its installer asks where to put it, and people say yes to whatever the
# current directory happens to be).
_PROFILES = ("~/.zshrc", "~/.zprofile", "~/.bash_profile", "~/.bashrc",
             "~/.profile", "~/.config/fish/config.fish")

# The SDK's own PATH snippet, in single or double quotes. Paths may contain
# spaces -- "google-cloud-sdk 2" is what a second download is named.
_INC_RE = re.compile(r"""['"]([^'"]*?[/\\]path\.(?:zsh|bash|fish)\.inc)['"]""")

_KNOWN = (
    "~/google-cloud-sdk/bin/gcloud",
    "/opt/homebrew/bin/gcloud",
    "/opt/homebrew/share/google-cloud-sdk/bin/gcloud",
    "/usr/local/bin/gcloud",
    "/usr/local/share/google-cloud-sdk/bin/gcloud",
    "/usr/local/Caskroom/google-cloud-sdk/latest/google-cloud-sdk/bin/gcloud",
    "/usr/lib/google-cloud-sdk/bin/gcloud",
    "/snap/bin/gcloud",
    "~/Downloads/google-cloud-sdk/bin/gcloud",
)


def _usable(path: str) -> bool:
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def find_gcloud() -> tuple[str, str]:
    """
    Locate the gcloud binary. Returns (path, how_it_was_found).

    Order matters: PATH first so an explicit choice wins, then the shell
    profile (authoritative, since the installer wrote it), then well-known
    locations as a last resort.
    """
    env = os.environ.get("GCLOUD_BIN", "")
    if _usable(env):
        return env, "GCLOUD_BIN"

    found = shutil.which("gcloud")
    if found:
        return found, "PATH"

    for prof in _PROFILES:
        p = os.path.expanduser(prof)
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        for inc in _INC_RE.findall(text):
            # .../google-cloud-sdk/path.zsh.inc -> .../google-cloud-sdk/bin/gcloud
            cand = os.path.join(os.path.dirname(os.path.expanduser(inc)),
                                "bin", "gcloud")
            if _usable(cand):
                return cand, f"{os.path.basename(p)}"

    for cand in _KNOWN:
        cand = os.path.expanduser(cand)
        if _usable(cand):
            return cand, "standard location"

    return "", ""


def sh(cmd: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, "command not found"
    except subprocess.TimeoutExpired:
        return 124, "timed out"


# ----------------------------------------------------------------------
# State detection — each check answers "is this already true?"
# ----------------------------------------------------------------------
class State:
    def __init__(self) -> None:
        self.env = load_env()
        for k, v in self.env.items():
            os.environ.setdefault(k, v)
        self.notes: dict[str, str] = {}
        self.gcloud = ""
        # preflight mints a token per user against both tenants -- slow, and
        # several steps ask about it. Answer it once per render.
        self._preflight: bool | None = None

    # -- 1 --------------------------------------------------------------
    def gcloud_ready(self) -> bool:
        path, how = find_gcloud()
        self.gcloud = path
        if not path:
            self.notes["gcloud"] = "gcloud not found on PATH or in any shell profile"
            return False
        rc, out = sh([path, "auth", "list",
                      "--filter=status:ACTIVE", "--format=value(account)"])
        if rc == 0 and out:
            # Say where it was found when it was not simply on PATH -- that is
            # the difference between "it works" and "it works only in your
            # terminal", and the operator should know which one they have.
            via = "" if how == "PATH" else f" (found via {how})"
            self.notes["gcloud"] = f"authenticated as {out.splitlines()[0]}{via}"
            return True
        self.notes["gcloud"] = f"found at {path} but not authenticated — gcloud auth login"
        return False

    # -- 2 --------------------------------------------------------------
    def env_written(self) -> bool:
        need = ["SOURCE_DOMAIN", "TARGET_DOMAIN", "SOURCE_ADMIN", "TARGET_ADMIN"]
        missing = [k for k in need if not self.env.get(k)]
        if missing:
            self.notes["env"] = "env.sh missing: " + ", ".join(missing)
            return False
        self.notes["env"] = (f"{self.env['SOURCE_DOMAIN']} -> "
                             f"{self.env['TARGET_DOMAIN']}")
        return True

    # -- 3 --------------------------------------------------------------
    def credentials_ready(self) -> bool:
        """
        Resolved through `config.Settings`, never straight from env.sh.

        Settings is what the engine authenticates with and what the web UI
        writes uploads to, and it supplies defaults (`./keys/source-sa.json`)
        when the environment says nothing. Reading the raw environment here
        instead meant a key uploaded to exactly the right place was reported
        missing, because nothing had written SOURCE_SA_KEY into env.sh -- the
        wizard and the uploader disagreeing about the same file.
        """
        from config import Settings

        st = Settings()
        mode = st.auth_mode

        if mode == "impersonate":
            have = st.source_sa_email and st.target_sa_email
            self.notes["creds"] = ("keyless impersonation" if have
                                   else "keyless, but *_SA_EMAIL not set")
            return bool(have)

        if mode == "oauth":
            # An OAuth run is ready when both tenants have consented; the
            # client secrets file alone grants nothing.
            import oauth_store

            store = oauth_store.TokenStore(st.oauth_token_dir)
            connected = [t for t in ("source", "target") if store.exists(t)]
            if len(connected) == 2:
                self.notes["creds"] = "both tenants connected via OAuth"
                return True
            if not os.path.exists(st.oauth_client_secrets):
                self.notes["creds"] = (
                    f"no OAuth client at {st.oauth_client_secrets}")
            else:
                missing = [t for t in ("source", "target") if t not in connected]
                self.notes["creds"] = "not connected: " + ", ".join(missing)
            return False

        pairs = [("source", st.source_sa_key), ("target", st.target_sa_key)]
        missing = [n for n, p in pairs if not (p and os.path.exists(p)
                                               and os.path.getsize(p) > 0)]
        if missing:
            self.notes["creds"] = "missing key file(s): " + ", ".join(missing)
            return False
        self.notes["creds"] = "both key files present"
        return True

    # -- 4 --------------------------------------------------------------
    def delegation_ok(self) -> bool:
        """The only real test is minting a token and making a call."""
        if self._preflight is not None:
            return self._preflight
        if not self.credentials_ready() or not self.identities_loaded():
            self.notes["dwd"] = "needs credentials and identity_map first"
            self._preflight = False
            return False
        rc, out = sh([sys.executable, "main.py", "preflight"], timeout=180)
        if rc == 0:
            self.notes["dwd"] = "preflight passed for every mapped user"
            self._preflight = True
            return True
        # Distinguish "not granted yet" from "granted but the accounts are
        # broken" -- they look identical in a pass/fail and need opposite fixes.
        if "unauthorized_client" in out:
            hint = "scopes not authorised yet (or a partial paste)"
        elif "Active session is invalid" in out:
            hint = ("accounts unreachable — suspended, pending login challenge, "
                    "or an expired trial")
        elif "invalid_grant" in out:
            # Check if DWD is authorised for super admin accounts even if mapped users don't exist yet
            try:
                from config import Settings
                from auth import AuthManager
                st = Settings()
                am = AuthManager(st)
                ok_s, _ = am.verify_delegation("source", st.source_admin) if st.source_admin else (False, "")
                ok_t, _ = am.verify_delegation("target", st.target_admin) if st.target_admin else (False, "")
                if ok_s and ok_t:
                    self.notes["dwd"] = "preflight passed for admin accounts (mapped users need provisioning)"
                    self._preflight = True
                    return True
            except Exception:
                pass
            hint = "an account in identity_map does not exist"
        else:
            first = next((l for l in out.splitlines() if "FAIL" in l), "")
            hint = first[:70] or "see: python3 main.py preflight"
        self.notes["dwd"] = f"preflight failing — {hint}"
        self._preflight = False
        return False


    # -- 5 --------------------------------------------------------------
    def identities_loaded(self) -> int:
        db = self.env.get("MIGRATION_DB", "migration.db")
        if not os.path.exists(db):
            self.notes["identities"] = "migration.db does not exist yet"
            return 0
        import sqlite3
        try:
            con = sqlite3.connect(db)
            n = con.execute("SELECT COUNT(*) FROM identity_map").fetchone()[0]
            rows = con.execute(
                "SELECT source_email, target_email FROM identity_map").fetchall()
            con.close()
        except Exception:
            return 0

        # A migration.db outlives the run that created it. Reusing a directory
        # for a second tenant pair leaves the previous map in place, and every
        # later command quietly targets the wrong tenant's users.
        try:
            from config import Settings
            from main import identity_domain_mismatch

            mismatch = identity_domain_mismatch(
                [{"source_email": a, "target_email": b} for a, b in rows],
                Settings())
        except Exception:  # noqa: BLE001 - detection must never break the view
            mismatch = ""

        if mismatch:
            self.notes["identities"] = f"{n} mapping(s) — WRONG TENANTS: {mismatch}"
            # Not "done": acting on this map would migrate the wrong accounts.
            return 0

        self.notes["identities"] = f"{n} identity mapping(s)"
        return n

    # -- 6 --------------------------------------------------------------
    def migration_progress(self) -> tuple[int, int, int]:
        """(succeeded, failed, users_done) straight from the ledger."""
        db = self.env.get("MIGRATION_DB", "migration.db")
        if not os.path.exists(db):
            return 0, 0, 0
        import sqlite3
        try:
            con = sqlite3.connect(db)
            ok = con.execute(
                "SELECT COUNT(*) FROM audit_log WHERE status='SUCCESS'"
            ).fetchone()[0]
            bad = con.execute(
                "SELECT COUNT(*) FROM audit_log WHERE status LIKE 'FAILED%'"
            ).fetchone()[0]
            users = con.execute(
                "SELECT COUNT(*) FROM identity_map WHERE status='DONE'"
            ).fetchone()[0]
            con.close()
            return ok, bad, users
        except Exception:
            return 0, 0, 0


# ----------------------------------------------------------------------
# Steps
# ----------------------------------------------------------------------
# Which steps belong to which kind of run. A step listed here is shown as
# "not needed" rather than outstanding -- a real migration should never present
# seeding as a task left to do, and a seed-only run should not sit at 3/9
# forever waiting for a migration nobody intends to perform.
RUN_MODES = {
    "migrate_only": {
        "label": "Migrate only",
        "blurb": "Move an existing tenant's data. The normal case.",
        "skip": [7],
        # Steps that must be satisfied before this path can run. Everything
        # else is noise for someone who only wants this one outcome.
        "requires": [2, 3, 4, 5],
        "runs": ["discover", "migrate_dry", "migrate", "verify", "acl_audit"],
        "setup": [
            "Two Workspace domains, and a super-admin account in each.",
            "A service-account key per tenant (or keyless / OAuth) — step 3.",
            "SOURCE delegation: the read scopes, plus gmail.settings.basic for "
            "signatures and filters, plus chat.spaces/chat.messages for Chat.",
            "TARGET delegation: the write scopes, same optional extras.",
            "Target accounts must already exist, or be created at step 6.",
            "Google Chat switched ON in both Admin Consoles, and a Chat app "
            "configured, if Chat is to migrate at all.",
            "Target Drive sharing settings permissive enough for the source's "
            "external shares — otherwise those ACLs fail with domainPolicy.",
        ],
    },
    "seed_only": {
        "label": "Seed only",
        "blurb": "Fill a sandbox source tenant with test data. Migrates nothing.",
        "skip": [6, 8, 9],
        "requires": [2, 3, 5],
        "runs": ["seed"],
        "setup": [
            "A THROWAWAY source domain. This writes fabricated data into a "
            "live tenant and is not reversible except by --reset.",
            "A service-account key for that tenant — step 3.",
            "SOURCE delegation with the WRITE scopes: drive, calendar, "
            "gmail.insert/labels/modify. The read-only migration line is not "
            "enough, and that editor replaces rather than appends.",
            "admin.directory.user (write) as well, if the five test accounts "
            "do not exist yet.",
            "At least two accounts: the corpus builds a cross-user sharing "
            "graph, so a single user has nobody to share with.",
        ],
    },
    "seed_and_migrate": {
        "label": "Seed, then migrate",
        "blurb": "Build a test corpus and move it. The full rehearsal.",
        "skip": [],
        "requires": [2, 3, 4, 5],
        "runs": ["seed", "discover", "migrate_dry", "migrate", "verify", "acl_audit"],
        "setup": [
            "Everything in both lists above.",
            "The SOURCE grant must carry the union: read scopes for migrating "
            "AND write scopes for seeding. Pasting either alone breaks the "
            "other half with unauthorized_client.",
            "Chat: switched on in both orgs plus a Chat app, or the Chat half "
            "of the rehearsal proves nothing.",
        ],
    },
}


def build_steps(st: State, run_mode: str = "") -> list[dict]:
    ok_count, failed, users_done = st.migration_progress()
    total_users = st.identities_loaded()

    if not run_mode:
        try:
            from config import Settings
            run_mode = Settings().run_mode
        except Exception:  # noqa: BLE001
            run_mode = "migrate_only"
    skipped = set(RUN_MODES.get(run_mode, RUN_MODES["migrate_only"])["skip"])

    steps = [
        {
            "n": 1, "title": "gcloud available and authenticated",
            "state": DONE if st.gcloud_ready() else MANUAL,
            "note": st.notes.get("gcloud", ""),
            "help": [
                "Install: https://cloud.google.com/sdk/docs/install",
                "Then:    gcloud auth login",
                "",
                "Or run this whole wizard from Cloud Shell, which is already",
                "authenticated as you.",
            ],
        },
        {
            "n": 2, "title": "Projects, service accounts, env.sh",
            "state": DONE if st.env_written() else TODO,
            "note": st.notes.get("env", ""),
            "auto": ("./setup.sh --source-domain <SRC> --target-domain <TGT> "
                     "--source-admin <ADMIN> --target-admin <ADMIN>"),
            "help": [
                "setup.sh creates both GCP projects, enables the APIs, creates",
                "both service accounts and writes env.sh.",
                "",
                "It also handles the two things that trip people up: service",
                "account creation is eventually consistent (it waits), and",
                "Google now blocks key downloads by default on new orgs (it",
                "falls back to keyless automatically).",
            ],
        },
        {
            "n": 3, "title": "Credentials usable",
            "state": DONE if st.credentials_ready() else TODO,
            "note": st.notes.get("creds", ""),
            "help": [
                "Either two key files, or keyless impersonation.",
                "",
                "If key creation was blocked by org policy, re-run setup.sh",
                "with --keyless. That needs no key, no policy change and no",
                "Organization Administrator role.",
            ],
        },
        {
            "n": 4, "title": "Identity map loaded",
            "state": DONE if total_users else TODO,
            "note": st.notes.get("identities", ""),
            "auto": "python3 main.py init-db --identities identities.csv",
            "help": [
                "A CSV of source_email,target_email,entity_type.",
                "The seeder writes one; or use --auto-map to match localparts",
                "across both directories.",
            ],
        },
        {
            "n": 5, "title": "Domain-Wide Delegation authorised",
            "state": DONE if st.delegation_ok() else MANUAL,
            "note": st.notes.get("dwd", ""),
            "manual": True,
            "help": [
                "THE ONE STEP THAT CANNOT BE AUTOMATED — by anyone, in any tool.",
                "Google provides no API for it on purpose: this is the grant",
                "that lets a credential act as every user in the organisation.",
                "",
                "admin.google.com -> Security -> Access and data control",
                "  -> API controls -> MANAGE DOMAIN WIDE DELEGATION -> Add new",
                "",
                "Run  python3 main.py scope  for the exact scope lines your",
                "current configuration needs, including any optional passes.",
                "",
                "Paste each line WHOLE — that editor replaces rather than",
                "appends, and a partial paste is the most common cause of",
                "everything failing with unauthorized_client.",
                "",
                "Grants take ~2 minutes to propagate, occasionally 30.",
            ],
        },
        {
            "n": 6, "title": "User accounts exist in both tenants",
            "state": DONE if (total_users and st.delegation_ok()) else TODO,
            "note": "provision-users creates any that are missing",
            "auto": "python3 main.py provision-users --tenant target --dry-run",
            "help": [
                "The engine maps identities, it does not create them during a",
                "migration — provisioning is its own command so that copying",
                "files can never be the thing that creates a licensed account.",
                "",
                "It only ever creates. An address that already exists is left",
                "untouched.",
            ],
        },
        {
            "n": 7, "title": "Source seeded (test tenants only)",
            "state": TODO,
            "note": "skip this for a real migration — the data is already there",
            "auto": ("cd data-generator && python3 seed_sandbox.py "
                     "--confirm-domain <SRC> --scale medium"),
            "help": [
                "Builds a five-user organisation with a cross-user sharing",
                "graph, mail, calendars, comments, drafts and the edge cases",
                "that have broken migrations before.",
                "",
                "Refuses to run unless SANDBOX_MODE=true, --confirm-domain",
                "matches, and the domain is not in PROTECTED_DOMAINS.",
            ],
        },
        {
            "n": 8, "title": "Migration",
            "state": (DONE if (users_done and users_done >= total_users and total_users)
                      else ACTIVE if ok_count else TODO),
            "note": (f"{ok_count:,} migrated, {failed} failed, "
                     f"{users_done}/{total_users} users done"
                     if ok_count or total_users else "not started"),
            "auto": "python3 main.py --dry-run migrate   # then without --dry-run",
            "help": [
                "Always dry-run first: it logs every intended write and",
                "performs none.",
                "",
                "Safe to interrupt — in-flight items finish, state commits, and",
                "a re-run resumes rather than duplicating.",
                "",
                "Watch it live from another terminal:  python3 tui.py",
            ],
        },
        {
            "n": 9, "title": "Reconciliation",
            "state": TODO,
            "note": "asks the target directly rather than trusting the ledger",
            "auto": "python3 verify.py --samples 25",
            "help": [
                "report tells you what the engine believes happened.",
                "verify.py asks the target tenant and compares against source.",
                "",
                "Exits non-zero on any failed check, so it can gate a cutover:",
                "  python3 verify.py || { echo 'HOLD CUTOVER'; exit 1; }",
            ],
        },
    ]

    for step in steps:
        if step["n"] in skipped:
            step["state"] = SKIP
            step["note"] = "not needed for this run"
            step["skipped"] = True
    return steps


# ----------------------------------------------------------------------
def show(steps: list[dict], st: State) -> None:
    rule("Google Workspace tenant-to-tenant migration")
    done = sum(1 for s in steps if s["state"] == DONE)
    print(f"  {bar(done, len(steps))}   {done}/{len(steps)} steps complete\n")

    for s in steps:
        print(f"  {MARK[s['state']]} {s['n']}. {BOLD(s['title'])}")
        if s.get("note"):
            print(f"      {DIM(s['note'])}")
    print()

    nxt = next((s for s in steps if s["state"] in (TODO, MANUAL, ACTIVE)), None)
    if not nxt:
        rule()
        print(GREEN(BOLD("  Everything is done.")))
        return

    rule(f"NEXT — step {nxt['n']}: {nxt['title']}")
    if nxt["state"] == MANUAL:
        print(YELLOW("  This one needs you.\n"))
    for line in nxt.get("help", []):
        print(f"  {line}")
    if nxt.get("auto"):
        print()
        print(BOLD("  Run:"))
        print(f"    {CYAN(nxt['auto'])}")
    print()

    ok_count, failed, _ = st.migration_progress()
    if failed:
        print(RED(f"  {failed} item(s) marked FAILED — "
                  f"python3 resolve_failures.py --dry-run"))
        print()


def watch(st: State, interval: int = 20) -> None:
    """Re-detect on a loop, so a manual step is picked up without a re-run."""
    try:
        while True:
            os.system("clear" if os.name != "nt" else "cls")
            st.__init__()  # re-read env.sh and re-detect
            steps = build_steps(st)
            show(steps, st)
            if all(s["state"] == DONE for s in steps):
                return
            print(DIM(f"  re-checking every {interval}s — Ctrl-C to stop"))
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nstopped.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Guided migration setup and run.")
    ap.add_argument("--status", action="store_true",
                    help="print current state and exit")
    ap.add_argument("--watch", action="store_true",
                    help="keep re-checking, so manual steps are picked up live")
    ap.add_argument("--interval", type=int, default=20)
    args = ap.parse_args(argv)

    st = State()
    if args.watch:
        watch(st, args.interval)
        return 0
    show(build_steps(st), st)
    if not args.status:
        print(DIM("  python3 wizard.py --watch   to follow along live\n"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
