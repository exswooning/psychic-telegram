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



# The missing-browser message, as a function rather than a literal buried in
# a branch -- its exact wording is the whole point, so it needs to be
# assertable without driving a full setup run.
_NO_BROWSER_MARKER = "no browser available"


def log(msg: str) -> None:
    """Diagnostic line, on stderr.

    stdout carries the --json result and nothing else, so anything written
    for a human goes here -- where the api_server launch path already
    redirects it to full-setup-<side>.err, and where a CLI run shows it
    inline.

    This exists because four call sites were already calling log() and
    nothing defined it. Three were new; the fourth had been sitting in the
    pre-check's `except` branch since yesterday, which is why it never
    fired -- a NameError waiting for the one path that would have most
    needed to explain itself.
    """
    print(msg, file=sys.stderr, flush=True)


def optional_missing_detail(scopes: list[str]) -> str:
    """What to append when an optional scope did not land.

    Named, not silent: this is the message that sends someone to the right
    console entry instead of wondering why a panel is empty. A function
    rather than a literal buried in a branch because its exact wording is
    the point, and that has to be assertable without driving a setup run.
    """
    short = ", ".join(sc.rsplit("/", 1)[-1] for sc in scopes)
    return (f" (optional scopes not granted: {short} — re-paste the scope line "
            f"in the delegation panel to enable the features that use them; "
            f"nothing else is affected)")


def _vs_for(account_id: int | None):
    """Settings for this account, falling back to the legacy tenant."""
    try:
        return Settings(account_id=account_id)
    except Exception:      # noqa: BLE001
        return Settings()


def _admin_can_reach_project(project: str, admin_email: str,
                             admin_password: str) -> bool:
    """Can this admin open the project in the Cloud console?

    Asked with gcloud as the admin, because that is the identity every
    console-driven step actually runs as. Anything else would be testing a
    different question than the one that matters.

    False on any doubt: an unreachable project and an unanswerable check
    lead to the same place, and guessing "probably fine" is how the
    unusable-key case went unnoticed in the first place.
    """
    cfg = ""
    try:
        ok, _detail, cfg = gcloud_browser_auth.login(
            admin_email, admin_password, timeout=240)
        if not ok:
            return False
        env = dict(os.environ, CLOUDSDK_CONFIG=cfg)
        proc = subprocess.run(
            ["gcloud", "projects", "describe", project, "--format=value(projectId)"],
            capture_output=True, text=True, timeout=180, env=env)
        return proc.returncode == 0
    except Exception:      # noqa: BLE001 - never block setup on the probe
        return False
    finally:
        if cfg:
            try:
                gcloud_browser_auth.cleanup(cfg)
            except Exception:      # noqa: BLE001
                pass


def _delegation_already_live(settings, side: str, client_id: str) -> bool:
    """Is this key already carrying a working delegation?

    The difference between "repair it" and "leave it alone". A key whose
    scopes mint today is migrating someone's tenant; replacing it to unlock
    one service would trade a working migration for a feature.
    """
    if not client_id:
        return False
    try:
        import scope_guard
        return scope_guard.is_complete(
            settings, side, verify_scopes.required_scopes(settings, side))
    except Exception:      # noqa: BLE001
        return False


def _chat_access_hint(project: str, admin_email: str, detail: str) -> str:
    """Why the Chat form did not render, when the likely reason is access.

    Setup provisions projects AND accepts uploaded keys. A key uploaded by
    hand can point at a project the Workspace admin has no IAM role on --
    Workspace admin and GCP IAM are separate systems, and an upload carries
    no relationship to who owns the project behind it. Every console-driven
    step then runs as an account that cannot open the page.

    Confirmed live: an uploaded key for wsmig-src-96030, whose admin owns a
    different project entirely, reported "could not find the app name field
    -- console may have changed". Naming the likelier cause costs one line
    and saves an afternoon spent editing selectors that were fine.
    """
    return (f"{detail} — this usually means {admin_email or 'the admin'} has "
            f"no IAM role on {project}, so the page never rendered. Common "
            f"with an UPLOADED key: the project behind it was created by a "
            f"different account. Check with `gcloud projects describe "
            f"{project}`, then grant access with `gcloud projects "
            f"add-iam-policy-binding {project} --member=user:{admin_email} "
            f"--role=roles/editor` from an account that owns it. Projects "
            f"this tool provisions itself get that grant automatically.")


