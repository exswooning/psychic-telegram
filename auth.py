"""
auth.py
=======
Domain-Wide Delegation for both tenants.

Two service accounts, one per tenant (see README.md section 1.1) — never one
shared account impersonating both sides, which would mean the source tenant's
super-admin authorises a key that can also write into the target.

Each call builds a fresh `httplib2.Http` (httplib2 is not thread-safe, and the
engine is one thread per user) but credentials are cached per (tenant, user)
since minting them is what's expensive.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import google_auth_httplib2
import httplib2
from google.oauth2 import service_account
from googleapiclient.discovery import build

from config import Settings, source_scopes, target_scopes

log = logging.getLogger(__name__)

_API_VERSIONS = {"drive": "v3", "gmail": "v1", "calendar": "v3",
                 "chat": "v1"}


class AuthManager:
    """Hands out delegated `googleapiclient` service objects for either tenant."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._creds_cache: dict[tuple[str, str], Any] = {}
        self._lock = threading.Lock()
        import oauth_store
        self._token_store = oauth_store.TokenStore(settings.oauth_token_dir)

    def _key_path(self, tenant: str) -> str:
        return self.settings.source_sa_key if tenant == "source" else self.settings.target_sa_key

    def _scopes(self, tenant: str) -> list[str]:
        # Computed per run, not constant: server_side mode and the optional
        # Gmail-settings pass each widen the grant, and requesting a scope the
        # Admin Console has not authorised fails every call outright.
        return (source_scopes(self.settings) if tenant == "source"
                else target_scopes(self.settings))

    def _oauth_credentials(self, tenant: str, user: str):
        """
        Credentials from an administrator's OAuth consent.

        The important difference from delegation: OAuth grants act as the
        **consenting admin**, not as an arbitrary user. Google's APIs accept
        `userId="me"` / the admin's own Drive, but there is no `subject` to
        switch into someone else's mailbox.

        For a per-user migration that matters, so a tenant connected this way
        can only migrate the account that consented -- unless the app is
        installed domain-wide from the Marketplace, which grants delegation
        proper. This method therefore refuses loudly when asked to act as
        somebody other than the consenting account, rather than silently
        migrating the wrong mailbox.
        """
        import oauth_store

        data = self._token_store.load(tenant)
        if not data:
            raise RuntimeError(
                f"AUTH_MODE=oauth but the {tenant} tenant is not connected. "
                f"Run the web UI and use 'Connect {tenant} tenant'."
            )
        account = (data.get("account") or "").lower()
        if account and user.lower() != account:
            raise RuntimeError(
                f"OAuth for the {tenant} tenant was granted by {account}, so it "
                f"cannot act as {user}. Migrating other users needs either "
                f"domain-wide delegation (AUTH_MODE=key) or a Marketplace "
                f"domain-install."
            )
        return oauth_store.credentials_from_dict(data)

    def _sa_email(self, tenant: str) -> str:
        return (self.settings.source_sa_email if tenant == "source"
                else self.settings.target_sa_email)

    def _impersonated_credentials(self, tenant: str, user: str):
        """
        Domain-wide delegation without a downloaded key.

        A DWD token is minted from a JWT signed by the service account. That
        signature normally comes from a private key on disk -- but the
        IAM Credentials API will sign it for you (`signJwt`) if you hold
        `roles/iam.serviceAccountTokenCreator` on the account. Nothing is
        downloaded, so there is no key file to leak, expire or rotate, and no
        `disableServiceAccountKeyCreation` org policy to fight.

        The caller's own credentials come from the ambient environment
        (`gcloud auth application-default login`, a GCE/Cloud Shell service
        account, or GOOGLE_APPLICATION_CREDENTIALS), so this works anywhere
        that identity already exists.
        """
        import google.auth
        from google.auth import impersonated_credentials

        source_creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        target_principal = self._sa_email(tenant)
        if not target_principal:
            raise RuntimeError(
                f"AUTH_MODE=impersonate needs {tenant.upper()}_SA_EMAIL set to "
                f"the service account to impersonate "
                f"(e.g. source-sa@my-project.iam.gserviceaccount.com)"
            )
        return impersonated_credentials.Credentials(
            source_credentials=source_creds,
            target_principal=target_principal,
            target_scopes=self._scopes(tenant),
            # `subject` is what makes this domain-wide delegation rather than
            # plain impersonation -- it is the end user being acted for.
            subject=user,
            lifetime=3600,
        )

    def _credentials(self, tenant: str, user: str):
        key = (tenant, user)
        with self._lock:
            creds = self._creds_cache.get(key)
            if creds is None:
                if self.settings.auth_mode == "oauth":
                    creds = self._oauth_credentials(tenant, user)
                elif self.settings.auth_mode == "impersonate":
                    creds = self._impersonated_credentials(tenant, user)
                else:
                    creds = service_account.Credentials.from_service_account_file(
                        self._key_path(tenant), scopes=self._scopes(tenant)
                    ).with_subject(user)
                self._creds_cache[key] = creds
            return creds

    def _service(self, tenant: str, api: str, user: str):
        creds = self._credentials(tenant, user)
        http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=300))
        return build(api, _API_VERSIONS[api], http=http, cache_discovery=False)

    # -- shorthands mirrored by tests/fakes.FakeAuth ------------------------
    def source_drive(self, user: str):
        return self._service("source", "drive", user)

    def target_drive(self, user: str):
        return self._service("target", "drive", user)

    def source_gmail(self, user: str):
        return self._service("source", "gmail", user)

    def target_gmail(self, user: str):
        return self._service("target", "gmail", user)

    def source_calendar(self, user: str):
        return self._service("source", "calendar", user)

    def target_calendar(self, user: str):
        return self._service("target", "calendar", user)

    def source_chat(self, user: str):
        return self._service("source", "chat", user)

    def target_chat(self, user: str):
        return self._service("target", "chat", user)

    def directory(self, tenant: str, writable: bool = False):
        """
        Directory API for either tenant.

        `writable` swaps in admin.directory.user (create) in place of the
        read-only scope. Only the provision-users command passes it; nothing
        in the migration path can reach this with writable=True.
        """
        from config import DIRECTORY_WRITE_SCOPE

        admin = (self.settings.source_admin if tenant == "source"
                 else self.settings.target_admin)
        if not admin:
            raise RuntimeError(
                f"{tenant.upper()}_ADMIN is not set; the Directory API has to "
                f"be called as a super admin of that domain"
            )
        scopes = list(self._scopes(tenant))
        if writable:
            scopes.append(DIRECTORY_WRITE_SCOPE)
        creds = service_account.Credentials.from_service_account_file(
            self._key_path(tenant), scopes=scopes
        ).with_subject(admin)
        http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=300))
        return build("admin", "directory_v1", http=http, cache_discovery=False)

    def source_directory(self):
        """
        Directory API as the source admin.

        Chat identifies senders as `users/{id}` and never returns an address,
        so replaying a message as its original author needs this to turn the
        id into something `identity_map` can be looked up with.
        """
        creds = self._credentials("source", self.settings.source_admin)
        http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=300))
        return build("admin", "directory_v1", http=http, cache_discovery=False)

    def verify_delegation(self, tenant: str, user: str) -> tuple[bool, str]:
        """Mint a token and make one trivial call. Cheap way to catch a DWD
        misconfiguration in seconds instead of four hours into a batch."""
        try:
            drive = self._service(tenant, "drive", user)
            drive.about().get(fields="user").execute()
            return True, "ok"
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator verbatim
            msg = str(exc)
            # One credential carries every scope this run needs, so a scope the
            # Admin Console has not authorised fails the token exchange itself
            # -- here, on a Drive call, with no hint that Chat is the reason.
            # Print the exact list to paste, because the console's editor
            # *replaces* the scope line rather than appending to it, and a
            # half-remembered list silently drops whatever is left out.
            if "unauthorized_client" in msg or "invalid_scope" in msg:
                wanted = ",".join(self._scopes(tenant))
                msg += (f"\n\n  The {tenant} client ID is not authorised for "
                        f"every scope this run needs. Paste this exact list "
                        f"into Admin Console > Security > API controls > "
                        f"Domain-wide delegation (it replaces the line, so "
                        f"partial lists lose scopes):\n\n  {wanted}")
            return False, msg


def list_domain_users(auth: AuthManager, tenant: str, domain: str) -> list[str]:
    """
    All primary emails in `domain`, via the Admin SDK Directory API.

    Used by `main.py init-db --auto-map` to derive source->target pairs by
    matching localparts. Requires admin.directory.user.readonly, granted to
    both service accounts (see README.md section 1.2).
    """
    admin_email = auth.settings.source_admin if tenant == "source" else auth.settings.target_admin
    creds = auth._credentials(tenant, admin_email)
    http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=300))
    svc = build("admin", "directory_v1", http=http, cache_discovery=False)

    users: list[str] = []
    page_token = None
    while True:
        resp = svc.users().list(
            domain=domain, maxResults=500, pageToken=page_token,
            projection="basic", orderBy="email",
        ).execute()
        users.extend(u["primaryEmail"].lower() for u in resp.get("users", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            return users
