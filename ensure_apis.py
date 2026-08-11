"""
ensure_apis.py
==============
Are the Cloud APIs this migration calls actually enabled on the projects
behind the service accounts -- and can we turn them on ourselves?

Why this is a separate axis from scopes
---------------------------------------
There are two independent gates between this tool and a Google API, in two
different consoles, and they fail with different errors:

  1. **Domain-wide delegation** (Workspace Admin console). The tenant's
     super admin authorises a client ID for a scope list. Missing scope ->
     `unauthorized_client`, and the token request fails outright.
     verify_scopes.py checks this axis.
  2. **API enablement** (Google Cloud console, per project). The API has to
     be switched on in the GCP project the service account lives in.
     Missing -> `SERVICE_DISABLED` / `PERMISSION_DENIED` with "has not been
     used in project N before or it is disabled".

Passing (1) tells you nothing about (2). This project hit exactly that:
domain-wide delegation showed 17/17 scopes live on the source, including
`contacts` and `tasks`, while People and Tasks were never enabled on the
project -- so `seed_contacts` and `seed_tasks` failed on every user, wrote
their exception into a `note` field, and the seeding run reported success
having produced zero contacts and zero tasks.

Why it self-heals rather than just reporting
--------------------------------------------
setup.sh already enables these at project-creation time, and its own
comment records the same bug being found once before. That only helps
projects created *after* that line was added; the ones in use here predate
it, and nothing re-checked. A migration that can turn on an API it needs is
strictly better than one that discovers the gap six hours in, on the one
service nobody tested.

Enabling needs `serviceusage.services.enable`, which a migration service
account does not have by default and arguably should not. So the honest
behaviour is: try, and when it is refused, say precisely which project,
which API, and the one command that fixes it -- rather than failing with
Google's message, which names a project *number* nobody recognises.

    python3 ensure_apis.py --tenant source            # report
    python3 ensure_apis.py --tenant source --enable   # report and fix
    python3 ensure_apis.py --tenant both --enable
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Settings  # noqa: E402

# Everything any engine or the seeder can call. Deliberately the full set
# rather than "what this run needs": a service toggled on later (contacts,
# tasks, chat) must not rediscover this problem, and enabling an API that
# goes unused costs nothing.
REQUIRED_APIS = {
    "drive.googleapis.com": "Drive",
    "gmail.googleapis.com": "Gmail",
    "calendar-json.googleapis.com": "Calendar",
    "admin.googleapis.com": "Admin SDK (directory)",
    "people.googleapis.com": "People (contacts)",
    "tasks.googleapis.com": "Tasks",
    "chat.googleapis.com": "Chat",
    "iamcredentials.googleapis.com": "IAM credentials (keyless auth)",
    "cloudidentity.googleapis.com": "Cloud Identity (groups)",
}

CLOUD_PLATFORM = "https://www.googleapis.com/auth/cloud-platform"

# APIs where enabling the service is necessary but NOT sufficient, with the
# manual step that is still required and the error you get without it.
#
# Chat is the one that bites: `gcloud services enable chat.googleapis.com`
# reports success and `serviceusage` reports ENABLED, but every call returns
# `404 Google Chat app not found` until a Chat *app* is configured in the
# Cloud console (display name, avatar, enabled state). There is no API and no
# gcloud command for that configuration, so a freshly provisioned project has
# Chat "enabled" and completely unusable -- observed directly when new
# projects replaced hand-made ones that had it configured years earlier.
NEEDS_CONSOLE_CONFIG = {
    "chat.googleapis.com": (
        "Chat also needs an app configured (name + avatar + enabled) at "
        "console.cloud.google.com/apis/api/chat.googleapis.com/hangouts-chat "
        "-- without it every call returns 404 'Google Chat app not found' "
        "even though the API reports ENABLED."),
}


def key_path(settings: Settings, tenant: str) -> str:
    return settings.source_sa_key if tenant == "source" else settings.target_sa_key


def project_of(key_file: str) -> str:
    with open(key_file, encoding="utf-8") as fh:
        return json.load(fh).get("project_id", "")


def _service_usage(key_file: str):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    # Not delegated: enabling an API is project-level IAM, not a Workspace
    # action, so the service account acts as itself rather than impersonating
    # a user. with_subject() here would ask Google to let a *user* administer
    # a Cloud project, which is not what DWD grants.
    creds = service_account.Credentials.from_service_account_file(
        key_file, scopes=[CLOUD_PLATFORM])
    return build("serviceusage", "v1", credentials=creds, cache_discovery=False)


def console_url(project: str, api: str) -> str:
    return (f"https://console.developers.google.com/apis/api/{api}/overview"
            f"?project={project}")


def check(key_file: str, project: str) -> dict:
    """api -> "ENABLED" | "DISABLED" | "UNKNOWN: <why>".

    UNKNOWN is its own answer, not a synonym for DISABLED. Being unable to
    *ask* (no serviceusage permission) and knowing it is off are different
    situations with different fixes, and collapsing them would send an
    operator to enable something that may already be on.
    """
    try:
        su = _service_usage(key_file)
    except Exception as exc:      # noqa: BLE001
        return {api: f"UNKNOWN: {str(exc)[:80]}" for api in REQUIRED_APIS}

    out = {}
    for api in REQUIRED_APIS:
        try:
            r = su.services().get(
                name=f"projects/{project}/services/{api}").execute()
            out[api] = "ENABLED" if r.get("state") == "ENABLED" else "DISABLED"
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "PERMISSION_DENIED" in msg or "403" in msg:
                out[api] = "UNKNOWN: no serviceusage permission"
            else:
                out[api] = f"UNKNOWN: {msg[:70]}"
    return out


def enable(key_file: str, project: str, apis: list[str]) -> dict:
    """api -> "" on success, or the reason it could not be enabled."""
    if not apis:
        return {}
    try:
        su = _service_usage(key_file)
    except Exception as exc:      # noqa: BLE001
        return {api: str(exc)[:120] for api in apis}

    results = {}
    for api in apis:
        try:
            su.services().enable(
                name=f"projects/{project}/services/{api}").execute()
            results[api] = ""
        except Exception as exc:  # noqa: BLE001
            results[api] = str(exc)[:160]
    return results


def ensure(settings: Settings, tenant: str, do_enable: bool = False) -> dict:
    """Check, optionally enable, and return a structured result."""
    key = key_path(settings, tenant)
    if not os.path.isfile(key):
        return {"tenant": tenant, "ok": False,
                "error": f"no service-account key at {key}"}
    project = project_of(key)
    states = check(key, project)
    disabled = [a for a, s in states.items() if s == "DISABLED"]
    unknown = [a for a, s in states.items() if s.startswith("UNKNOWN")]

    enabled_now: dict = {}
    if do_enable and disabled:
        enabled_now = enable(key, project, disabled)
        for api, err in enabled_now.items():
            if not err:
                states[api] = "ENABLED"

    return {"tenant": tenant, "project": project, "ok": not disabled,
            "states": states, "disabled": disabled, "unknown": unknown,
            "enabled_now": enabled_now}


def advice(project: str, apis: list[str]) -> str:
    """The one command that fixes it, with the project ID rather than the
    number Google's own error quotes -- nobody recognises the number."""
    return (f"  gcloud services enable {' '.join(apis)} --project={project}\n"
            f"  (or grant the service account roles/serviceusage.serviceUsageAdmin "
            f"on {project} and re-run with --enable)")


