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


def gcloud_ready() -> tuple[bool, str]:
    if not shutil.which("gcloud"):
        return False, ("gcloud is not installed. https://cloud.google.com/sdk "
                       "-- or run this whole thing from Cloud Shell, which "
                       "already has it and is already authenticated.")
    rc, out = run(["gcloud", "auth", "list", "--filter=status:ACTIVE",
                   "--format=value(account)"])
    account = out.strip().splitlines()[0] if out.strip() else ""
    if rc != 0 or not account:
        return False, "no active gcloud account -- run: gcloud auth login"
    return True, account


def detect_org() -> str:
    """The org id, if this account can see exactly one.

    Worth automating: a project created outside an org cannot inherit the
    org's policies, and finding the id by hand means knowing that
    `gcloud organizations list` is where it lives. Ambiguity is returned as
    empty rather than guessed -- picking the wrong org silently is worse
    than asking.
    """
    rc, out = run(["gcloud", "organizations", "list", "--format=value(ID)"])
    if rc != 0:
        return ""
    ids = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return ids[0] if len(ids) == 1 else ""


def project_exists(project: str) -> bool:
    rc, _ = run(["gcloud", "projects", "describe", project])
    return rc == 0


def ensure_project(project: str, org: str, steps: list[Step],
                   dry_run: bool) -> bool:
    s = Step(f"project {project}")
    steps.append(s)
    if project_exists(project):
        s.status, s.detail = "skipped", "already exists"
        return True
    if dry_run:
        s.status, s.detail = "skipped", "dry run"
        return True
    argv = ["gcloud", "projects", "create", project, f"--name={project}"]
    if org:
        argv.append(f"--organization={org}")
    rc, out = run(argv, timeout=600)
    if rc != 0:
        s.status, s.detail = "failed", out.strip()[-300:]
        return False
    s.status, s.detail = "ok", f"created{' in org ' + org if org else ''}"
    return True


def enable_apis(project: str, apis: list[str], steps: list[Step],
                dry_run: bool) -> bool:
    """One API per call, deliberately.

    `gcloud services enable a b c ...` fails on a freshly created project
    with SERVICE_CONFIG_NOT_FOUND_OR_PERMISSION_DENIED naming none of them,
    while the identical list enabled individually succeeds every time. It is
    slower and it works, which is the correct trade for a step that runs
    once per tenant.
    """
    ok = True
    for api in apis:
        s = Step(f"enable {api} on {project}")
        steps.append(s)
        if dry_run:
            s.status, s.detail = "skipped", "dry run"
            continue
        rc, out = run(["gcloud", "services", "enable", api,
                       f"--project={project}"], timeout=300)
        if rc == 0:
            s.status = "ok"
        else:
            s.status, s.detail = "failed", out.strip()[-200:]
            ok = False
    return ok


def relax_key_policy(project: str, steps: list[Step], dry_run: bool) -> None:
    """Allow SA key creation on THIS project only.

    Newer Workspace orgs enforce `iam.managed.disableServiceAccountKeyCreation`
    by default. gcloud then fails key creation with
    CUSTOM_ORG_POLICY_VIOLATION and -- worse -- still creates a zero-byte
    file where the key should be, so a caller that only checks "does the
    file exist" carries on with an empty credential.

    Scoped to the project rather than lifting it org-wide: the default is a
    good one, and a migration needs the exception in exactly two places.
    """
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
        env = dict(os.environ, CLOUDSDK_CORE_PROJECT=project)
        run(["gcloud", "services", "enable", "orgpolicy.googleapis.com",
             f"--project={project}"], timeout=300)
        rc, out = run(["gcloud", "org-policies", "set-policy", path],
                      timeout=300, env=env)
        if rc == 0:
            s.status, s.detail = "ok", "key creation permitted on this project"
        else:
            # Not fatal: the org may not enforce it at all, in which case
            # nothing needed doing and key creation will simply work.
            s.status, s.detail = "skipped", out.strip()[-200:]
    except Exception as exc:      # noqa: BLE001
        s.status, s.detail = "skipped", str(exc)[:200]
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def ensure_service_account(project: str, sa: str, steps: list[Step],
                           dry_run: bool) -> str:
    email = f"{sa}@{project}.iam.gserviceaccount.com"
    s = Step(f"service account {sa}")
    steps.append(s)
    if dry_run:
        s.status, s.detail = "skipped", "dry run"
        return email
    rc, _ = run(["gcloud", "iam", "service-accounts", "describe", email,
                 f"--project={project}"])
    if rc == 0:
        s.status, s.detail = "skipped", "already exists"
        return email
    rc, out = run(["gcloud", "iam", "service-accounts", "create", sa,
                   f"--project={project}",
                   "--display-name=workspace migration"], timeout=300)
    s.status, s.detail = ("ok", email) if rc == 0 else ("failed", out.strip()[-200:])
    return email


