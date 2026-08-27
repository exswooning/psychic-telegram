"""
provision_gcp.py
================
Create the Cloud side of a migration from nothing: projects, APIs, service
accounts, keys. Idempotent, scriptable, and with no step that needs a human
to know which console to open.

Why this exists as well as setup.sh
-----------------------------------
setup.sh does roughly this and predates it. It also, on a real fresh org:

  * uses `declare -A`, which is bash 4 -- macOS ships bash 3.2, so it does
    not run on the machine most operators are sitting at;
  * enables all nine APIs in ONE `gcloud services enable` call, which fails
    with SERVICE_CONFIG_NOT_FOUND_OR_PERMISSION_DENIED on a project created
    seconds earlier. Enabling them one at a time works every time;
  * has no answer for `iam.managed.disableServiceAccountKeyCreation`, a
    custom org policy that is ON by default in newer Workspace orgs. Key
    creation fails with CUSTOM_ORG_POLICY_VIOLATION, writes a zero-byte
    file, and the run continues as though it had a key;
  * never grants the service account any Cloud IAM, so the tool can never
    afterwards check or repair its own API enablement.

Each of those cost a debugging cycle on this project. They are handled here
rather than documented as gotchas.

What it cannot do
-----------------
Domain-wide delegation. Google publishes no API for it -- a super admin
must click it through, which is what dwd_helper.py drives. This script
finishes by printing the exact client IDs and the command that grants them,
so the handoff is one copy-paste rather than a hunt through two consoles.

    python3 provision_gcp.py --source-domain c.example.com \
                             --target-domain a.example.com --dry-run
    python3 provision_gcp.py --source-domain c.example.com \
                             --target-domain a.example.com --json
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
from typing import Callable
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Every API any engine or the seeder can reach for. Kept in one place with
# ensure_apis so a service switched on later cannot rediscover the
# SERVICE_DISABLED problem this project already paid for once.
try:
    from ensure_apis import REQUIRED_APIS
    APIS = list(REQUIRED_APIS)
except Exception:      # noqa: BLE001 - keep provisioning usable standalone
    APIS = [
        "drive.googleapis.com", "gmail.googleapis.com",
        "calendar-json.googleapis.com", "admin.googleapis.com",
        "iamcredentials.googleapis.com", "people.googleapis.com",
        "tasks.googleapis.com", "chat.googleapis.com",
        "cloudidentity.googleapis.com",
    ]

# Needed to create the keys themselves, to read/repair enablement later, and
# to look up projects and the org at all. cloudresourcemanager is easy to
# forget because gcloud only needs it on whichever project it happens to be
# billing quota to -- which is fine until that project is one this script
# just created, and then `gcloud organizations list` starts failing with
# SERVICE_DISABLED against a project the caller never chose.
SUPPORT_APIS = ["iam.googleapis.com", "serviceusage.googleapis.com",
                "cloudresourcemanager.googleapis.com"]

# The org policy that silently blocks key creation on newer orgs.
KEY_POLICY = "iam.managed.disableServiceAccountKeyCreation"


class Step:
    """One provisioning action and how it went, so a UI can render progress
    without parsing gcloud's prose."""

    def __init__(self, name: str):
        self.name = name
        self.status = "pending"     # pending | ok | skipped | failed
        self.detail = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "detail": self.detail}


def run(argv: list[str], timeout: int = 300, env: dict | None = None
        ) -> tuple[int, str]:
    """gcloud, captured and non-interactive. Returns (rc, combined output).

    `--quiet` on every call is not cosmetic. gcloud prompts on several of
    these paths -- "API [x] not enabled ... Would you like to enable and
    retry (y/N)?" is the common one -- and a prompt with no tty attached is
    a job that hangs until its timeout with no output explaining why. This
    script is meant to be launched from a UI button, where that failure mode
    looks exactly like a crash and is far harder to diagnose than a clean
    non-zero exit.

    stdin is closed for the same reason: belt and braces against any prompt
    that ignores --quiet.
    """
    if argv and argv[0] == "gcloud" and "--quiet" not in argv:
        argv = [argv[0], "--quiet", *argv[1:]]
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL, env=env)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, "gcloud not found on PATH"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"


