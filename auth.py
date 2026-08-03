"""
auth.py
=======
Domain-Wide Delegation for both tenants.

Two service accounts, one per tenant (see README.md section 1.1) — never one
shared account impersonating both sides, which would mean the source tenant's
super-admin authorises a key that can also write into the target.

Credentials are cached per (tenant, user) because minting them is expensive.
Service objects are cached too, but *per thread* -- httplib2.Http is not
thread-safe, so a shared client would be a data race, while a fresh one per
call throws away connection pooling and re-parses the discovery document. The
cache is bounded: each entry holds an open TLS connection, and a worker thread
walks through many users over a long run.
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
                 "chat": "v1", "people": "v1", "tasks": "v1"}


class AuthManager:
    """Hands out delegated `googleapiclient` service objects for either tenant."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._creds_cache: dict[tuple[str, str], Any] = {}
        self._lock = threading.Lock()
        # Service objects are cached per thread rather than shared: each holds
        # an httplib2.Http, which is not thread-safe.
        self._local = threading.local()
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

    # Per thread, because httplib2.Http is not thread-safe. Bounded, because a
    # worker thread processes many users over a long run and each cached entry
    # holds an open TLS connection -- an unbounded cache would walk into
    # RLIMIT_NOFILE (256 soft on macOS, commonly 1024 on Linux) some hours in,
    # which surfaces as a cascade of connection errors rather than as anything
    # that names the real cause.
    _SERVICE_CACHE_MAX = 12

    def _service(self, tenant: str, api: str, user: str):
        """
        A cached, per-thread API client.

        Previously this built a fresh `httplib2.Http` and re-ran `build()` on
        every call. Where a service object is held for the life of an engine
        that cost little, but chat_engine calls `auth.target_chat(sender)`
        *per message* -- so replaying a conversation meant a new TLS handshake
        and a fresh discovery parse for every single message.

        Reusing one Http also restores connection pooling, so the second and
        subsequent calls to an API skip the handshake entirely.
        """
        cache = getattr(self._local, "services", None)
        if cache is None:
            cache = self._local.services = {}
        key = (tenant, api, user)
        svc = cache.get(key)
        if svc is not None:
            cache[key] = cache.pop(key)      # keep it fresh for LRU eviction
            return svc

        creds = self._credentials(tenant, user)
        http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=300))
        # static_discovery is left at its default: with no discoveryServiceUrl
        # set, the client already resolves that to True and uses the bundled
        # document, so there is no network fetch to avoid here.
        svc = build(api, _API_VERSIONS[api], http=http, cache_discovery=False)

        while len(cache) >= self._SERVICE_CACHE_MAX:
            # dicts are insertion-ordered, and a hit re-inserts its key above,
            # so the first key is the least recently used.
            evicted = cache.pop(next(iter(cache)))
            try:
                evicted.close()          # releases the pooled TLS connection
            except Exception:            # noqa: BLE001 - eviction must not fail a call
                pass
        cache[key] = svc
        return svc

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

    def source_people(self, user: str):
        return self._service("source", "people", user)

    def target_people(self, user: str):
        return self._service("target", "people", user)

    def source_tasks(self, user: str):
        return self._service("source", "tasks", user)

    def target_tasks(self, user: str):
        return self._service("target", "tasks", user)

    def target_directory(self):
        """Directory API as the target admin. Needed to resolve an SSO
        assignment's org unit or group on the receiving side, where the ids
        from the source tenant mean nothing."""
        creds = self._credentials("target", self.settings.target_admin)
        http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=300))
        return build("admin", "directory_v1", http=http, cache_discovery=False)

    def cloud_identity(self, tenant: str):
        """
        Cloud Identity as that tenant's admin.

        SSO is org-level configuration, not per-user data, so unlike every
        other service here this is called once per tenant as the admin rather
        than impersonated per mailbox.
        """
        admin = (self.settings.source_admin if tenant == "source"
                 else self.settings.target_admin)
        if not admin:
            raise RuntimeError(
                f"{tenant.upper()}_ADMIN is not set; Cloud Identity has to be "
                f"called as a super admin of that domain")
        creds = self._credentials(tenant, admin)
        http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=300))
        return build("cloudidentity", "v1", http=http, cache_discovery=False)

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