def render(result: dict) -> str:
    if result.get("error"):
        return f"{result['tenant']}: {result['error']}"
    lines = [f"Cloud APIs on {result['tenant']} (project {result['project']})", ""]
    for api, state in sorted(result["states"].items()):
        mark = {"ENABLED": "OK  ", "DISABLED": "OFF "}.get(state, "??  ")
        tail = "" if state in ("ENABLED", "DISABLED") else f"  -- {state[9:]}"
        lines.append(f"  {mark} {REQUIRED_APIS[api]:<32} {api}{tail}")

    for api, err in (result.get("enabled_now") or {}).items():
        lines.append(f"  {'enabled' if not err else 'could not enable'} {api}"
                     + (f": {err}" if err else ""))

    if result["disabled"]:
        lines += ["", "  DISABLED — calls to these fail with SERVICE_DISABLED,",
                  "  regardless of how many DWD scopes are granted:"]
        lines += [f"    {a}" for a in result["disabled"]]
        lines += ["", advice(result["project"], result["disabled"])]
    caveats = [(api, note) for api, note in NEEDS_CONSOLE_CONFIG.items()
               if result["states"].get(api) == "ENABLED"]
    if caveats:
        lines += ["", "  Enabled, but NOT yet usable without a manual step:"]
        for api, note in caveats:
            lines.append(f"    {api}")
            lines.append(f"      {note}")

    if result["unknown"]:
        lines += ["", "  Could not determine (this is not the same as 'off'):"]
        lines += [f"    {a}" for a in result["unknown"]]
        lines.append("  The service account cannot read serviceusage on this "
                     "project, so\n  enablement can only be checked from the "
                     "Cloud console.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[3])
    ap.add_argument("--tenant", choices=["source", "target", "both"],
                    default="both")
    ap.add_argument("--enable", action="store_true",
                    help="turn on anything that is off (needs "
                         "serviceusage.services.enable)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    settings = Settings()
    tenants = ["source", "target"] if args.tenant == "both" else [args.tenant]
    results = [ensure(settings, t, args.enable) for t in tenants]

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            print(render(r))
            print()
    # Unknown is not failure: a run that simply cannot ask should not block.
    return 1 if any(r.get("disabled") for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