def gcloud_ready(env: dict | None = None) -> tuple[bool, str]:
    if not shutil.which("gcloud"):
        return False, ("gcloud is not installed. https://cloud.google.com/sdk "
                       "-- or run this whole thing from Cloud Shell, which "
                       "already has it and is already authenticated.")
    rc, out = run(["gcloud", "auth", "list", "--filter=status:ACTIVE",
                   "--format=value(account)"], env=env)
    # run() returns COMBINED stdout+stderr, and an empty auth list makes
    # this exact gcloud version print a diagnostic line ("WARNING: The
    # following filter keys were not present in any resource : status")
    # instead of just empty output. Without this filter that warning was
    # read as the account name -- gcloud_ready() reported True with no
    # real authenticated account at all, a false positive that would only
    # surface as a much more confusing failure the first time something
    # actually tried to use "account" WARNING:....
    lines = [ln.strip() for ln in out.strip().splitlines()
             if ln.strip() and not ln.strip().upper().startswith(("WARNING", "ERROR"))]
    account = lines[0] if lines else ""
    if rc != 0 or not account:
        return False, "no active gcloud account -- run: gcloud auth login"
    return True, account


def can_create_projects(account: str) -> bool:
    """Whether that gcloud identity is allowed to create a project at all.

    Google refuses this for service accounts by design:

        ERROR: (gcloud.projects.create) PERMISSION_DENIED:
        Service accounts cannot create projects

    which matters because gcloud_ready() answers "is anybody signed in",
    and on a box that has already run a migration the answer is usually a
    service account left active by the previous tenant's setup. Live, that
    made Quick Setup skip its own browser sign-in, adopt
    source-sa@wsmig-src-96030.iam.gserviceaccount.com, and fail on the
    first real call -- reported to the operator as a permission problem
    with their admin account, which it was not.
    """
    account = (account or "").strip().lower()
    return bool(account) and not account.endswith(".iam.gserviceaccount.com")


def detect_org(env: dict | None = None) -> str:
    """The org id, if this account can see exactly one.

    Worth automating: a project created outside an org cannot inherit the
    org's policies, and finding the id by hand means knowing that
    `gcloud organizations list` is where it lives. Ambiguity is returned as
    empty rather than guessed -- picking the wrong org silently is worse
    than asking.
    """
    rc, out = run(["gcloud", "organizations", "list", "--format=value(ID)"], env=env)
    if rc != 0:
        return ""
    ids = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return ids[0] if len(ids) == 1 else ""


def project_exists(project: str, env: dict | None = None) -> bool:
    rc, _ = run(["gcloud", "projects", "describe", project], env=env)
    return rc == 0


def delete_project(project: str, env: dict | None = None) -> tuple[bool, str]:
    """Tear down a project this tool created.

    `gcloud projects delete` only ever soft-deletes -- Google holds the
    project (and everything in it: the service account, its key material,
    billing history) for 30 days before actually purging it, recoverable
    with `gcloud projects undelete` in the meantime. That is Google's
    behaviour, not a guarantee made here; nothing in this codebase depends
    on the 30-day window.

    Deliberately does NOT check project_exists() first: a project that is
    already gone (or already soft-deleted) makes `describe` fail exactly
    the same way `delete` would have, so calling delete directly and
    reading its own error is one fewer round trip and one fewer place for
    the two checks to disagree about a project's state.
    """
    rc, out = run(["gcloud", "projects", "delete", project], timeout=120, env=env)
    if rc == 0:
        return True, f"{project} deleted (recoverable for 30 days via undelete)"
    if "not found" in out.lower() or "does not exist" in out.lower():
        return True, f"{project} already gone"
    return False, out.strip()[-300:]


# Known, real gates a brand-new Google account can hit here, none of
# which look anything like "fix your gcloud command" -- matched by a
# substring of gcloud's own error text so whoever reads the Result box
# knows immediately whether this is something the automation should have
# already cleared (ToS), something only the account owner can clear
# (billing), or something that just needs time (quota).
_KNOWN_PROJECT_CREATE_ERRORS = (
    ("Callers must accept Terms of Service",
     "This Google account has never used Google Cloud before, and Google "
     "gates project creation on a one-time Terms of Service acceptance "
     "that gcloud_browser_auth.py's sign-in step should already have "
     "cleared automatically. Seeing this anyway means that step didn't "
     "land -- visit https://console.cloud.google.com once as this "
     "account, accept the terms shown there, then try again."),
    ("billing account",
     "This project needs a billing account, which is not something this "
     "tool can create on your behalf (it needs real payment details). "
     "Link one at https://console.cloud.google.com/billing for this "
     "account, then try again."),
    ("quota",
     "This Google account has hit a project-creation quota -- common and "
     "usually temporary for a brand-new account. Wait a while and try "
     "again, or request an increase at "
     "https://console.cloud.google.com/iam-admin/quotas."),
)


