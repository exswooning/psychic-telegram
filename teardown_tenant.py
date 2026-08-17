"""
teardown_tenant.py
===================
The reverse of full_setup.py: delete a GCP project and revoke its
domain-wide delegation entry, one call. Built for cleaning up throwaway
tenants (sandbox testing, a cancelled trial) without an operator having to
SSH in and drive gcloud/the Admin Console by hand.

Both underlying operations are genuinely real deletions:

  * `gcloud projects delete` soft-deletes -- Google holds the project (and
    everything in it) for 30 days before actually purging it, recoverable
    via `gcloud projects undelete` in the meantime. See
    provision_gcp.delete_project()'s own docstring.
  * dwd_helper.revoke() removes the Admin Console delegation entry
    immediately and is NOT undoable -- confirmed live (see dwd_helper.py).

Neither step needs an ambient gcloud identity: like full_setup.py, this
drives a real browser through the tenant admin's own sign-in for the
gcloud side (gcloud_browser_auth.py), and the same admin session already
covers the Admin Console side (dwd_helper.py).

    python3 teardown_tenant.py --project wsmig-src-12345 \
        --client-id 107479933434636662752 --admin admin@tenant.example \
        --json
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dwd_helper           # noqa: E402
import gcloud_browser_auth  # noqa: E402
import provision_gcp        # noqa: E402


class Phase:
    def __init__(self, name: str):
        self.name = name
        self.status = "pending"     # pending | ok | failed | skipped
        self.detail = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "detail": self.detail}


def run_teardown(
    project: str, client_id: str, admin_email: str, admin_password: str,
    timeout: int = 300, progress_file: str | None = None,
) -> dict:
    """Delete `project` and revoke `client_id`'s DWD entry. Either can be
    omitted (empty string) to do only the other half -- a project without
    a live delegation entry, or vice versa, from a prior partial cleanup."""

    def _progress(pct: int, label: str) -> None:
        if not progress_file:
            return
        try:
            tmp = progress_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"pct": pct, "label": label}, fh)
            os.replace(tmp, progress_file)
        except OSError:
            pass

    phases: list[Phase] = []
    _progress(2, "starting")

    if client_id:
        p = Phase(f"revoke DWD delegation ({client_id})")
        phases.append(p)
        _progress(10, f"signing in to Admin Console as {admin_email}")
        os.environ["DWD_EMAIL"] = admin_email
        os.environ["DWD_PASSWORD"] = admin_password
        try:
            rc = dwd_helper.revoke(client_id, timeout, headful=True)
        except Exception as exc:      # noqa: BLE001 - same crash-recovery
            rc, p.detail = None, str(exc)[:200]
        if rc is None:
            p.status = "failed"
            p.detail = p.detail or "dwd_helper.revoke() crashed unexpectedly"
        elif rc == 0:
            p.status, p.detail = "ok", "revoked (or already gone)"
        else:
            p.status, p.detail = "failed", f"revoke exited {rc}"
        _progress(50, "delegation step done")

    if project:
        p = Phase(f"delete project ({project})")
        phases.append(p)
        _progress(60, f"signing in to gcloud as {admin_email}")
        ok, detail, cloudsdk_config = gcloud_browser_auth.login(
            admin_email, admin_password, timeout=timeout)
        admin_password = None  # noqa: F841
        if not ok:
            p.status, p.detail = "failed", f"gcloud sign-in failed: {detail}"
        else:
            _progress(85, f"deleting {project}")
            env = dict(os.environ, CLOUDSDK_CONFIG=cloudsdk_config)
            try:
                ok, detail = provision_gcp.delete_project(project, env=env)
                p.status, p.detail = ("ok" if ok else "failed"), detail
            finally:
                gcloud_browser_auth.cleanup(cloudsdk_config)

    _progress(100, "done")
    return {"ok": all(x.status == "ok" for x in phases) if phases else False,
            "phases": [x.as_dict() for x in phases]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[3])
    ap.add_argument("--project", default="", help="GCP project id to delete")
    ap.add_argument("--client-id", default="", help="service-account client ID to revoke")
    ap.add_argument("--admin", required=True, help="super admin email")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--progress-file", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if not args.project and not args.client_id:
        ap.error("need --project, --client-id, or both")

    password = os.environ.get("DWD_PASSWORD") or getpass.getpass(
        f"Password for {args.admin} (never stored, never logged): ")

    result = run_teardown(args.project, args.client_id, args.admin, password,
                          args.timeout, args.progress_file)
    password = None  # noqa: F841

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for ph in result["phases"]:
            mark = {"ok": "ok  ", "failed": "FAIL"}.get(ph["status"], "??  ")
            print(f"  {mark} {ph['name']}" + (f"  {ph['detail']}" if ph["detail"] else ""))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