def grant_service_usage(project: str, sa_email: str, steps: list[Step],
                        dry_run: bool) -> None:
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
                       "--condition=None"], timeout=300)
        if rc == 0:
            s.status, s.detail = "ok", "ensure_apis can now self-heal"
            return
        time.sleep(5 * (attempt + 1))
    s.status, s.detail = "failed", out.strip()[-200:]


def create_key(project: str, sa_email: str, dest: str, steps: list[Step],
               dry_run: bool, force: bool) -> bool:
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
    rc, out = run(["gcloud", "iam", "service-accounts", "keys", "create", dest,
                   f"--iam-account={sa_email}", f"--project={project}"],
                  timeout=300)
    # gcloud leaves a zero-byte file behind when the org policy blocks this,
    # so existence is not success -- check the content is real JSON.
    if rc == 0 and os.path.isfile(dest) and os.path.getsize(dest) > 0:
        try:
            with open(dest, encoding="utf-8") as fh:
                json.load(fh)
            os.chmod(dest, 0o600)
            s.status, s.detail = "ok", "created"
            return True
        except (OSError, ValueError) as exc:
            s.status, s.detail = "failed", f"key file is not valid JSON: {exc}"
            return False
    if os.path.isfile(dest) and os.path.getsize(dest) == 0:
        os.unlink(dest)     # do not leave an empty file that looks like a key
    s.status, s.detail = "failed", out.strip()[-250:]
    return False


def client_id_of(key_path: str) -> str:
    try:
        with open(key_path, encoding="utf-8") as fh:
            return json.load(fh).get("client_id", "")
    except (OSError, ValueError):
        return ""


def provision_side(side: str, project: str, org: str, key_dest: str,
                   dry_run: bool, force: bool) -> dict:
    steps: list[Step] = []
    sa = f"{side}-sa"

    if not ensure_project(project, org, steps, dry_run):
        return {"side": side, "project": project, "ok": False,
                "steps": [s.as_dict() for s in steps], "clientId": ""}

    enable_apis(project, SUPPORT_APIS + APIS, steps, dry_run)
    sa_email = ensure_service_account(project, sa, steps, dry_run)
    grant_service_usage(project, sa_email, steps, dry_run)
    relax_key_policy(project, steps, dry_run)
    created = create_key(project, sa_email, key_dest, steps, dry_run, force)

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
    out = [f"gcloud account : {result.get('account')}",
           f"organisation   : {result.get('org') or '(none detected — projects '
                                                    'will be created outside an org)'}", ""]
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
    ap.add_argument("--source-domain", required=True)
    ap.add_argument("--target-domain", required=True)
    ap.add_argument("--org-id", default="", help="auto-detected when the "
                                                 "account can see exactly one")
    ap.add_argument("--source-project", default="")
    ap.add_argument("--target-project", default="")
    ap.add_argument("--keys-dir", default="keys")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="replace existing key files")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    result = provision(args.source_domain, args.target_domain, args.org_id,
                       args.source_project, args.target_project,
                       args.keys_dir, args.dry_run, args.force)
    print(json.dumps(result, indent=2) if args.json else render(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