def _explain_project_create_failure(raw: str) -> str:
    for needle, explanation in _KNOWN_PROJECT_CREATE_ERRORS:
        if needle.lower() in raw.lower():
            return f"{explanation} (raw: {raw[-200:]})"
    return raw


def ensure_project(project: str, org: str, steps: list[Step],
                   dry_run: bool, env: dict | None = None) -> bool:
    s = Step(f"project {project}")
    steps.append(s)
    if project_exists(project, env=env):
        s.status, s.detail = "skipped", "already exists"
        return True
    if dry_run:
        s.status, s.detail = "skipped", "dry run"
        return True
    argv = ["gcloud", "projects", "create", project, f"--name={project}"]
    if org:
        argv.append(f"--organization={org}")
    rc, out = run(argv, timeout=600, env=env)
    if rc != 0:
        s.status, s.detail = "failed", _explain_project_create_failure(out.strip()[-300:])
        return False
    s.status, s.detail = "ok", f"created{' in org ' + org if org else ''}"
    return True


def enable_apis(project: str, apis: list[str], steps: list[Step],
                dry_run: bool, env: dict | None = None,
                on_step: Callable[[Step], None] | None = None) -> bool:
    """One API per call, deliberately.

    `gcloud services enable a b c ...` fails on a freshly created project
    with SERVICE_CONFIG_NOT_FOUND_OR_PERMISSION_DENIED naming none of them,
    while the identical list enabled individually succeeds every time. It is
    slower and it works, which is the correct trade for a step that runs
    once per tenant.

    on_step, called after each API (not each list), is what gives a caller
    real per-API progress through the slowest part of provisioning instead
    of one silent 20-second gap between "project created" and "APIs done".
    """
    ok = True
    for api in apis:
        s = Step(f"enable {api} on {project}")
        steps.append(s)
        if dry_run:
            s.status, s.detail = "skipped", "dry run"
        else:
            rc, out = run(["gcloud", "services", "enable", api,
                           f"--project={project}"], timeout=300, env=env)
            if rc == 0:
                s.status = "ok"
            else:
                s.status, s.detail = "failed", out.strip()[-200:]
                ok = False
        if on_step:
            on_step(s)
    return ok


def _self_grant_orgpolicy_admin(org: str, steps: list[Step], dry_run: bool,
                                env: dict | None = None) -> None:
    """Close the gap this project hit live, automatically: a Workspace
    super admin does NOT automatically hold roles/orgpolicy.policyAdmin --
    it is a separate Cloud IAM role Google does not bundle with Workspace
    admin rights, confirmed the hard way when a real provisioning run
    retried key creation 7 times over ~180s and still failed with
    CUSTOM_ORG_POLICY_VIOLATION, because the account relaxing the policy
    was never actually allowed to.

    What that same live investigation also confirmed: an account that is
    the org's actual owner already holds
    roles/resourcemanager.organizationAdmin (granted automatically), and
    THAT role can self-grant orgpolicy.policyAdmin in one IAM call -- no
    human, no second admin, no support ticket. This does that call
    automatically, every run, before it would otherwise be needed:

    * If the account already holds orgpolicy.policyAdmin, granting it
      again is a no-op -- gcloud returns success immediately.
    * If the account lacks organizationAdmin too, the grant fails with a
      permission error, caught here and reported as "skipped" -- the
      run falls through to the existing manual-intervention message
      exactly as before this function existed. Never fatal on its own.
    """
    s = Step(f"self-grant orgpolicy.policyAdmin on org {org}" if org
            else "self-grant orgpolicy.policyAdmin")
    steps.append(s)
    if dry_run:
        s.status, s.detail = "skipped", "dry run"
        return
    if not org:
        s.status, s.detail = "skipped", "no organization id known"
        return
    rc, out = run(["gcloud", "config", "get-value", "account"], env=env)
    account = next((ln.strip() for ln in out.splitlines() if "@" in ln), "")
    if rc != 0 or not account:
        s.status, s.detail = "skipped", "could not determine the calling account"
        return
    rc, out = run(
        ["gcloud", "organizations", "add-iam-policy-binding", org,
         f"--member=user:{account}", "--role=roles/orgpolicy.policyAdmin"],
        timeout=120, env=env)
    if rc == 0:
        s.status, s.detail = "ok", f"{account} now holds roles/orgpolicy.policyAdmin"
    else:
        s.status, s.detail = "skipped", (
            f"{account} cannot grant IAM roles on this org -- "
            f"{out.strip()[-150:]}")


