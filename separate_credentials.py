"""
separate_credentials.py -- seed and migrate each get their own service account on
the source, each delegated EXACTLY the scopes its own job requests.

Domain-wide delegation is one scope list per service-account client ID. With one
key on the source, the seeder's write scopes (drive, gmail.insert, chat.spaces,
admin.directory.user ...) and the migration's read-only ones had to share that
list -- so a migration's source credential could write, the guarantee the tool
rests on was untrue, and "narrow for migrate" (an overwrite) was the only way back,
undone by the next seed and up to 15 minutes of propagation each way.

Two keys end that:
  migrate  the account's source-sa.json  -> migrate_scopes(): what a production
           migration requests from the source, in either transfer mode offered
  seed     seed-sa.json, beside it       -> seed_scopes(): what the seeder and its
           reset tooling request, and nothing it does not

    python separate_credentials.py --account-id 3            # create, grant both, verify
    python separate_credentials.py --account-id 3 --dry-run  # say what it would do

Unattended: the seed service account is created with gcloud signed in as the
source admin in a headless browser (gcloud_browser_auth -- revoked afterwards),
and both console entries are written by dwd_helper with the client ID checked
before Authorize and the row read back after. Logins come from the root-only
/etc/bitport/dwd.env, never argv.
"""
from __future__ import annotations

import argparse
import dataclasses
import glob
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
SEED_SA = "seed-sa"
OFFERED_TRANSFER_MODES = ("download_upload", "server_side")   # link_flip is never offered
PROPAGATION_WAIT_SEC = 15 * 60


def log(msg: str) -> None:
    print(msg, flush=True)


def migrate_scopes(settings) -> list[str]:
    """Exactly what a migration requests from the source (verify_scopes owns it: the
    same list the run's own gate checks). Not a .readonly suffix rule: server-side
    copy needs `drive` and the SSO pass admin.directory.user.security."""
    import verify_scopes
    return verify_scopes.migrate_source_scopes(settings)


def seed_scopes() -> list[str]:
    """Exactly what the seeder and its source reset request: its corpus writes, plus
    account creation, headcount, groups, licence usage, purging mail and chat."""
    sys.path.insert(0, os.path.join(HERE, "data-generator"))
    from seed_sandbox import (SEED_SCOPES, GROUP_WRITE_SCOPE, REPORTS_SCOPE, GMAIL_PURGE_SCOPE,
                              DIRECTORY_READONLY_SCOPE, GMAIL_SETTINGS_SCOPE)
    from provision import DIRECTORY_WRITE_SCOPE
    extra = {GROUP_WRITE_SCOPE, REPORTS_SCOPE, GMAIL_PURGE_SCOPE, DIRECTORY_READONLY_SCOPE,
             GMAIL_SETTINGS_SCOPE, DIRECTORY_WRITE_SCOPE,
             "https://www.googleapis.com/auth/gmail.settings.sharing",
             "https://www.googleapis.com/auth/chat.delete"}
    return sorted(set(SEED_SCOPES) | extra)


