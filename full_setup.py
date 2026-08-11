"""
full_setup.py
==============
One tenant, one call: project -> APIs -> service account -> key -> domain-wide
delegation -> verified. Optionally chained into seeding (source) or user
provisioning (target).

Why this exists on top of provision_gcp.py / dwd_helper.py / verify_scopes.py
-------------------------------------------------------------------------
Those three are correct and composable, and using them by hand is still five
separate steps with a client ID to copy from one into the next. This is the
glue: it calls provision_gcp.provision_side() for the Cloud side, feeds its
client ID straight into dwd_helper.run() for delegation, and calls
verify_scopes.verify() to confirm what actually landed -- the same
functions the CLI and the UI panels already call, not a reimplementation.
provision_side() rather than provision(): the latter always builds BOTH
tenants in one call, which is wrong for a function whose whole point is
"just this one side."

The one thing this cannot paper over
-------------------------------------
dwd_helper.run() drives a REAL browser through the sign-in flow. That needs
a display and, for anything beyond best-effort auto-fill, a human available
for 2FA/captcha. It cannot run on a headless VPS -- which is exactly why
provision_gcp.py already refuses cleanly there ("gcloud is not installed").
This script inherits that constraint rather than hiding it: call it from
wherever dwd_helper.py already works.

The admin password is used exactly once, to fill the sign-in form, and
never touches disk: it flows from function argument -> os.environ for the
dwd_helper subprocess-equivalent call -> browser keystrokes, and out of
scope the moment this function returns. Nothing here logs it, and the
underlying dwd_helper.log() calls never receive it either.

    python3 full_setup.py --side source --domain c.example.com \
        --admin admin@c.example.com --org-id 12345 \
        --seed --scale small --create-users

    python3 full_setup.py --side target --domain a.example.com \
        --admin admin@a.example.com --provision-users
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Module level, not inside run_full_setup(): a function-local `import X`
# binds a local name that tests cannot monkeypatch (`fs.provision_gcp` would
# not exist until the function actually ran). These three ARE the seams
# tests replace to exercise every branch without gcloud, a browser, or a
# live tenant.
import dwd_helper       # noqa: E402
import provision_gcp    # noqa: E402
import verify_scopes    # noqa: E402
from config import Settings  # noqa: E402


class Phase:
    def __init__(self, name: str):
        self.name = name
        self.status = "pending"     # pending | ok | failed | skipped
        self.detail = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "detail": self.detail}


def run_full_setup(
    side: str, domain: str, admin_email: str, admin_password: str,
    org_id: str = "", keys_dir: str = "keys", dry_run: bool = False,
    seed: bool = False, seed_scale: str = "small", create_users: bool = False,
    provision_users: bool = False, timeout: int = 900,
) -> dict:
    """side is 'source' or 'target'. Returns {phases: [...], ok: bool, ...}."""
    if side not in ("source", "target"):
        raise ValueError("side must be 'source' or 'target'")

    phases: list[Phase] = []

    # -- 1. Cloud project, APIs, service account, key ----------------------
    # provision_side(), not provision(): the latter always creates BOTH
    # source and target in one call, which is wrong here on two counts --
    # it does work for a side the caller did not ask for, and (worse) it
    # would have made this function's own "source" lookup wrong for a
    # target call, silently reading the other tenant's project and key.
    p = Phase(f"provision Cloud project ({side})")
    phases.append(p)
    ready, account_or_err = provision_gcp.gcloud_ready()
    if not ready:
        p.status, p.detail = "failed", account_or_err
        return {"side": side, "ok": False, "phases": [x.as_dict() for x in phases]}

    org = org_id or provision_gcp.detect_org()
    # "src"/"tgt", matching provision_gcp.provision()'s own naming --
    # side[:3] would give "sou"/"tar" instead, a second convention for the
    # same thing that makes project names harder to eyeball together in
    # the Cloud console.
    abbrev = "src" if side == "source" else "tgt"
    project = f"wsmig-{abbrev}-{random.randint(10000, 99999)}"
    key_path = os.path.join(keys_dir, f"{side}-sa.json")
    result = provision_gcp.provision_side(side, project, org, key_path,
                                          dry_run, force=False)
    if not result.get("ok"):
        p.status = "failed"
        p.detail = "; ".join(s["detail"] for s in result["steps"]
                             if s["status"] == "failed") or "see steps"
        return {"side": side, "ok": False, "phases": [x.as_dict() for x in phases],
                "gcpSteps": result["steps"]}
    p.status, p.detail = "ok", f"project {project}"
    client_id = provision_gcp.client_id_of(key_path)

    if dry_run:
        phases.append(Phase("domain-wide delegation (skipped: dry run)"))
        return {"side": side, "ok": True, "phases": [x.as_dict() for x in phases],
                "clientId": client_id}

    # -- 2. Domain-wide delegation, driven end to end -----------------------
    p = Phase(f"domain-wide delegation ({side})")
    phases.append(p)
    if not client_id:
        p.status, p.detail = "failed", "no client ID from step 1"
        return {"side": side, "ok": False, "phases": [x.as_dict() for x in phases]}

    os.environ[f"{side.upper()}_ADMIN"] = admin_email
    os.environ[f"{side.upper()}_DOMAIN"] = domain
    scopes = ",".join(verify_scopes.required_scopes(Settings(), side))

    prev_email = os.environ.get("DWD_EMAIL")
    prev_pw = os.environ.get("DWD_PASSWORD")
    os.environ["DWD_EMAIL"] = admin_email
    os.environ["DWD_PASSWORD"] = admin_password
    try:
        rc = dwd_helper.run(client_id, scopes, timeout, headful=True, tenant=side)
    finally:
        # Never leave the password sitting in this process's environment
        # longer than the one call that needs it.
        for var, val in (("DWD_EMAIL", prev_email), ("DWD_PASSWORD", prev_pw)):
            if val is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = val
    admin_password = None  # noqa: F841 - drop the only local reference

    if rc != 0:
        p.status = "failed"
        p.detail = ("dwd_helper exited nonzero -- likely needs a human for "
                    "2FA/captcha, or the sign-in form changed. Re-run "
                    f"dwd_helper.py --tenant {side} --client-id {client_id} "
                    "by hand to see the browser.")
        return {"side": side, "ok": False, "phases": [x.as_dict() for x in phases],
                "clientId": client_id}
    p.status = "ok"

    # -- 3. Verify, functionally -------------------------------------------
    p = Phase(f"verify delegation ({side})")
    phases.append(p)
    rows = verify_scopes.verify(Settings(), side, scopes.split(","))
    missing = [r["scope"] for r in rows if not r["ok"]]
    if missing:
        p.status, p.detail = "failed", f"{len(missing)} scope(s) still not live"
        return {"side": side, "ok": False, "phases": [x.as_dict() for x in phases],
                "clientId": client_id, "missingScopes": missing}
    p.status, p.detail = "ok", f"{len(rows)}/{len(rows)} scopes confirmed live"

    # -- 4. Optional: seed (source) or provision users (target) ------------
    if seed and side == "source":
        p = Phase("seed source tenant")
        phases.append(p)
        import subprocess

        argv = [sys.executable, "data-generator/seed_sandbox.py",
                "--confirm-domain", domain, "--scale", seed_scale, "--yes"]
        if create_users:
            argv.append("--create-users")
        env = dict(os.environ, SANDBOX_MODE="true")
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=7200,
                              env=env)
        if proc.returncode != 0:
            p.status, p.detail = "failed", (proc.stderr or proc.stdout)[-300:]
        else:
            p.status, p.detail = "ok", "seed complete -- see identities.csv"

    if provision_users and side == "target":
        p = Phase("provision target users")
        phases.append(p)
        import subprocess

        proc = subprocess.run(
            [sys.executable, "main.py", "provision-users", "--tenant", "target",
             "--yes"], capture_output=True, text=True, timeout=1800)
        if proc.returncode != 0:
            p.status, p.detail = "failed", (proc.stderr or proc.stdout)[-300:]
        else:
            p.status, p.detail = "ok", "accounts created"

    return {"side": side, "ok": all(x.status != "failed" for x in phases),
            "phases": [x.as_dict() for x in phases], "clientId": client_id}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[3])
    ap.add_argument("--side", choices=["source", "target"], required=True)
    ap.add_argument("--domain", required=True)
    ap.add_argument("--admin", required=True, help="super admin email")
    ap.add_argument("--org-id", default="")
    ap.add_argument("--keys-dir", default="keys")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--seed", action="store_true",
                    help="source only: seed the tenant after delegation")
    ap.add_argument("--scale", default="small",
                    choices=["tiny", "small", "medium", "large", "huge"])
    ap.add_argument("--create-users", action="store_true",
                    help="seed: create the test accounts first")
    ap.add_argument("--provision-users", action="store_true",
                    help="target only: create accounts from identity_map "
                         "after delegation")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    password = os.environ.get("DWD_PASSWORD") or getpass.getpass(
        f"Password for {args.admin} (never stored, never logged): ")

    result = run_full_setup(
        args.side, args.domain, args.admin, password, args.org_id,
        args.keys_dir, args.dry_run, args.seed, args.scale, args.create_users,
        args.provision_users)
    password = None  # noqa: F841

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for ph in result["phases"]:
            mark = {"ok": "ok  ", "failed": "FAIL", "skipped": "--  "}.get(
                ph["status"], "??  ")
            print(f"  {mark} {ph['name']}" + (f"  {ph['detail']}" if ph["detail"] else ""))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
