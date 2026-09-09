"""
repair_console_setup.py -- redo the console steps for a tenant already set up.

Two things a tenant needs that have no API and no gcloud command, so both
are browser automation against a Google console:

    delegation   the DWD grant, which has to be re-pasted whenever the
                 scope list grows -- an ungranted scope does not fail
                 loudly, it disables one feature quietly
    chat app     a Chat app configured in the Cloud project, without which
                 every chat call returns 404 "Google Chat app not found"
                 while the API itself reports ENABLED

full_setup does both, once, when a tenant is first configured. Neither was
reachable afterwards. So a tenant whose setup died before those phases
finished -- or one set up before a scope was added -- had no route back
except re-running the entire setup against a project that already exists.

Measured on the live tenant: 46 finished users, 46 chat 404s, no chat data
at all, and a setup result file still reading {"running": true}. Separately,
its delegation predates the purge scope, so its resets can only trash.

Runs on the box. dwd_helper._ensure_display() starts an Xvfb display, which
is what full_setup's own Chat phase already relies on -- the "this server is
headless, here is a command to run elsewhere" note on /api/dwd/automate was
written before that existed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable


def _run(argv: list[str], env: dict) -> tuple[bool, str]:
    """Stream a child through, keeping its last line as the detail.

    Streamed rather than captured for the reason remove_tenant_setup
    documents: a captured child says nothing until it exits, and these take
    minutes at a browser's pace.
    """
    tail: list[str] = []
    try:
        proc = subprocess.Popen(argv, cwd=HERE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                bufsize=1, env=env)
    except Exception as exc:      # noqa: BLE001
        return False, str(exc)[:200]
    for line in proc.stdout:      # type: ignore[union-attr]
        line = line.rstrip("\n")
        print(f"    {line}", flush=True)
        if line.strip():
            tail.append(line.strip())
            del tail[:-20]
    return proc.wait() == 0, (tail[-1][:200] if tail else "")


def repair(side: str, do_grant: bool, do_chat: bool,
           account_id: int | None = None) -> dict:
    from config import Settings
    import dwd_helper
    import verify_scopes

    st = Settings(account_id=account_id) if account_id else Settings()
    key = st.source_sa_key if side == "source" else st.target_sa_key
    admin = st.source_admin if side == "source" else st.target_admin
    phases: list[dict] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        phases.append({"name": name, "status": "ok" if ok else "failed",
                       "detail": detail})
        print(f"  {'ok  ' if ok else 'FAIL'} {name}"
              + (f"  {detail}" if detail else ""), flush=True)

    env = dict(os.environ)
    if not env.get("DWD_PASSWORD"):
        return {"ok": False, "phases": [],
                "error": "DWD_PASSWORD is not set -- both steps sign in to a "
                         "Google console and neither can prompt"}

    try:
        with open(key, encoding="utf-8") as fh:
            keydata = json.load(fh)
    except Exception as exc:      # noqa: BLE001
        return {"ok": False, "phases": [],
                "error": f"could not read {key}: {str(exc)[:120]}"}
    client_id = keydata.get("client_id", "")
    project = keydata.get("project_id", "")

    if do_grant:
        # grant_scopes, not _load_payload. Its own docstring says to use it
        # "wherever a grant is being *written*" -- it unions OPTIONAL_SCOPES
        # and every scope a feature toggle could need, where _load_payload
        # returned a narrower line: 18 against 25 on the live tenant.
        #
        # dwd_helper MERGES into the existing entry (that is what --no-merge
        # opts out of), so a narrow line does not drop what is already
        # granted -- confirmed on the tenant, where all seven scopes missing
        # from the narrow line still issued afterwards. It simply never adds
        # them, which on a tenant that lacked one is a feature silently
        # staying off.
        from config import Settings as _S

        scopes = ",".join(verify_scopes.grant_scopes(
            _S(account_id=account_id) if account_id else _S(), side))
        n = len([x for x in scopes.split(",") if x])
        if not client_id:
            add(f"re-grant delegation ({side})", False,
                "no client_id in the service-account key")
        else:
            # dwd_helper takes the sign-in identity from DWD_EMAIL, not
            # from a flag -- it has no --admin. Passing one would have been
            # an argparse error, which is exit 2 before the browser opens.
            ok, detail = _run([PY, "dwd_helper.py", "--client-id", client_id,
                               "--scopes", scopes],
                              dict(env, DWD_EMAIL=admin or
                                   env.get("DWD_EMAIL", "")))
            add(f"re-grant delegation ({side}, {n} scopes)", ok, detail)

    if do_chat:
        if not project or not admin:
            add(f"configure Chat app ({side})", False,
                "no project or admin on file for this tenant")
        else:
            # Before the console step, not after: being a Workspace super
            # admin confers nothing on a Cloud project, and full_setup
            # grants roles/editor for exactly this reason while repair did
            # not. A tenant whose provision skipped that grant therefore
            # failed here every single time, and the failure arrived as
            # "could not find the app name field -- console may have
            # changed" -- a selector report for a page the account was
            # never allowed to see. Confirmed on wsmig-src-96030:
            #   You need additional access to the project: wsmig-src-96030
            #   resourcemanager.projects.get (Missing)
            # Non-fatal, exactly as in provision: an already-granted
            # project reports ok, and an identity that cannot grant reports
            # why, which is strictly more than the console page said.
            import provision_gcp

            steps: list = []
            provision_gcp.grant_admin_console_access(
                project, admin, steps, dry_run=False, env=env)
            for st_ in steps:
                add(f"project access for {admin} ({side})",
                    st_.status == "ok", st_.detail)
            ok, detail = _run([PY, "gcloud_browser_auth.py",
                               "--configure-chat", "--project", project,
                               "--admin", admin], env)
            add(f"configure Chat app ({side})", ok, detail)

    return {"ok": all(p["status"] == "ok" for p in phases), "phases": phases}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--side", choices=("source", "target"), default="source")
    ap.add_argument("--account-id", type=int)
    ap.add_argument("--skip-grant", action="store_true")
    ap.add_argument("--skip-chat", action="store_true")
    a = ap.parse_args(argv)

    res = repair(a.side, not a.skip_grant, not a.skip_chat, a.account_id)
    if res.get("error"):
        print(res["error"])
        return 2
    print()
    print("repaired" if res["ok"] else "finished with failures above")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
