"""
sso.py
======
Sign-in configuration: what can be migrated, what can only be inventoried,
and why the difference is not a gap in this tool.

Three things get confused under the word "SSO", and only one of them is
copyable:

  1. SSO *into* Google -- Okta, Entra, Ping et al. as the identity provider,
     with Google as the service provider. This is `inboundSamlSsoProfiles`
     plus the `inboundSsoAssignments` that decide who it applies to. Both are
     readable and creatable through the Cloud Identity API, so this module
     migrates them.

  2. "Sign in with Google" grants -- a user having authorised Slack, Zoom or
     an internal tool to use their Google account. These are *consent*, and
     there is deliberately no API that creates one: an endpoint able to grant
     an app access to a mailbox without the owner acting would be a
     vulnerability, not a feature. They are inventoried instead, because the
     useful question at cutover is "what will 141 people have to reconnect,
     and which apps hold the most grants" -- answerable, and worth answering.

  3. Saved passwords -- Chrome/Password Manager entries. No admin export or
     import exists in any API. Not inventoried either, because nothing can
     see them; they are end-to-end encrypted to the user. Users keep them by
     signing into Chrome with a personal profile, or they are lost.

The honest caveat on (1)
------------------------
Copying an SSO profile is not the whole job and this module does not pretend
otherwise. The profile names the IdP, but the IdP also has to be told about
the new tenant -- its ACS URL and entity ID both change. A migrated profile
is therefore *staged*, not live, and this module never assigns it to anyone
by default: `--assign` is a separate, deliberate step.

Why assignments are the dangerous part
--------------------------------------
An assignment decides who signs in through the IdP. Apply one that points at
a not-yet-configured profile and those users cannot log in -- including the
admin running the migration, which is how a migration locks itself out of the
tenant it is migrating into. So assignments targeting *everyone* are refused
unless explicitly forced, and org-unit and group targets are remapped by path
and email rather than by id, because ids do not survive a tenant change.

    python3 sso.py --inventory
    python3 sso.py --migrate          # profiles only, unassigned
    python3 sso.py --migrate --assign # also recreate assignments
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from auth import AuthManager        # noqa: E402
from config import Settings         # noqa: E402
from db import MigrationDB          # noqa: E402

log = logging.getLogger("sso")

# An assignment with no orgUnit and no group applies to the whole customer.
# Recreating one of those points every account at an IdP that has not been
# told about this tenant yet.
TENANT_WIDE = "customer"


class SSOMigrator:
    def __init__(self, auth: AuthManager, db: MigrationDB, settings: Settings):
        self.auth = auth
        self.db = db
        self.settings = settings
        self.stats = {"profiles": 0, "assignments": 0, "skipped": 0,
                      "failed": 0, "grants_seen": 0}
        self._target_svc = None

    def _writer(self):
        """
        Built on first write, not up front.

        A run where every assignment is refused -- the default, and the safe
        outcome -- otherwise needs a live Cloud Identity credential to decide
        it is not going to use one.
        """
        if self._target_svc is None:
            self._target_svc = self.auth.cloud_identity("target")
        return self._target_svc

    # -- reading -------------------------------------------------------------
    def read_profiles(self, tenant: str) -> list[dict]:
        svc = self.auth.cloud_identity(tenant)
        out, token = [], None
        while True:
            resp = svc.inboundSamlSsoProfiles().list(
                pageSize=100, pageToken=token).execute()
            out.extend(resp.get("inboundSamlSsoProfiles", []))
            token = resp.get("nextPageToken")
            if not token:
                return out

    def read_assignments(self, tenant: str) -> list[dict]:
        svc = self.auth.cloud_identity(tenant)
        out, token = [], None
        while True:
            resp = svc.inboundSsoAssignments().list(
                pageSize=100, pageToken=token).execute()
            out.extend(resp.get("inboundSsoAssignments", []))
            token = resp.get("nextPageToken")
            if not token:
                return out

    def read_oauth_grants(self, users: list[str]) -> dict:
        """
        Which third-party apps each user has authorised.

        Inventory only -- see the module docstring. Grouped by app rather than
        by user because the actionable output is a list of apps to warn people
        about, not 141 individual lists.
        """
        directory = self.auth.source_directory()
        by_app: dict[str, dict] = {}
        for user in users:
            try:
                resp = directory.tokens().list(userKey=user).execute()
            except Exception as exc:  # noqa: BLE001 - one user must not stop the scan
                log.debug("tokens unavailable for %s: %s", user, exc)
                continue
            for t in resp.get("items", []):
                name = t.get("displayText") or t.get("clientId") or "unknown"
                entry = by_app.setdefault(
                    name, {"users": 0, "client_id": t.get("clientId"),
                           "scopes": sorted(t.get("scopes") or [])})
                entry["users"] += 1
                self.stats["grants_seen"] += 1
        return by_app

    # -- writing -------------------------------------------------------------
    def migrate_profiles(self) -> dict:
        """
        Recreate each source profile on the target, unassigned.

        The IdP's *configuration* is copied -- entityId and the sign-in URI,
        which describe the IdP and have not moved. Its signing certificate is
        NOT: credentials are a separate sub-resource
        (inboundSamlSsoProfiles.idpCredentials), and create() has nowhere to
        put one. This docstring used to claim the certificate came with it,
        which is the sort of thing you find out by assigning the profile.

        A profile with no credential cannot validate a SAML assertion, so it
        is not a working sign-in path -- it is a shell with the right name.
        That is why nothing is assigned here, and why assign_is_safe() below
        refuses to assign one.
        """
        mapping: dict[str, str] = {}
        existing = {p.get("displayName"): p.get("name")
                    for p in self.read_profiles("target")}

        for prof in self.read_profiles("source"):
            display = prof.get("displayName") or "Imported SSO profile"
            if display in existing:
                # Idempotent for the same reason every other engine here is:
                # a resumed run must not leave two profiles with one name and
                # no way to tell which one is authoritative.
                mapping[prof["name"]] = existing[display]
                self.stats["skipped"] += 1
                continue
            if self.settings.dry_run:
                log.info("[DRY RUN] would create SSO profile %r", display)
                self.stats["profiles"] += 1
                continue

            body = {"displayName": display}
            if prof.get("idpConfig"):
                # entityId, singleSignOnServiceUri, logoutRedirectUri, and
                # changePasswordUri. Copied verbatim: they describe the IdP,
                # which is the thing that has not moved.
                body["idpConfig"] = prof["idpConfig"]
            try:
                op = self._writer().inboundSamlSsoProfiles().create(
                    body=body).execute()
            except Exception as exc:  # noqa: BLE001
                self.db.log_audit("sso", display, "sso_profile", "FAILED",
                                  str(exc))
                self.stats["failed"] += 1
                continue

            created = (op.get("response") or {}).get("name") or op.get("name")
            mapping[prof["name"]] = created
            self.db.log_audit("sso", display, "sso_profile", "SUCCESS",
                              f"created unassigned as {created}")
            self.stats["profiles"] += 1
        return mapping

    def profile_is_live(self, profile_name: str) -> tuple[bool, str]:
        """Is this target profile a working sign-in path, or a shell?

        Assigning a profile the IdP cannot complete is the lockout this
        module exists to avoid, and "it exists on the target" is not the same
        question. A profile needs both halves:

          idpConfig       who the IdP is -- copied by migrate_profiles
          idpCredentials  the signing certificate -- NOT copied, because it
                          is a separate sub-resource with no place in
                          create(); somebody has to upload it

        The certificate is the half that is missing by construction after a
        migration, so it is the half worth checking before anyone is sent
        through it.
        """
        svc = self.auth.cloud_identity("target")
        try:
            prof = svc.inboundSamlSsoProfiles().get(name=profile_name).execute()
        except Exception as exc:  # noqa: BLE001
            return False, f"cannot read target profile: {exc}"
        idp = prof.get("idpConfig") or {}
        if not idp.get("entityId") or not idp.get("singleSignOnServiceUri"):
            return False, "profile has no IdP entityId/sign-in URI"
        try:
            creds = svc.inboundSamlSsoProfiles().idpCredentials().list(
                parent=profile_name).execute().get("idpCredentials", [])
        except Exception as exc:  # noqa: BLE001
            return False, f"cannot read IdP credentials: {exc}"
        if not creds:
            return False, ("no IdP signing certificate uploaded -- migration "
                           "cannot copy one, so SAML would fail for everyone "
                           "assigned")
        return True, f"{len(creds)} credential(s), IdP {idp['entityId']}"

    def migrate_assignments(self, profile_map: dict, force_tenant_wide: bool
                            ) -> None:
        """
        Recreate who each profile applies to.

        Targets are remapped by *path* (org units) and *email* (groups),
        because ids are tenant-local and copying one across produces an
        assignment pointing at something that does not exist -- which fails
        open, sending users to a login that is not configured.
        """
        for a in self.read_assignments("source"):
            profile = a.get("samlSsoInfo", {}).get("inboundSamlSsoProfile")
            target_profile = profile_map.get(profile)
            label = a.get("name", "?")
            if not target_profile:
                self.db.log_audit("sso", label, "sso_assignment",
                                  "SKIPPED_NO_PROFILE",
                                  f"source profile {profile} was not migrated")
                self.stats["skipped"] += 1
                continue

            scope, resolved = self._remap_target(a)
            if scope == TENANT_WIDE and not force_tenant_wide:
                # The lockout case, refused by default. Everyone includes the
                # admin running this.
                self.db.log_audit(
                    "sso", label, "sso_assignment", "SKIPPED_TENANT_WIDE",
                    "applies to every account; re-run with --force-tenant-wide "
                    "once the IdP knows about this tenant")
                self.stats["skipped"] += 1
                continue
            if scope is None:
                self.db.log_audit("sso", label, "sso_assignment",
                                  "SKIPPED_UNMAPPED", resolved)
                self.stats["skipped"] += 1
                continue

            # Last, and deliberately so: the checks above are local and
            # this one is two API calls. An assignment already refused for
            # being tenant-wide or unmappable should say that, not "the
            # profile is not live" -- and should not pay for the lookup.
            live, why = self.profile_is_live(target_profile)
            if not live:
                # Refused, not warned. Everyone this assignment covers loses
                # sign-in the moment it lands, and the admin fixing it may be
                # one of them.
                self.db.log_audit("sso", label, "sso_assignment",
                                  "SKIPPED_PROFILE_NOT_LIVE", why)
                self.stats["skipped"] += 1
                continue

            # Built explicitly. The previous version set targetGroup for
            # anything that was not an org unit and popped it again when the
            # scope turned out to be tenant-wide -- so the correctness of a
            # lockout-grade field depended on a later pop still being there.
            body = {
                "rank": a.get("rank", 0),
                "samlSsoInfo": {"inboundSamlSsoProfile": target_profile},
                "ssoMode": a.get("ssoMode", "SAML_SSO"),
            }
            if scope == "orgUnit":
                body["targetOrgUnit"] = resolved
            elif scope == "group":
                body["targetGroup"] = resolved
            # TENANT_WIDE carries no target at all: that is what makes it
            # apply to everyone.
            if self.settings.dry_run:
                log.info("[DRY RUN] would assign %s to %s", target_profile, resolved)
                self.stats["assignments"] += 1
                continue
            try:
                self._writer().inboundSsoAssignments().create(
                    body=body).execute()
            except Exception as exc:  # noqa: BLE001
                self.db.log_audit("sso", label, "sso_assignment", "FAILED",
                                  str(exc))
                self.stats["failed"] += 1
                continue
            self.db.log_audit("sso", label, "sso_assignment", "SUCCESS",
                              f"applied to {resolved}")
            self.stats["assignments"] += 1

    def _remap_target(self, assignment: dict) -> tuple[str | None, str]:
        """
        Translate a source assignment target into a target-tenant one.

        Returns (scope, resolved) where scope is 'orgUnit', 'group',
        TENANT_WIDE, or None when it cannot be mapped -- in which case
        `resolved` carries the reason for the audit row.
        """
        org = assignment.get("targetOrgUnit")
        group = assignment.get("targetGroup")
        if not org and not group:
            return TENANT_WIDE, "every account in the tenant"

        if org:
            path = self._org_unit_path("source", org)
            if not path:
                return None, f"could not read source org unit {org}"
            # The root org unit IS everyone. Assigning SSO at "/" is how an
            # admin applies it tenant-wide in practice, and its scope here is
            # "orgUnit", so it walked straight past the guard that exists to
            # stop exactly this -- a staged profile applied to every account,
            # the admin running the migration included.
            #
            # It happened to fail rather than lock anyone out, because
            # "/".lstrip("/") is "" and the target lookup then 404s. Failing
            # safe by accident, reported as "target has no org unit at /",
            # which reads like a missing org unit rather than a refusal.
            if path == "/":
                return TENANT_WIDE, "the root org unit, which is every account"
            found = self._org_unit_by_path("target", path)
            if not found:
                return None, f"target has no org unit at {path}"
            return "orgUnit", found
        email = self._group_email("source", group)
        if not email:
            return None, f"could not read source group {group}"
        local = email.split("@")[0]
        candidate = f"{local}@{self.settings.target_domain}"
        if not self._group_exists("target", candidate):
            return None, f"target has no group {candidate}"
        return "group", candidate

    # -- directory lookups ---------------------------------------------------
    def _org_unit_path(self, tenant: str, resource: str) -> str | None:
        """The human path of an org unit named by a Cloud Identity resource.

        `resource` looks like "orgUnits/03ph8a2z1abcdef". orgunits.get takes
        either a real path ("/Engineering") or the unit's id, and the id form
        must be prefixed "id:" -- the bare id was being sent, which the API
        reads as a path, does not find, and returns 404 for. Every org-unit
        assignment then reported "could not read source org unit" and was
        skipped, so the remap that exists to keep ids from crossing tenants
        never ran for org units at all.

        The sibling below strips exactly this prefix off orgUnitId, which is
        how the shape was known here and not used.
        """
        directory = (self.auth.source_directory() if tenant == "source"
                     else self.auth.target_directory())
        unit_id = resource.split("/")[-1]
        if not unit_id:
            return None
        key = unit_id if unit_id.startswith("id:") else f"id:{unit_id}"
        try:
            ou = directory.orgunits().get(
                customerId="my_customer", orgUnitPath=key).execute()
            return ou.get("orgUnitPath")
        except Exception:  # noqa: BLE001
            return None

    def _org_unit_by_path(self, tenant: str, path: str) -> str | None:
        directory = (self.auth.source_directory() if tenant == "source"
                     else self.auth.target_directory())
        try:
            ou = directory.orgunits().get(
                customerId="my_customer", orgUnitPath=path.lstrip("/")).execute()
            return f"orgUnits/{ou.get('orgUnitId', '').replace('id:', '')}"
        except Exception:  # noqa: BLE001
            return None

    def _group_email(self, tenant: str, resource: str) -> str | None:
        directory = (self.auth.source_directory() if tenant == "source"
                     else self.auth.target_directory())
        try:
            g = directory.groups().get(groupKey=resource.split("/")[-1]).execute()
            return (g.get("email") or "").lower()
        except Exception:  # noqa: BLE001
            return None

    def _group_exists(self, tenant: str, email: str) -> bool:
        directory = (self.auth.source_directory() if tenant == "source"
                     else self.auth.target_directory())
        try:
            directory.groups().get(groupKey=email).execute()
            return True
        except Exception:  # noqa: BLE001
            return False


def print_inventory(mig: SSOMigrator, users: list[str]) -> None:
    profiles = mig.read_profiles("source")
    assignments = mig.read_assignments("source")

    print(f"\n=== SSO into Google (migratable) ===")
    if not profiles:
        print("  no inbound SAML profiles configured")
    for p in profiles:
        idp = (p.get("idpConfig") or {}).get("entityId", "-")
        print(f"  {p.get('displayName', '?')}\n      idp entity: {idp}")
    print(f"  {len(assignments)} assignment(s)")
    for a in assignments:
        who = (a.get("targetOrgUnit") or a.get("targetGroup")
               or "EVERY ACCOUNT IN THE TENANT")
        print(f"      -> {who}")

    print(f"\n=== 'Sign in with Google' grants (NOT migratable) ===")
    print("  Each is a user's consent. No API creates one, by design --")
    print("  people must reconnect these apps after cutover.")
    grants = mig.read_oauth_grants(users)
    for name, info in sorted(grants.items(),
                             key=lambda kv: -kv[1]["users"])[:20]:
        print(f"  {info['users']:>4} user(s)  {name}")
    if not grants:
        print("  none found (needs admin.directory.user.security)")

    print(f"\n=== Saved passwords ===")
    print("  Not migratable and not inventoriable: they are encrypted to the")
    print("  user, so no admin can read them. Users keep them by signing into")
    print("  Chrome, or they are lost.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Migrate or inventory SSO config.")
    ap.add_argument("--inventory", action="store_true")
    ap.add_argument("--migrate", action="store_true")
    ap.add_argument("--assign", action="store_true",
                    help="also recreate assignments (who the profile applies to)")
    ap.add_argument("--force-tenant-wide", action="store_true",
                    help="allow an assignment covering every account")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = Settings()
    if not settings.migrate_sso:
        print("MIGRATE_SSO is not set. This writes tenant-wide login "
              "configuration, so it stays off until asked for:\n"
              "    export MIGRATE_SSO=true")
        return 1

    db = MigrationDB(settings.db_path)
    auth = AuthManager(settings)
    mig = SSOMigrator(auth, db, settings)
    users = [r["source_email"] for r in db.all_identities()
             if r["entity_type"] == "user"]

    if args.inventory or not args.migrate:
        if args.json:
            print(json.dumps({
                "profiles": mig.read_profiles("source"),
                "assignments": mig.read_assignments("source"),
                "oauth_grants": mig.read_oauth_grants(users),
            }, indent=2))
        else:
            print_inventory(mig, users)
        return 0

    profile_map = mig.migrate_profiles()
    if args.assign:
        mig.migrate_assignments(profile_map, args.force_tenant_wide)
    else:
        print("\nProfiles created unassigned. Nothing signs in through them "
              "yet.\nPoint your IdP at this tenant's new ACS URL and entity "
              "ID first, then:\n    python3 sso.py --migrate --assign")

    print(f"\nprofiles={mig.stats['profiles']} "
          f"assignments={mig.stats['assignments']} "
          f"skipped={mig.stats['skipped']} failed={mig.stats['failed']}")
    return 1 if mig.stats["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
