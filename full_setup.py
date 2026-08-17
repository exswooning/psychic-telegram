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
dwd_helper.run() (and, when no Cloud project exists yet, gcloud_browser_auth
too) drives a REAL browser through a sign-in flow. That needs a display
and, for anything beyond best-effort auto-fill, a human available for
2FA/captcha -- this runs on the Xvfb+Chrome virtual display already set up
for exactly this on the VPS (see connect_vps.sh to watch/finish it by hand
if it stalls).

Cloud project creation needs an identity with org-level project-creation
rights. Phase 1 below prefers whatever ambient gcloud identity this
process already has (the legacy/local-gcloud caller, running on an
operator's own already-authenticated machine); failing that, it drives
gcloud's own OAuth consent through gcloud_browser_auth using the SAME
admin_email/admin_password as delegation below, so the project ends up
owned by the tenant's OWN identity -- never a shared operator one -- and
the credential is revoked and discarded the moment provisioning for this
tenant finishes.

The admin password is used exactly once per phase it is needed in, and
never touches disk: for delegation it flows from function argument ->
os.environ for the dwd_helper subprocess-equivalent call -> browser
keystrokes, and out of scope the moment this function returns; for Cloud
provisioning it flows the same way into gcloud_browser_auth.login(), whose
own OAuth token lives only in a throwaway CLOUDSDK_CONFIG directory that
cleanup() deletes (after revoking it on Google's side) once this call is
done. Nothing here logs it, and the underlying dwd_helper.log() /
gcloud_browser_auth.log() calls never receive it either.

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
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Module level, not inside run_full_setup(): a function-local `import X`
# binds a local name that tests cannot monkeypatch (`fs.provision_gcp` would
# not exist until the function actually ran). These three ARE the seams
# tests replace to exercise every branch without gcloud, a browser, or a
# live tenant.
import accounts_auth        # noqa: E402
import dwd_helper           # noqa: E402
import gcloud_browser_auth  # noqa: E402
import provision_gcp        # noqa: E402
import verify_scopes        # noqa: E402
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
    account_id: int | None = None, progress_file: str | None = None,
) -> dict:
    """side is 'source' or 'target'. Returns {phases: [...], ok: bool, ...}.

    account_id is None for the legacy/single-tenant caller (writes land in
    env.sh, exactly as before this parameter existed) and set for a SaaS
    account's own Quick Setup run (writes land in that account's
    tenant_configs row instead -- see phase 3b). It plays no part in the
    provisioning or DWD-verification phases above: those already get the
    right domain/admin via the transient os.environ override a few lines
    down, which works identically regardless of which account is calling.

    progress_file, when given, gets a live "how far along is this" signal
    the caller couldn't otherwise have: the `phases` list above only ever
    exists in full once this function returns, which for a real (non-dry)
    run can be several minutes away -- long enough that a UI polling for
    it sees nothing but "still running" the entire time. Best-effort by
    design (see _progress()) -- a failed write must never take down the
    run it's reporting on.
    """
    if side not in ("source", "target"):
        raise ValueError("side must be 'source' or 'target'")

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

    # -- 1. Cloud project, APIs, service account, key ----------------------
    # Skipped entirely when this account already has a real key uploaded
    # for this side (see api_server.py's POST /api/v2/setup/credentials).
    # Cloud project creation needs an identity with org-level
    # project-creation rights. Preferred order: (a) an ambient identity
    # this process already has (the legacy/local-gcloud caller, running on
    # an operator's own already-authenticated machine); (b) failing that,
    # gcloud_browser_auth drives a real browser through gcloud's own OAuth
    # consent using the SAME admin_email/admin_password already being used
    # for delegation below -- so a fresh tenant with no key on file yet
    # still needs nothing from the admin beyond the one sign-in, and the
    # project ends up owned by THEIR identity, not a shared operator one.
    # account_id is None (the legacy/local-gcloud caller) skips none of
    # this -- provision_side() is already idempotent and safe to re-run,
    # exactly as it always was before this branch existed.
    existing = (accounts_auth.get_tenant_config(account_id, side)
                if account_id is not None else None)
    uploaded_key = existing["sa_key_path"] if existing else None
    key_path = uploaded_key or os.path.join(keys_dir, f"{side}-sa.json")

    p = Phase(f"provision Cloud project ({side})")
    phases.append(p)
    if uploaded_key and os.path.isfile(uploaded_key):
        client_id = provision_gcp.client_id_of(uploaded_key)
        if not client_id:
            p.status, p.detail = "failed", (
                f"{uploaded_key} exists but has no client_id in it -- "
                "re-upload the key")
            return {"side": side, "ok": False, "phases": [x.as_dict() for x in phases]}
        p.status, p.detail = "skipped", "using an uploaded service-account key"
    elif dry_run:
        # A dry run touches no real gcloud state at all -- every
        # provision_gcp step already no-ops under dry_run and reports
        # nothing beyond "ok"/"project X" either way, so driving a real
        # browser sign-in (or even requiring an ambient gcloud identity)
        # just to immediately skip every step it would unlock is pure
        # waste. Preview needs no live credential at all. If a key from an
        # earlier real run already happens to sit at this exact path (the
        # legacy/local-gcloud caller's fixed keys/{side}-sa.json), surface
        # its client ID same as before -- otherwise there is none yet.
        p.status, p.detail = "skipped", "dry run"
        client_id = (provision_gcp.client_id_of(key_path)
                    if os.path.isfile(key_path) else "")
    else:
        # provision_side(), not provision(): the latter always creates BOTH
        # source and target in one call, which is wrong here on two counts --
        # it does work for a side the caller did not ask for, and (worse) it
        # would have made this function's own "source" lookup wrong for a
        # target call, silently reading the other tenant's project and key.
        env = None
        cloudsdk_config = ""
        _progress(3, "checking for an existing gcloud sign-in")
        ready, account_or_err = provision_gcp.gcloud_ready()
        if not ready:
            auth_phase = Phase(f"authenticate gcloud as {admin_email}")
            phases.append(auth_phase)
            _progress(5, f"signing in to Google Cloud as {admin_email}")
            ok, detail, cloudsdk_config = gcloud_browser_auth.login(
                admin_email, admin_password, timeout=timeout)
            if not ok:
                auth_phase.status, auth_phase.detail = "failed", detail
                p.status, p.detail = "failed", "gcloud sign-in did not complete"
                return {"side": side, "ok": False, "phases": [x.as_dict() for x in phases]}
            auth_phase.status, auth_phase.detail = "ok", detail
            env = dict(os.environ, CLOUDSDK_CONFIG=cloudsdk_config)
            _progress(18, "signed in -- creating the Cloud project")

        try:
            org = org_id or provision_gcp.detect_org(env=env)
            # "src"/"tgt", matching provision_gcp.provision()'s own naming --
            # side[:3] would give "sou"/"tar" instead, a second convention for
            # the same thing that makes project names harder to eyeball
            # together in the Cloud console.
            abbrev = "src" if side == "source" else "tgt"
            project = f"wsmig-{abbrev}-{random.randint(10000, 99999)}"
            # 18-75%: the slowest stretch, a project create plus a dozen
            # sequential API-enable calls -- on_step fires after each one,
            # so the bar actually moves through it instead of sitting still.
            result = provision_gcp.provision_side(
                side, project, org, key_path, dry_run, force=False, env=env,
                on_step=lambda done, total, name: _progress(
                    18 + int(57 * done / total), name))
        finally:
            if cloudsdk_config:
                gcloud_browser_auth.cleanup(cloudsdk_config)

        if not result.get("ok"):
            p.status = "failed"
            p.detail = "; ".join(s["detail"] for s in result["steps"]
                                 if s["status"] == "failed") or "see steps"
            return {"side": side, "ok": False, "phases": [x.as_dict() for x in phases],
                    "gcpSteps": result["steps"]}
        p.status, p.detail = "ok", f"project {project}"
        _progress(78, "Cloud project ready")
        client_id = provision_gcp.client_id_of(key_path)

        # Confirmed live: DWD scope propagation can take much longer than
        # phase 3's own retry budget below (one real grant took ~23
        # minutes; the budget waits ~15). Saving the key path here, the
        # moment it exists, rather than only after phase 3 succeeds (the
        # only place this used to happen -- see phase 3b) is what makes a
        # retry after that kind of "still propagating" failure cheap: the
        # project/service-account/client-id created above are already
        # real and already the target of whatever was just granted, so
        # the NEXT run's own uploaded_key check above finds this and
        # skips straight past this multi-minute branch entirely instead
        # of creating yet another throwaway project and restarting
        # propagation from zero on a brand new client ID. Best-effort:
        # a failed save here must not fail a provisioning run that
        # otherwise succeeded.
        if account_id is not None:
            try:
                accounts_auth.update_tenant_config(
                    account_id, side, domain=domain, admin_email=admin_email,
                    sa_key_path=key_path)
            except Exception:      # noqa: BLE001 - advisory only
                pass

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
    _progress(80, f"granting domain-wide delegation as {admin_email}")
    # Confirmed live: an uncaught Playwright TimeoutError deep inside
    # dwd_helper.run() (a re-click that raced a dialog closing under it)
    # propagated all the way out here and crashed the whole subprocess --
    # the caller's own status file was left reading {"running": true}
    # forever, since the process that would have written "failed" was
    # already dead. dwd_helper.py's own selectors are already
    # best-effort against a console that "changes without notice" (see
    # its module docstring); this makes THIS call site match that same
    # assumption instead of trusting every one of ~450 lines of browser
    # choreography to never raise.
    # tenant=side deliberately NOT passed: dwd_helper.run() would then do
    # its OWN functional verification internally, with no retry, and
    # return a non-zero rc on ANY scope not yet live -- confirmed live,
    # this short-circuited before phase 3 below (the same check, but
    # retried) ever got to run, defeating that retry entirely. Phase 3 is
    # the one and only verification gate now; this call only ever needs
    # to know whether Authorize was accepted.
    crash_detail = None
    try:
        rc = dwd_helper.run(client_id, scopes, timeout, headful=True)
    except Exception as exc:      # noqa: BLE001
        rc, crash_detail = None, str(exc)[:200]
    finally:
        # Never leave the password sitting in this process's environment
        # longer than the one call that needs it.
        for var, val in (("DWD_EMAIL", prev_email), ("DWD_PASSWORD", prev_pw)):
            if val is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = val
    admin_password = None  # noqa: F841 - drop the only local reference

    if crash_detail is not None:
        p.status, p.detail = "failed", (
            f"dwd_helper crashed unexpectedly ({crash_detail}) -- likely "
            "the Admin Console DOM shifted underneath it mid-click. Re-run "
            f"dwd_helper.py --tenant {side} --client-id {client_id} by hand "
            "to see the browser and finish it, or fix the selector it "
            "choked on.")
        return {"side": side, "ok": False, "phases": [x.as_dict() for x in phases],
                "clientId": client_id}

    if rc != 0:
        p.status = "failed"
        p.detail = ("dwd_helper exited nonzero -- likely needs a human for "
                    "2FA/captcha, or the sign-in form changed. Re-run "
                    f"dwd_helper.py --tenant {side} --client-id {client_id} "
                    "by hand to see the browser.")
        return {"side": side, "ok": False, "phases": [x.as_dict() for x in phases],
                "clientId": client_id}
    p.status = "ok"
    _progress(90, "delegation granted -- verifying")

    # -- 3. Verify, functionally -------------------------------------------
    p = Phase(f"verify delegation ({side})")
    phases.append(p)
    # Confirmed live: dwd_helper.run() itself already knows a freshly
    # accepted grant checks as 0/N scopes live for "a minute or two" --
    # its own log line says so ("re-run verify_scopes.py ... before
    # assuming it failed") -- but nothing here ever acted on that advice,
    # so a real run finishing at exactly the wrong moment reported a
    # confirmed failure on a grant that was, in fact, still just
    # propagating. Retrying is that same advice, automated.
    #
    # ~2.9 minutes (the original budget here) was NOT enough: confirmed
    # live on the trial tenant, a grant that Admin Console accepted at
    # 14:46:33 still showed 0/14 scopes live when the old budget gave up
    # at ~190s in, and was only confirmed fully live (14/14) at 14:59 --
    # comfortably over 3 minutes, nowhere near the old ceiling. This is a
    # real, wider propagation window than the org-policy/key-creation
    # delays elsewhere in this codebase, not the same delay reused --
    # those cleared in well under a minute. Sized with margin past the
    # slowest confirmed-live case rather than matching it exactly.
    backoffs = (15, 20, 30, 45, 60, 90, 120, 150, 180, 210)  # ~15.3 min total
    rows = missing = []
    for attempt in range(len(backoffs) + 1):
        rows = verify_scopes.verify(Settings(), side, scopes.split(","))
        missing = [r["scope"] for r in rows if not r["ok"]]
        if not missing or attempt == len(backoffs):
            break
        _progress(90, f"delegation granted -- waiting for it to propagate "
                      f"({len(rows) - len(missing)}/{len(rows)} scopes live so far)")
        time.sleep(backoffs[attempt])
    if missing:
        # By this point the grant has had ~15 minutes to propagate and
        # still has not -- rare (Google's own docs say this step can
        # occasionally take longer still), but the Admin Console entry
        # itself was already confirmed accepted back in phase 2, so this
        # is "still settling," not "never happened." Re-verifying costs
        # nothing (no browser, just a token probe per scope) -- point at
        # that instead of implying the whole setup needs to be redone.
        p.status, p.detail = "failed", (
            f"{len(missing)} scope(s) still not live after ~15 minutes of "
            "waiting -- the delegation itself was accepted by Admin Console "
            "(see the phase above); this is Google still propagating it. "
            f"Wait a few more minutes and re-run `python3 verify_scopes.py "
            f"--tenant {side}` -- no need to redo delegation itself.")
        return {"side": side, "ok": False, "phases": [x.as_dict() for x in phases],
                "clientId": client_id, "missingScopes": missing}
    p.status, p.detail = "ok", f"{len(rows)}/{len(rows)} scopes confirmed live"
    _progress(97, "saving tenant configuration")

    # -- 3b. Point the REST of the tool at what was just built ---------------
    # Without this, everything downstream -- seeding, migrate, the Setup
    # Wizard's own status page -- keeps reading whatever tenant env.sh
    # already pointed at, because nothing else here writes it. That is
    # a real gap this had at first: Quick Setup finished green and the
    # UI still had no way to seed the tenant it had just built, because
    # env.sh (which webui.py's /api/seed reads SOURCE_DOMAIN/SOURCE_ADMIN
    # from) had not moved. Reuses webui.write_config_raw rather than a
    # second env.sh writer, so this and the Setup Wizard's own save button
    # can never disagree about the file format.
    p = Phase(f"point env.sh at the {side} tenant" if account_id is None
              else f"save {side} tenant config for account {account_id}")
    phases.append(p)
    try:
        if account_id is not None:
            accounts_auth.update_tenant_config(
                account_id, side, domain=domain, admin_email=admin_email,
                sa_key_path=key_path)
            p.status, p.detail = "ok", "tenant_configs row updated"
        else:
            from webui import write_config_raw

            write_config_raw({
                f"{side.upper()}_DOMAIN": domain,
                f"{side.upper()}_ADMIN": admin_email,
                f"{side.upper()}_SA_KEY": key_path,
            })
            p.status, p.detail = "ok", f"{side.upper()}_DOMAIN/_ADMIN/_SA_KEY written"
    except Exception as exc:      # noqa: BLE001 - advisory: setup itself
        # already succeeded, this just means one more manual step
        p.status, p.detail = "failed", (
            f"{exc} -- set {side.upper()}_DOMAIN={domain}, "
            f"{side.upper()}_ADMIN={admin_email}, "
            f"{side.upper()}_SA_KEY={key_path} "
            + ("in env.sh by hand" if account_id is None
               else f"in tenant_configs for account {account_id} by hand"))

    # -- 4. Optional: seed (source) or provision users (target) ------------
    if seed and side == "source":
        _progress(99, "seeding test data")
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
        _progress(99, "provisioning target accounts")
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

    _progress(100, "done")
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
    # Present in argv (not just passed as a plain kwarg) on purpose: the
    # control plane's *_status polling identifies a running process by
    # grepping `ps -eo args=` for its own launch command, and two different
    # accounts both provisioning "source" at once need that grep to tell
    # them apart -- a bare `--side source` matches both.
    ap.add_argument("--account-id", type=int, default=None,
                    help="SaaS account this run belongs to, if any")
    ap.add_argument("--progress-file", default=None,
                    help="live {pct, label} JSON written as this run "
                         "progresses, for a caller polling from outside")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    password = os.environ.get("DWD_PASSWORD") or getpass.getpass(
        f"Password for {args.admin} (never stored, never logged): ")

    result = run_full_setup(
        args.side, args.domain, args.admin, password, args.org_id,
        args.keys_dir, args.dry_run, args.seed, args.scale, args.create_users,
        args.provision_users, account_id=args.account_id,
        progress_file=args.progress_file)
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