def is_no_browser(detail: str) -> bool:
    """Did this failure come from the host having no browser at all?"""
    return _NO_BROWSER_MARKER in (detail or "")


def no_browser_detail(crash_detail: str) -> str:
    """What to tell an operator whose host has no browser installed.

    Deliberately mentions neither 2FA nor a captcha. Both were in the
    message this replaces, and both are wrong here: the sign-in never
    happened, because nothing was ever launched to sign in with. Observed
    live -- a wizard run reported "likely needs a human for 2FA/captcha"
    for two tenants whose delegation was, at that moment, complete and
    working.
    """
    return (f"{crash_detail} Delegation was NOT changed. This is a host "
            "setup problem, not a sign-in problem: no browser could be "
            "started at all. Use the Manual tab to paste the scope line "
            "yourself, or install a browser on the host and retry.")


def run_full_setup(
    side: str, domain: str, admin_email: str, admin_password: str,
    org_id: str = "", keys_dir: str = "keys", dry_run: bool = False,
    seed: bool = False, seed_scale: str = "small", create_users: bool = False,
    provision_users: bool = False, timeout: int = 900,
    account_id: int | None = None, progress_file: str | None = None,
    reprovision: bool = False, scopes_override: list[str] | None = None,
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

    # Re-provision: build a brand-new project even though a key is on file.
    #
    # The reason this exists is specific, not general "force" convenience. An
    # UPLOADED key points at a project the Workspace admin may hold no IAM
    # role on -- an upload carries no relationship to who owns the project
    # behind it. Confirmed live: a key for wsmig-src-96030 whose admin was
    # owner of a different project entirely, which left every console-driven
    # step (Chat app configuration) unable to load its own page. There is no
    # way to grant access to that project from here; the only route the
    # admin controls is a project it creates itself.
    #
    # Destructive on purpose and gated at the API: it mints a new project,
    # service account and client ID, so the delegation granted against the
    # OLD client ID stops applying and has to be re-granted (which this run
    # then does). A tenant mid-migration should not be re-provisioned.
    if reprovision and uploaded_key:
        log(f"  re-provisioning {side}: ignoring the uploaded key at "
            f"{uploaded_key} and creating a new project")
        uploaded_key = None
        key_path = os.path.join(keys_dir, f"{side}-sa.json")

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

        # Can this admin actually administer the project behind that key?
        #
        # An upload carries no relationship to who owns the project it came
        # from. Confirmed live: a key for wsmig-src-96030 whose admin holds
        # no IAM role on it at all -- so every console-driven step ran as an
        # account that could not open the page, and Chat could never be
        # configured. It surfaced three layers up as "could not find the app
        # name field", a selector complaint about a page that never rendered.
        #
        # Detected here rather than discovered there, and repaired the only
        # way that works: provisioning a project this admin owns. That is
        # done ONLY when the key is not already carrying a working
        # delegation -- replacing credentials that are migrating a live
        # tenant, to unlock one service, is not a trade this should make on
        # anyone's behalf.
        project_of_key = provision_gcp.project_of(uploaded_key)
        if project_of_key and not dry_run:
            access = Phase(f"admin access to {project_of_key}")
            phases.append(access)
            reachable = _admin_can_reach_project(
                project_of_key, admin_email, admin_password)
            if reachable:
                access.status, access.detail = "ok", "the admin can administer it"
            else:
                working = _delegation_already_live(_vs_for(account_id), side,
                                                   client_id)
                if working:
                    access.status = "skipped"
                    access.detail = (
                        f"{admin_email} has no IAM role on {project_of_key}, so "
                        f"console steps (Chat app configuration) cannot run. "
                        f"Delegation IS live, so nothing was changed -- "
                        f"migration works. To unlock Chat, either grant this "
                        f"admin access to {project_of_key} from an account "
                        f"that owns it, or re-provision onto a project it "
                        f"owns (that mints a new client ID and re-grants).")
                else:
                    access.status = "ok"
                    access.detail = (
                        f"{admin_email} cannot administer {project_of_key} and "
                        f"no delegation is live on it -- provisioning a "
                        f"project this admin owns instead")
                    log(f"  {access.detail}")
                    uploaded_key = None
                    key_path = os.path.join(keys_dir, f"{side}-sa.json")
                    client_id = ""
                    p.status, p.detail = "skipped", (
                        "the uploaded key's project is not administrable by "
                        "this admin -- provisioning a fresh one")
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
                    18 + int(57 * done / total), name),
                # The console steps that follow (Chat app configuration) run
                # as this admin, and a Workspace super admin holds nothing on
                # a Cloud project by default -- see
                # provision_gcp.grant_admin_console_access.
                admin_email=admin_email)
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

        # Chat needs an app configured (name + status) before a single
        # chat.spaces() call stops 404ing -- see configure_chat_app()'s own
        # docstring. Not a one-time gate like the Cloud Console ToS above:
        # every tenant setup mints a brand-new project, so without this,
        # Chat would 404 on every new tenant forever. "skipped" rather than
        # "failed" on a miss -- run_full_setup()'s own ok flag is
        # all(status != "failed"), and Chat is one of several services this
        # setup enables, not a reason to report the whole run as broken.
        chat_phase = Phase(f"configure Chat app ({side})")
        phases.append(chat_phase)
        _progress(79, "configuring the Chat app")
        try:
            chat_ok, chat_detail = gcloud_browser_auth.configure_chat_app(
                admin_email, admin_password, project, timeout=90)
            chat_phase.status = "ok" if chat_ok else "skipped"
            chat_phase.detail = chat_detail
            # A console step that fails because the account cannot SEE the
            # project must say so. Confirmed live: an uploaded key pointed at
            # wsmig-src-96030, a project this admin has no role on (it owns a
            # different one it created itself), and the failure surfaced as
            # "could not find the app name field -- console may have changed"
            # -- a selector report for a page that never rendered.
            if not chat_ok and "name field" in chat_detail:
                chat_phase.detail = _chat_access_hint(project, admin_email,
                                                      chat_detail)
        except Exception as exc:      # noqa: BLE001 - never fail setup over this
            chat_phase.status, chat_phase.detail = "skipped", str(exc)[:150]

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
    # Settings(account_id=...), not bare Settings(). A SaaS account's key
    # lives at keys/<id>/<side>-sa.json; bare Settings() resolves the
    # LEGACY env.sh key instead, whose client ID was never granted on this
    # tenant -- so verification below could not pass no matter how long it
    # waited. Confirmed live: this loop sat at "0/11 scopes live" for its
    # whole retry budget while an account-scoped check of the very same
    # tenant returned 11/11. The account's tenant_configs row is written in
    # phase 1 above, before this runs, so it is available here.
    try:
        _vs = Settings(account_id=account_id)
    except Exception:      # noqa: BLE001
        # A tenant_configs row that cannot be read must not abort a setup
        # that has already provisioned a project and granted delegation --
        # fall back to the legacy resolution and let verification report
        # whatever it finds, rather than crashing at the last phase.
        _vs = Settings()
    # grant_scopes(), not required_scopes(): the console line carries the
    # optional extras too (apps.licensing, for per-account plans), so a
    # tenant set up by this runner has them from the start instead of
    # needing a second hand-pasted grant later. They are graded leniently
    # below -- optional means optional, including when the grant fails.
    required = set(verify_scopes.required_scopes(_vs, side))
    if scopes_override:
        # An operator-chosen list, unioned with what the code will actually
        # request. Union rather than replace, deliberately: a token request
        # fails WHOLE if any requested scope is ungranted, so a chooser that
        # let someone deselect a required scope would not produce a
        # narrower migration -- it would produce a tenant that cannot
        # migrate at all, diagnosed later by scope_guard as a delegation
        # gap. What the chooser genuinely controls is the OPTIONAL extras
        # and anything beyond them.
        chosen = {s.strip() for s in scopes_override if s.strip()}
        granted = sorted(chosen | required)
        dropped = sorted(required - chosen)
        if dropped:
            log(f"  scope selection omitted {len(dropped)} required "
                f"scope(s); granting them anyway -- a token request fails "
                f"whole without them")
    else:
        granted = verify_scopes.grant_scopes(_vs, side)
    scopes = ",".join(granted)
    optional = set(granted) - required

    # Is this already done?
    #
    # Driving the Admin Console is the slowest, most fragile thing this tool
    # does -- a real browser, a real sign-in, and selectors against a console
    # that changes without notice. Doing it to re-grant scopes that are
    # already live is pure risk for no gain, and it is the *common* case:
    # re-running setup on a working tenant, or setting up a tenant that was
    # seeded earlier.
    #
    # Confirmed live: the wizard reported "FAIL domain-wide delegation" for
    # BOTH tenants of a pair that, in the same minute, minted every one of
    # their required scopes in a single token request (15/15 and 11/11) and
    # completed a 1:1 Gmail migration through them. Nothing was wrong with
    # the delegation; the browser simply could not start. One token mint
    # answers that before any of the fragile machinery runs.
    already_granted = False
    try:
        import scope_guard
        if scope_guard.is_complete(_vs, side, scopes.split(",")):
            already_granted = True
            p.status = "ok"
            p.detail = ("already granted -- every required scope minted in "
                        "one token request, so the console was not touched")
            _progress(90, "delegation already complete -- verifying")
    except Exception as exc:      # noqa: BLE001 - advisory only
        # A failed pre-check must never be the reason setup does not run;
        # fall through and do it the long way.
        log(f"  pre-check could not run ({str(exc)[:90]}); granting anyway")

    if not already_granted:
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

        # A failed grant attempt is not automatically a failed setup.
        #
        # The console line now carries optional scopes too, so a tenant whose
        # required delegation is already complete still comes through here --
        # it is being asked to add apps.licensing, nothing more. If the
        # browser cannot run (no display, 2FA, a captcha), failing the whole
        # setup would turn a previously-green tenant red over a panel
        # feature. Check what is actually live before deciding.
        grant_failed = (crash_detail is not None) or (rc != 0)
        if grant_failed:
            # Say what this decided and why. A silent `except: pass` here
            # turns a transient probe failure into a failed setup that
            # blames the browser, which is exactly the kind of misdirection
            # this whole area has already cost a day to.
            try:
                import scope_guard
                complete = scope_guard.is_complete(_vs, side, sorted(required))
                log(f"  grant attempt failed; required scopes complete="
                    f"{complete} ({len(required)} checked)")
            except Exception as exc:      # noqa: BLE001
                complete = False
                log(f"  grant attempt failed and the required-scope check "
                    f"could not run ({type(exc).__name__}: {str(exc)[:90]})")
            if complete:
                log("  every REQUIRED scope is already live -- continuing; "
                    "only the optional extras were missed")
                p.status = "ok"
                p.detail = ("required delegation already complete; the "
                            "optional extras could not be added this run")
                already_granted = True
                crash_detail, rc = None, 0

        if crash_detail is not None and is_no_browser(crash_detail):
            p.status, p.detail = "failed", no_browser_detail(crash_detail)
            return {"side": side, "ok": False,
                    "phases": [x.as_dict() for x in phases],
                    "clientId": client_id}

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
    missing_optional: list[str] = []
    for attempt in range(len(backoffs) + 1):
        rows = verify_scopes.verify(_vs, side, scopes.split(","))
        not_live = [r["scope"] for r in rows if not r["ok"]]
        # Only the required set decides whether to keep waiting, and whether
        # this phase passes. An optional scope that never lands costs one
        # panel feature; failing setup over it would cost the whole tenant.
        missing = [sc for sc in not_live if sc in required]
        missing_optional = [sc for sc in not_live if sc in optional]
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
    live = len(rows) - len(missing_optional)
    p.status = "ok"
    p.detail = f"{live}/{len(rows)} scopes confirmed live"
    if missing_optional:
        p.detail += optional_missing_detail(missing_optional)
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
    ap.add_argument("--reprovision", action="store_true",
                    help="create a NEW Cloud project even if a key is already "
                         "on file. Mints a new service account and client ID, "
                         "so the existing delegation stops applying and is "
                         "re-granted by this run. Use when the uploaded key's "
                         "project is one this admin cannot administer.")
    ap.add_argument("--scopes", default="",
                    help="comma-separated scope line to grant instead of the "
                         "default. Required scopes are added back regardless: "
                         "a token request fails whole if any requested scope "
                         "is ungranted, so omitting one does not narrow the "
                         "migration, it breaks it.")
    args = ap.parse_args(argv)

    password = os.environ.get("DWD_PASSWORD") or getpass.getpass(
        f"Password for {args.admin} (never stored, never logged): ")

    result = run_full_setup(
        args.side, args.domain, args.admin, password, args.org_id,
        args.keys_dir, args.dry_run, args.seed, args.scale, args.create_users,
        args.provision_users, account_id=args.account_id,
        progress_file=args.progress_file, reprovision=args.reprovision,
        scopes_override=[s for s in args.scopes.split(",") if s.strip()]
        or None)
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