def relax_key_policy(project: str, steps: list[Step], dry_run: bool,
                     org: str = "", env: dict | None = None) -> None:
    """Allow SA key creation on THIS project only.

    Newer Workspace orgs enforce `iam.managed.disableServiceAccountKeyCreation`
    by default. gcloud then fails key creation with
    CUSTOM_ORG_POLICY_VIOLATION and -- worse -- still creates a zero-byte
    file where the key should be, so a caller that only checks "does the
    file exist" carries on with an empty credential.

    Scoped to the project rather than lifting it org-wide: the default is a
    good one, and a migration needs the exception in exactly two places.
    """
    # Before even trying to relax the constraint: give the calling account
    # every chance to already be allowed to. See
    # _self_grant_orgpolicy_admin's own docstring for why this is both
    # necessary (Workspace admin != orgpolicy.policyAdmin) and safe to
    # attempt unconditionally on every run. Runs first (and is itself
    # dry-run-aware) so the step count -- and so provision_side()'s own
    # `total` estimate -- stays right in both modes, not just the real one.
    _self_grant_orgpolicy_admin(org, steps, dry_run, env=env)

    s = Step(f"allow SA keys on {project}")
    steps.append(s)
    if dry_run:
        s.status, s.detail = "skipped", "dry run"
        return
    policy = (f"name: projects/{project}/policies/{KEY_POLICY}\n"
              f"spec:\n  rules:\n  - enforce: false\n")
    path = os.path.join("/tmp", f"orgpolicy-{project}-{int(time.time())}.yaml")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(policy)
        # orgpolicy has to be enabled *somewhere* for this call to work, and
        # gcloud bills it to whatever quota project is configured -- which on
        # a fresh install is a project the caller may not own. Point it at
        # the project being provisioned, which we know exists and is ours.
        # Based on the CALLER's env (not a bare os.environ), so a per-tenant
        # CLOUDSDK_CONFIG override from an ephemeral browser-auth login
        # survives into this call instead of silently falling back to
        # whatever config directory this process would otherwise default to.
        policy_env = dict(env or os.environ, CLOUDSDK_CORE_PROJECT=project)
        enable_rc, enable_out = run(
            ["gcloud", "services", "enable", "orgpolicy.googleapis.com",
             f"--project={project}"], timeout=300, env=policy_env)
        # Confirmed live: enabling an API on a project created moments
        # earlier in this SAME run can succeed and still not be usable
        # yet -- the very next call fails SERVICE_DISABLED against the
        # API this just turned on. Retrying set-policy specifically on
        # that error (not e.g. a real syntax problem with the policy
        # file) is what actually gets past a propagation gap instead of
        # giving up one call too early.
        rc, out = 1, ""
        for attempt in range(4):
            rc, out = run(["gcloud", "org-policies", "set-policy", path],
                          timeout=300, env=policy_env)
            if rc == 0 or "SERVICE_DISABLED" not in out or attempt == 3:
                break
            time.sleep(5 * (attempt + 1))
        if rc == 0:
            s.status, s.detail = "ok", "key creation permitted on this project"
        elif not enable_rc == 0 and "SERVICE_DISABLED" in out:
            # The enable call itself never succeeded (rare -- e.g. the API
            # is org-blocked outright) -- say so plainly rather than the
            # more general "not fatal" framing below, which assumes
            # enablement worked and only enforcement is what's stale.
            s.status, s.detail = "skipped", (
                f"could not enable orgpolicy.googleapis.com: {enable_out.strip()[-150:]}")
        else:
            # Not fatal: the org may not enforce the constraint at all, in
            # which case nothing needed doing and key creation will simply
            # work -- create_key()'s own retry covers the remaining case
            # where THIS succeeded but enforcement is still catching up.
            s.status, s.detail = "skipped", out.strip()[-200:]
    except Exception as exc:      # noqa: BLE001
        s.status, s.detail = "skipped", str(exc)[:200]
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def ensure_service_account(project: str, sa: str, steps: list[Step],
                           dry_run: bool, env: dict | None = None) -> str:
    email = f"{sa}@{project}.iam.gserviceaccount.com"
    s = Step(f"service account {sa}")
    steps.append(s)
    if dry_run:
        s.status, s.detail = "skipped", "dry run"
        return email
    rc, _ = run(["gcloud", "iam", "service-accounts", "describe", email,
                 f"--project={project}"], env=env)
    if rc == 0:
        s.status, s.detail = "skipped", "already exists"
        return email
    rc, out = run(["gcloud", "iam", "service-accounts", "create", sa,
                   f"--project={project}",
                   "--display-name=workspace migration"], timeout=300, env=env)
    s.status, s.detail = ("ok", email) if rc == 0 else ("failed", out.strip()[-200:])
    return email


