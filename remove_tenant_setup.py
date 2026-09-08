"""
remove_tenant_setup.py
======================
Wipe a configured tenant's seeded data and remove the setup itself.

The most destructive thing in this product, and deliberately the one with
the most explicit gate: it undoes a whole tenant setup in a single run.

What it does, in order
----------------------
  1. wipe the tenant's data       reset_target.py (target) or
                                  seed_sandbox.py --reset (source)
  2. remove the Cloud setup       teardown_tenant.run_teardown -- revoke the
                                  DWD grant, delete the GCP project
  3. forget the configuration     tenant_configs blanked, key file removed

Order matters and is not adjustable. The data wipe needs the credential
that step 2 destroys, so doing them the other way round leaves a tenant full
of data and no way left to reach it.

Why the order also means partial failure is survivable
------------------------------------------------------
Each step is independently re-runnable and reports its own outcome. A run
that wipes the data and fails to delete the project leaves a tenant that is
empty and still reachable -- annoying, and fixable by re-running. The
reverse would not be.

The ledger is NOT touched here. It records what was migrated, which is
evidence about a migration that happened, and survives the tenant it
happened to -- see reset_drive_ledger.py for clearing it deliberately.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

import accounts_auth
import teardown_tenant
from config import Settings

HERE = os.path.dirname(os.path.abspath(__file__))
PY_EXE = sys.executable


def _run(argv: list[str], env: dict | None = None) -> tuple[bool, str]:
    try:
        p = subprocess.run(argv, cwd=HERE, capture_output=True, text=True,
                           env=env or os.environ)
    except Exception as exc:                    # noqa: BLE001
        return False, str(exc)[:200]
    tail = (p.stdout or p.stderr or "").strip().splitlines()
    return p.returncode == 0, (tail[-1][:200] if tail else "")


def remove(side: str, domain: str, admin_email: str, admin_password: str,
           account_id: int | None, project: str = "", client_id: str = "",
           wipe_data: bool = True, delete_setup: bool = True) -> dict:
    phases: list[dict] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        phases.append({"name": name, "status": "ok" if ok else "failed",
                       "detail": detail})

    st = Settings(account_id=account_id) if account_id else Settings()
    env = dict(os.environ, SANDBOX_MODE="true")
    if account_id:
        env["MIGRATION_DB"] = st.db_path

    if wipe_data:
        if side == "target":
            ok, detail = _run([PY_EXE, "reset_target.py",
                               "--confirm-domain", domain, "--yes"], env)
        else:
            ok, detail = _run([PY_EXE, "data-generator/seed_sandbox.py",
                               "--confirm-domain", domain, "--reset", "--yes"], env)
        add(f"wipe {side} data ({domain})", ok, detail)
        if not ok:
            # Stop. Removing the credential now would leave data behind with
            # nothing left that can reach it.
            add("remove Cloud setup", False,
                "skipped -- the data wipe failed, and removing the "
                "credential now would strand whatever is left")
            return {"ok": False, "phases": phases}

    if delete_setup and (project or client_id):
        res = teardown_tenant.run_teardown(project, client_id, admin_email,
                                           admin_password)
        for ph in res.get("phases", []):
            phases.append(ph)
    elif delete_setup:
        add("remove Cloud setup", True,
            "nothing recorded to remove -- no project or client id on file")

    if delete_setup and account_id:
        try:
            cfg = accounts_auth.get_tenant_config(account_id, side) or {}
            key = cfg.get("sa_key_path") or ""
            accounts_auth.forget_tenant_config(account_id, side)
            removed = ""
            if key:
                path = key if os.path.isabs(key) else os.path.join(HERE, key)
                try:
                    os.remove(path)
                    removed = f", key file deleted"
                except OSError:
                    removed = f", key file left at {key}"
            add(f"forget {side} configuration", True,
                f"tenant_configs blanked{removed}")
        except Exception as exc:                # noqa: BLE001
            add(f"forget {side} configuration", False, str(exc)[:200])

    return {"ok": all(p["status"] == "ok" for p in phases), "phases": phases}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[3])
    ap.add_argument("--side", choices=["source", "target"], required=True)
    ap.add_argument("--confirm-domain", required=True,
                    help="type the domain being removed; it must match the "
                         "configured one exactly")
    ap.add_argument("--admin-email", default="")
    ap.add_argument("--project", default="")
    ap.add_argument("--client-id", default="")
    ap.add_argument("--account-id", type=int)
    ap.add_argument("--keep-data", action="store_true",
                    help="remove the setup but leave the tenant's data")
    ap.add_argument("--keep-setup", action="store_true",
                    help="wipe the data but leave the project and grant")
    args = ap.parse_args(argv)

    st = Settings(account_id=args.account_id) if args.account_id else Settings()
    configured = st.source_domain if args.side == "source" else st.target_domain
    if args.confirm_domain.strip().lower() != (configured or "").lower():
        sys.exit(f"REFUSING: {args.confirm_domain!r} is not the configured "
                 f"{args.side} domain ({configured!r})")

    password = os.environ.get("DWD_PASSWORD", "")
    res = remove(args.side, configured, args.admin_email or "", password,
                 args.account_id, args.project, args.client_id,
                 wipe_data=not args.keep_data,
                 delete_setup=not args.keep_setup)
    for ph in res["phases"]:
        mark = {"ok": "ok  ", "failed": "FAIL"}.get(ph["status"], "??  ")
        print(f"  {mark} {ph['name']}" + (f"  {ph['detail']}" if ph["detail"] else ""))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