def _key(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def seed_key_path(settings) -> str | None:
    """The seed key for this source tenant, wherever it was made: the same project as
    the source key. Two accounts can share one source tenant (and its key), and a
    seed key made under one serves the other."""
    try:
        project = _key(settings.source_sa_key).get("project_id")
    except (OSError, ValueError, TypeError):
        return None
    mine = os.path.join(os.path.dirname(settings.source_sa_key), "seed-sa.json")
    for path in [mine] + sorted(glob.glob(os.path.join(HERE, "keys", "*", "seed-sa.json"))):
        try:
            if os.path.getsize(path) and _key(path).get("project_id") == project:
                return path
        except (OSError, ValueError):
            continue
    return None


def _create_seed_key(settings, dest: str, login: dict) -> tuple[bool, str]:
    """The service account and its key, made with gcloud signed in as the source admin."""
    import gcloud_browser_auth
    import provision_gcp
    project = _key(settings.source_sa_key)["project_id"]
    ok, detail, cfg = gcloud_browser_auth.login(login["DWD_EMAIL_SOURCE"], login["DWD_PASSWORD_SOURCE"])
    if not ok:
        return False, f"gcloud sign-in as the source admin failed: {detail}"
    try:
        env = dict(os.environ, CLOUDSDK_CONFIG=cfg)
        steps: list = []
        email = provision_gcp.ensure_service_account(project, SEED_SA, steps, False, env=env)
        made = provision_gcp.create_key(project, email, dest, steps, False, False, env=env)
        for s in steps:
            log(f"  {s.name}: {s.status} {s.detail}")
        return made and os.path.isfile(dest), email
    finally:
        gcloud_browser_auth.cleanup(cfg)


def _grant(account_id, client_id: str, scopes: list[str], key: str, login: dict) -> bool:
    """Write the entry as exactly `scopes` (--no-merge: this IS the whole list), then
    dwd_helper reads the row back from the console."""
    argv = [PY, os.path.join(HERE, "dwd_helper.py"), "--tenant", "source", "--client-id", client_id,
            "--scopes", ",".join(scopes), "--no-merge", "--key", key]
    if account_id:
        argv += ["--account-id", str(account_id)]
    env = dict(os.environ, **login)
    env.setdefault("DISPLAY", os.getenv("BITPORT_XVFB_DISPLAY", ":99"))
    return subprocess.run(argv, cwd=HERE, env=env).returncode == 0


def _probe(settings, key: str, scopes: list[str]) -> dict[str, bool]:
    import verify_scopes
    st = dataclasses.replace(settings, source_sa_key=key)
    return {r["scope"]: bool(r["ok"]) for r in verify_scopes.verify(st, "source", scopes)}


def separate(account_id: int | None, dry_run: bool = False, wait: int = PROPAGATION_WAIT_SEC,
             settings=None) -> int:
    from config import Settings
    import scope_guard
    st = settings or (Settings(account_id=account_id) if account_id else Settings())
    migrate, seed = migrate_scopes(st), seed_scopes()
    migrate_client = _key(st.source_sa_key).get("client_id", "")
    key = seed_key_path(st)
    log(f"source {st.source_domain}: migrate key {st.source_sa_key} (client {migrate_client}), "
        f"{len(migrate)} scope(s); seed key {key or '(to be created)'}, {len(seed)} scope(s)")
    if dry_run:
        log("dry run: nothing changed")
        return 0
    login = scope_guard._console_login(st, "source")
    if not login:
        log(f"REFUSING: no source admin console login on file for {st.source_domain} "
            "(DWD_EMAIL_SOURCE / DWD_PASSWORD_SOURCE in /etc/bitport/dwd.env)")
        return 2
    if not key:
        key = os.path.join(os.path.dirname(st.source_sa_key), "seed-sa.json")
        ok, detail = _create_seed_key(st, key, login)
        if not ok:
            log(f"FAILED to create the seed key: {detail}")
            return 3
        log(f"created the seed key {key} ({detail})")
    seed_client = _key(key).get("client_id", "")
    if not seed_client or seed_client == migrate_client:
        log("REFUSING: the seed key has no client ID of its own")
        return 4
    # Seed first: narrowing the migrate entry takes the write scopes away from
    # whatever still seeds with it, so the seed entry has to exist before that.
    if not _grant(account_id, seed_client, seed, key, login):
        log("FAILED to write the seed entry")
        return 5
    if not _grant(account_id, migrate_client, migrate, st.source_sa_key, login):
        log("FAILED to write the migrate entry")
        return 6
    # Propagation takes up to ~15 minutes. The run is done when the seed key holds
    # every seed scope and the migrate key no longer holds a write scope it does not use.
    writes = sorted(set(seed) - set(migrate))
    deadline = time.time() + wait
    while True:
        seed_ok = _probe(st, key, seed)
        migrate_extra = [s for s, ok in _probe(st, st.source_sa_key, writes).items() if ok]
        missing = [s for s, ok in seed_ok.items() if not ok]
        if not missing and not migrate_extra:
            log(f"done: seed key holds all {len(seed)} seed scope(s); the migrate key holds "
                f"none of the {len(writes)} write scope(s) it does not use")
            return 0
        if time.time() > deadline:
            log(f"propagation not finished after {wait // 60} min: seed missing {missing}; "
                f"migrate still holds {migrate_extra}. Google can take longer; re-run to check.")
            return 7
        time.sleep(30)


def narrow_if_wide(settings) -> str:
    """The migrate gate's safety net: a seed key exists, yet the migration's own key
    can still mint a seed write scope -- something re-widened it. Write it back to
    exactly the migrate set. Returns what it did, "" when nothing was needed."""
    import scope_guard
    key = seed_key_path(settings)
    if not key:
        return ""
    writes = sorted(set(seed_scopes()) - set(migrate_scopes(settings)))
    live = [s for s, ok in _probe(settings, settings.source_sa_key, writes[:4]).items() if ok]
    if not live:
        return ""
    login = scope_guard._console_login(settings, "source")
    if not login:
        return f"the source key still holds {', '.join(live)} and no source admin login is on file to narrow it"
    client = _key(settings.source_sa_key).get("client_id", "")
    ok = _grant(getattr(settings, "account_id", None), client, migrate_scopes(settings),
                settings.source_sa_key, login)
    return (f"narrowed the source key back to exactly the migration's {len(migrate_scopes(settings))} scope(s)"
            f" (it held {', '.join(live)})" if ok else "could not narrow the source key")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--account-id", type=int)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--wait", type=int, default=PROPAGATION_WAIT_SEC,
                    help="seconds to wait for Google to propagate both entries")
    a = ap.parse_args(argv)
    return separate(a.account_id, a.dry_run, a.wait)


if __name__ == "__main__":
    sys.exit(main())