def grant_service_usage(project: str, sa_email: str, steps: list[Step],
                        dry_run: bool, env: dict | None = None) -> None:
    """Let the service account read and repair its own API enablement.

    Without this, ensure_apis.py can only ever report UNKNOWN -- it cannot
    even ask whether an API is on, let alone switch one on. Granting it here
    is what turns "tell a human to go to the console" into a button.

    Service account creation is eventually consistent: binding a policy to
    an identity Google has not finished propagating fails, so this retries.
    """
    s = Step(f"grant serviceUsageAdmin to {sa_email}")
    steps.append(s)
    if dry_run:
        s.status, s.detail = "skipped", "dry run"
        return
    for attempt in range(4):
        rc, out = run(["gcloud", "projects", "add-iam-policy-binding", project,
                       f"--member=serviceAccount:{sa_email}",
                       "--role=roles/serviceusage.serviceUsageAdmin",
                       "--condition=None"], timeout=300, env=env)
        if rc == 0:
            s.status, s.detail = "ok", "ensure_apis can now self-heal"
            return
        time.sleep(5 * (attempt + 1))
    s.status, s.detail = "failed", out.strip()[-200:]


def grant_admin_console_access(project: str, admin_email: str,
                               steps: list, dry_run: bool,
                               env: dict | None = None) -> None:
    """Give the Workspace admin a role on the project it is expected to
    administer.

    Workspace admin and GCP IAM are separate permission systems -- being a
    super admin of the domain confers nothing on a Cloud project, even one
    created moments ago on that domain's behalf. Every browser-driven
    console step afterwards is performed AS that admin, so without this they
    open the console and get:

        You need additional access to the project: <project>
        resourcemanager.projects.get (Missing)

    Confirmed live on wsmig-src-96030: the Chat app configuration page never
    rendered its form, and that surfaced three layers up as "could not find
    the app name field -- console may have changed". A selector report for a
    page the account was never allowed to see.

    roles/editor rather than roles/owner: enough to configure the Chat app
    and read the project, not enough to hand out further IAM or delete the
    project. These are per-tenant throwaway projects belonging to the
    operator, so this is not privilege escalation -- it gives the project's
    actual owner the access the rest of the tool already assumes they have.

    Non-fatal, like the other grants here: a project provisioned without it
    still migrates fine; only the console-driven extras (Chat) need it.
    """
    s = Step(f"grant project access to {admin_email or 'the admin'}")
    steps.append(s)
    if dry_run:
        s.status, s.detail = "skipped", "dry run"
        return
    if not admin_email:
        s.status, s.detail = "skipped", "no admin email known at this point"
        return
    out = ""
    for attempt in range(3):
        rc, out = run(["gcloud", "projects", "add-iam-policy-binding", project,
                       f"--member=user:{admin_email}",
                       "--role=roles/editor",
                       "--condition=None"], timeout=300, env=env)
        if rc == 0:
            s.status = "ok"
            s.detail = "the admin can now open this project in the console"
            return
        time.sleep(5 * (attempt + 1))
    # Skipped, not failed: everything except the console-driven Chat step
    # works without it, and failing the whole provision here would cost more
    # than the gap it leaves.
    s.status, s.detail = "skipped", out.strip()[-180:]


# Confirmed live: a brand-new org's Workspace super admin does not
# automatically hold the GCP-side "Organization Policy Administrator"
# role needed to relax iam.managed.disableServiceAccountKeyCreation --
# Workspace admin and GCP IAM are separate permission systems, and being
# the former never implies the latter. relax_key_policy() already treats
# its own failure to set the policy as non-fatal (the org may simply not
# enforce it, the common case) -- when it DOES enforce it and the relax
# attempt was denied rather than merely absent, key creation fails here
# with this exact shape, and no retry or different URL fixes it: it is a
# real permissions gap, not a missing click.
_KNOWN_KEY_CREATE_ERRORS = (
    ("disableServiceAccountKeyCreation",
     "This organization enforces a policy blocking service-account key "
     "creation, and this account doesn't hold the Organization Policy "
     "Administrator role needed to relax it for this one project (being "
     "a Workspace super admin doesn't automatically grant that -- it's a "
     "separate Google Cloud IAM role). Either have an Organization "
     "Administrator grant this account roles/orgpolicy.policyAdmin (or "
     "relax the constraint for this project directly) and try again, or "
     "use the Manual tab instead: run provision_gcp.py on a machine "
     "signed in as someone who already holds that role."),
    ("IAM_PERMISSION_DENIED",
     "This account doesn't have the IAM permissions this step needs. "
     "Ask an Organization Administrator to grant the missing role, or "
     "use the Manual tab to provision from a machine signed in as "
     "someone who already holds it."),
)


def _explain_key_create_failure(raw: str) -> str:
    for needle, explanation in _KNOWN_KEY_CREATE_ERRORS:
        if needle.lower() in raw.lower():
            return f"{explanation} (raw: {raw[-200:]})"
    return raw


def create_key(project: str, sa_email: str, dest: str, steps: list[Step],
               dry_run: bool, force: bool, env: dict | None = None) -> bool:
    s = Step(f"key -> {dest}")
    steps.append(s)
    if dry_run:
        s.status, s.detail = "skipped", "dry run"
        return True
    # A non-empty existing key is a working credential; replacing it would
    # invalidate nothing on Google's side but would strand whatever is
    # already deployed with the old file.
    if os.path.isfile(dest) and os.path.getsize(dest) > 0 and not force:
        s.status, s.detail = "skipped", "key already present (use --force to replace)"
        return True
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)

    # relax_key_policy() runs immediately before this and can genuinely
    # succeed -- confirmed live, the hard way: `gcloud org-policies
    # describe --effective` read back enforce: false right after the
    # override was applied, and the very next `keys create` call STILL
    # hit CUSTOM_ORG_POLICY_VIOLATION. Google's org-policy ENFORCEMENT
    # path lags behind its own READ path; a 30-second retry budget (four
    # attempts, 5-20s apart) was not enough to see it clear even once in
    # three separate live runs against a real org. This budget is
    # generous on purpose -- Google's own guidance on org-policy
    # propagation cites delays up to several minutes -- and, same as
    # grant_service_usage()'s IAM-binding retry above, only fires for
    # THIS specific error; a wrong project or missing service account
    # fails on the first attempt exactly like it always did.
    backoffs = (10, 15, 20, 30, 45, 60)  # ~3 minutes total if every attempt is denied
    out = ""
    retried = False
    for attempt in range(len(backoffs) + 1):
        rc, out = run(["gcloud", "iam", "service-accounts", "keys", "create", dest,
                       f"--iam-account={sa_email}", f"--project={project}"],
                      timeout=300, env=env)
        # gcloud leaves a zero-byte file behind when the org policy blocks
        # this, so existence is not success -- check the content is real JSON.
        if rc == 0 and os.path.isfile(dest) and os.path.getsize(dest) > 0:
            try:
                with open(dest, encoding="utf-8") as fh:
                    json.load(fh)
                os.chmod(dest, 0o600)
                s.status, s.detail = ("ok", "created") if not retried else (
                    "ok", f"created (needed {attempt + 1} attempts -- org-policy "
                          "enforcement took a moment to catch up with the override)")
                return True
            except (OSError, ValueError) as exc:
                s.status, s.detail = "failed", f"key file is not valid JSON: {exc}"
                return False
        if os.path.isfile(dest) and os.path.getsize(dest) == 0:
            os.unlink(dest)     # do not leave an empty file that looks like a key
        if "disableServiceAccountKeyCreation" not in out or attempt == len(backoffs):
            break
        retried = True
        time.sleep(backoffs[attempt])

    explained = _explain_key_create_failure(out.strip()[-250:])
    if retried:
        explained = (
            f"Retried {len(backoffs) + 1} times over ~{sum(backoffs)}s after the org-policy "
            f"override was applied -- Google's enforcement still hadn't caught up. "
            f"{explained} It may just need more time than this run waited; "
            "re-running often succeeds on its own once propagation finishes.")
    s.status, s.detail = "failed", explained
    return False


def client_id_of(key_path: str) -> str:
    try:
        with open(key_path, encoding="utf-8") as fh:
            return json.load(fh).get("client_id", "")
    except (OSError, ValueError):
        return ""


def project_of(key_path: str) -> str:
    """Which GCP project minted this service-account key.

    The companion to client_id_of(). An uploaded key says which project it
    came from but nothing about whether the admin using it can administer
    that project -- and console-driven setup steps run as the admin, not as
    the key. full_setup checks the two against each other.
    """
    try:
        with open(key_path, encoding="utf-8") as fh:
            return json.load(fh).get("project_id", "")
    except (OSError, ValueError):
        return ""


def provision_side(side: str, project: str, org: str, key_dest: str,
                   dry_run: bool, force: bool, env: dict | None = None,
                   on_step: Callable[[int, int, str], None] | None = None,
                   admin_email: str = "") -> dict:
    """on_step(done, total, step_name), called after every single step --
    not just once per function -- is what lets a caller show real,
    smoothly-advancing progress through the slowest part of setup (a
    dozen sequential API-enable calls) instead of one long silent gap
    between "project created" and "APIs done"."""
    steps: list[Step] = []
    sa = f"{side}-sa"
    # project, N APIs, SA, grant, self-grant-orgpolicy, relax, key
    total = 7 + len(SUPPORT_APIS + APIS)

    def _tick() -> None:
        if on_step and steps:
            on_step(len(steps), total, steps[-1].name)

    if not ensure_project(project, org, steps, dry_run, env=env):
        _tick()
        return {"side": side, "project": project, "ok": False,
                "steps": [s.as_dict() for s in steps], "clientId": ""}
    _tick()

    enable_apis(project, SUPPORT_APIS + APIS, steps, dry_run, env=env,
               on_step=lambda s: on_step and on_step(len(steps), total, s.name))
    sa_email = ensure_service_account(project, sa, steps, dry_run, env=env)
    _tick()
    grant_service_usage(project, sa_email, steps, dry_run, env=env)
    _tick()
    grant_admin_console_access(project, admin_email, steps, dry_run, env=env)
    _tick()
    relax_key_policy(project, steps, dry_run, org=org, env=env)
    _tick()
    created = create_key(project, sa_email, key_dest, steps, dry_run, force, env=env)
    _tick()

    return {"side": side, "project": project, "saEmail": sa_email,
            "keyPath": key_dest,
            "clientId": client_id_of(key_dest) if created else "",
            "ok": all(s.status != "failed" for s in steps),
            "steps": [s.as_dict() for s in steps]}


def provision(source_domain: str, target_domain: str, org: str = "",
              source_project: str = "", target_project: str = "",
              keys_dir: str = "keys", dry_run: bool = False,
              force: bool = False) -> dict:
    ready, account = gcloud_ready()
    if not ready:
        return {"ok": False, "error": account, "sides": []}
    if not can_create_projects(account):
        return {"ok": False, "sides": [], "error": (
            f"signed in as {account}, which is a service account -- Google "
            "refuses 'gcloud projects create' for those. Run 'gcloud auth "
            "login' as a user who can create projects in this org.")}

    org = org or detect_org()
    rnd = random.randint(10000, 99999)
    source_project = source_project or f"wsmig-src-{rnd}"
    target_project = target_project or f"wsmig-tgt-{rnd + 1}"

    sides = [
        provision_side("source", source_project, org,
                       os.path.join(keys_dir, "source-sa.json"), dry_run, force),
        provision_side("target", target_project, org,
                       os.path.join(keys_dir, "target-sa.json"), dry_run, force),
    ]
    return {"ok": all(s["ok"] for s in sides), "account": account, "org": org,
            "sourceDomain": source_domain, "targetDomain": target_domain,
            "sides": sides}


def render(result: dict) -> str:
    if result.get("error"):
        return f"REFUSING: {result['error']}"
    # Built outside the f-string: nested same-type quotes inside an f-string
    # are PEP 701 (Python 3.12+), and the deploy target runs 3.10 -- where
    # this is a SyntaxError at import, not a runtime failure. Caught only
    # because the VPS refused to parse the file at all.
    org = result.get("org") or "(none detected — created outside an org)"
    out = [f"gcloud account : {result.get('account')}",
           f"organisation   : {org}", ""]
    for side in result["sides"]:
        out.append(f"== {side['side'].upper()} — project {side['project']} ==")
        for st in side["steps"]:
            mark = {"ok": "ok  ", "skipped": "--  ", "failed": "FAIL"}.get(
                st["status"], "??  ")
            tail = f"  {st['detail']}" if st["detail"] else ""
            out.append(f"  {mark} {st['name']}{tail}")
        out.append("")

    ids = {s["side"]: s.get("clientId", "") for s in result["sides"]}
    if any(ids.values()):
        out += ["Domain-wide delegation is the one step with no API — a super "
                "admin must",
                "authorise these client IDs. dwd_helper.py drives that browser "
                "for you:", ""]
        for side, cid in ids.items():
            if cid:
                out.append(f"  python3 dwd_helper.py --tenant {side} "
                           f"--client-id {cid}")
        out += ["", "Then confirm what actually took effect:",
                "  python3 verify_scopes.py --tenant source",
                "  python3 verify_scopes.py --tenant target"]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[3])
    # Not `required=True`: --delete-project needs neither, and argparse's
    # own required-arg check runs before this function ever gets a chance
    # to branch on --delete-project itself. Enforced by hand below instead,
    # only on the path that actually needs them.
    ap.add_argument("--source-domain", default="")
    ap.add_argument("--target-domain", default="")
    ap.add_argument("--org-id", default="", help="auto-detected when the "
                                                 "account can see exactly one")
    ap.add_argument("--source-project", default="")
    ap.add_argument("--target-project", default="")
    ap.add_argument("--keys-dir", default="keys")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="replace existing key files")
    # Not read by provision() below -- accepted purely so it shows up in
    # `ps -eo args=`. api_server.py's gcp_status polls that to tell two
    # different accounts' concurrent provisioning runs apart, since neither
    # this process's stdout/stderr redirection nor its output file path is
    # visible to a ps listing.
    ap.add_argument("--account-id", default="")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--delete-project", default="",
                    help="tear down a project this tool created, instead "
                         "of creating anything -- gcloud projects delete, "
                         "soft-deleted and recoverable for 30 days")
    args = ap.parse_args(argv)

    if args.delete_project:
        ok, detail = delete_project(args.delete_project)
        print(json.dumps({"ok": ok, "detail": detail}, indent=2)
              if args.json else detail)
        return 0 if ok else 1

    if not args.source_domain or not args.target_domain:
        ap.error("--source-domain and --target-domain are required "
                 "(unless using --delete-project)")

    # Only offer this from the CLI, not from provision()/provision_side()
    # themselves -- those are also called non-interactively (full_setup.py,
    # the control plane), where a blocking input() would hang a request
    # with no tty to answer it. This script is meant to run on the admin's
    # own machine specifically because that is where a real terminal and
    # browser exist to complete gcloud's OAuth consent screen -- the VPS
    # this project also runs on deliberately never gets here.
    ready, detail = gcloud_ready()
    if not ready and "not installed" not in detail and sys.stdin.isatty():
        answer = input(f"{detail}\nRun 'gcloud auth login' now? [Y/n] ").strip().lower()
        if answer in ("", "y", "yes"):
            subprocess.run(["gcloud", "auth", "login"])

    result = provision(args.source_domain, args.target_domain, args.org_id,
                       args.source_project, args.target_project,
                       args.keys_dir, args.dry_run, args.force)
    print(json.dumps(result, indent=2) if args.json else render(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
