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

import json
import logging
import os
import re
import threading
import urllib.parse
from typing import Any

import google_auth_httplib2
import httplib2
from google.oauth2 import service_account
from googleapiclient.discovery import build, build_from_document

from config import Settings, source_scopes, target_scopes
from resilience import PermanentAPIError

log = logging.getLogger(__name__)

_API_VERSIONS = {"drive": "v3", "gmail": "v1", "calendar": "v3",
                 "chat": "v1", "people": "v1", "tasks": "v1",
                 # A document's own API, for rebuilding a native file that
                 # is too large to export. Read-only on the source -- see
                 # native_api.py.
                 "sheets": "v4", "docs": "v1", "slides": "v1",
                 # A Form's responses Sheet (drive_engine._note_form_link). Read-only.
                 "forms": "v1"}


# One parsed discovery document per API, shared by every client built from it.
#
# build() parses the bundled document afresh for every client: measured 0.55 MB
# per Drive client, against 0.01 MB built from a shared parsed copy. A live run
# holds ~600 clients at once (per-thread caches across ~300 threads), and
# tracemalloc put ~300 MiB of a 2.4 GB run in exactly these parses.
#
# Sharing is only safe because of the warm-up. googleapiclient WRITES into the
# document the first time each nested resource's methods are built
# (_fix_up_parameters: parameters[name] = ..., "body", "media_body") -- lazily,
# when a thread first calls e.g. .permissions(). Two threads doing that first
# touch at once would insert keys under each other's iteration. So every nested
# resource is built once, under the lock, BEFORE the document is published;
# after that, building another client only overwrites existing keys with equal
# values, which never resizes a dict (tests/test_shared_discovery.py pins it).
_DISCOVERY: dict[tuple[str, str], dict] = {}
_DISCOVERY_LOCK = threading.Lock()


def _warm(resource, desc: dict) -> None:
    for name, sub in (desc.get("resources") or {}).items():
        _warm(getattr(resource, name)(), sub)


def shared_discovery_doc(api: str, version: str) -> dict | None:
    key = (api, version)
    doc = _DISCOVERY.get(key)
    if doc is not None:
        return doc
    with _DISCOVERY_LOCK:
        doc = _DISCOVERY.get(key)
        if doc is None:
            from googleapiclient import discovery_cache
            content = discovery_cache.get_static_doc(api, version)
            if content is None:
                return None           # no bundled copy: build() as before
            doc = json.loads(content)
            _warm(build_from_document(doc, http=httplib2.Http()), doc)
            _DISCOVERY[key] = doc
    return doc


class SourceWriteRefused(PermanentAPIError):
    """A write to the SOURCE Drive that a migration has no business making."""


# The source's last line of defence: every request a SOURCE client built by AuthManager sends goes through
# SourceReadOnlyHttp, whatever route made it -- a wrapped collection, auth.api(), a collection ReadOnlyDrive does
# not list, a batch, the raw transport (svc._http), an HttpRequest altered after it was built, an upload. Only
# reads leave the box (27 Nov 2026: ReadOnlyDrive passed every unlisted attribute through and api("source",
# "drive") was unwrapped, so on a server-side run, where the source holds the `drive` scope, one such call would
# have written to it). The deliberate writers of a declared sandbox (source_drive(writable=True), directory(
# writable=True)) pass domain_guard first and get an unguarded client.
_READ_METHODS = {"GET", "HEAD"}
_READ_POSTS = (re.compile(r"/v4/spreadsheets/[^/]+:getByDataFilter$"),
               re.compile(r"/v4/spreadsheets/[^/]+/values:batchGetByDataFilter$"),
               re.compile(r"/calendar/v3/freeBusy$"))
_COPY_PATH = re.compile(r"/drive/v3/files/[^/]+/copy$")
_FILE_PATH = re.compile(r"/drive/v3/files/[^/]+$")
_COPY_TARGETS: set = set()      # staging drives on the TARGET a source file may be copied into (allow_copy_into)
_MOVE_TARGETS: set = set()      # the same, for a move run's single files.update


def _json_body(body):
    if body in (None, b"", ""):
        return {}
    try:
        return json.loads(body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else body)
    except (ValueError, UnicodeDecodeError, AttributeError):
        return None


def check_source_request(method: str, uri: str, body=None) -> None:
    """Raise SourceWriteRefused unless this request only reads the source (or is the run's copy/move into its own
    staging drive on the target)."""
    m = (method or "GET").upper()
    parts = urllib.parse.urlsplit(str(uri))
    path = parts.path
    if m in _READ_METHODS:
        return
    if m == "POST" and any(p.search(path) for p in _READ_POSTS):
        return
    if m == "POST" and path.startswith("/batch"):
        text = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body or "")
        inner = re.findall(r"^([A-Z]+) (\S+) HTTP/1\.1", text, flags=re.M)
        if inner and all(im in _READ_METHODS for im, _ in inner):
            return
        raise SourceWriteRefused(f"source batch refused: it carries {sorted({im for im, _ in inner}) or 'no readable'} "
                                 "request(s); a batch on the source may only read")
    if m == "POST" and _COPY_PATH.search(path):
        parents = (_json_body(body) or {}).get("parents")
        if isinstance(parents, list) and len(parents) == 1 and parents[0] in _COPY_TARGETS:
            return
        raise SourceWriteRefused(f"source Drive copy into {parents} refused: a migration copies only into its own "
                                 "staging drive on the target")
    if m == "PATCH" and _FILE_PATH.search(path) and _MOVE_TARGETS:
        query = urllib.parse.parse_qs(parts.query)
        added = query.get("addParents", [""])[0]
        # A complete move only: out of the source parent and into staging (a bare addParents would leave the file
        # in both, altering the source -- found by the red-team harness, 27 Nov 2026).
        if added in _MOVE_TARGETS and query.get("removeParents", [""])[0] and _json_body(body) == {} and set(query) <= {
                "addParents", "removeParents", "supportsAllDrives", "fields", "alt"}:
            return
    raise SourceWriteRefused(f"source {m} {path} refused: a migration only reads the source")


_PROTECTED_SUBJECT_CACHE: dict = {}


def _protected_source_subject(subject: str) -> bool:
    """Is this impersonated user on a protected source domain (domain_guard's record: a source not declared a
    sandbox, or any government domain)? Cached a minute. If the protection record cannot be read, uncertainty is
    protected: a direct AuthorizedHttp client must not gain write access because the registry is unavailable."""
    import time
    domain = subject.rsplit("@", 1)[-1].lower()
    hit = _PROTECTED_SUBJECT_CACHE.get(domain)
    if hit and time.monotonic() - hit[1] < 60:
        return hit[0]
    try:
        import domain_guard
        protected = bool(domain_guard.source_refuse_reason(domain))      # includes every government domain
    except Exception as exc:      # noqa: BLE001 - registry uncertainty must not disable the transport guard
        try:
            import domain_guard
            protected = bool(domain_guard.is_government(domain))
        except Exception:      # noqa: BLE001
            protected = domain.endswith(".gov") or ".gov." in domain
        # Government classification can establish protection, but a negative result does not prove this is not a
        # configured source. Refuse direct-client writes until the registry is readable again.
        protected = True
        logging.getLogger(__name__).warning(
            "source protection of %s could not be read; refusing writes until it is available (%s)",
            domain, type(exc).__name__)
    _PROTECTED_SUBJECT_CACHE[domain] = (protected, time.monotonic())
    return protected


_unguarded_authorized_request = google_auth_httplib2.AuthorizedHttp.request


def _guarded_authorized_request(self, uri, method="GET", body=None, headers=None, *args, **kwargs):
    """Every delegated call any module makes, whoever built the client: a write acting as a user of a protected
    source domain is checked like a source client's (12 modules load the source key themselves, outside
    AuthManager). Only an explicitly writable client of a declared sandbox (_bitport_source_writes_ok) skips it."""
    subject = getattr(getattr(self, "credentials", None), "_subject", None)
    if (subject and (method or "GET").upper() not in _READ_METHODS
            and not getattr(self, "_bitport_source_writes_ok", False)
            and getattr(self, "_bitport_tenant", None) != "target" and _protected_source_subject(subject)):
        check_source_request(method, uri, body)
    return _unguarded_authorized_request(self, uri, method, body, headers, *args, **kwargs)


google_auth_httplib2.AuthorizedHttp.request = _guarded_authorized_request


class SourceReadOnlyHttp:
    """An http object for a source client: every request is checked by check_source_request before it is sent."""

    def __init__(self, inner):
        object.__setattr__(self, "_inner", inner)

    def request(self, uri, method="GET", body=None, headers=None, *args, **kwargs):
        check_source_request(method, uri, body)
        return self._inner.request(uri, method, body, headers, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __setattr__(self, name, value):
        setattr(self._inner, name, value)


# Every Drive v3 collection, and the calls on each that only read. A server-side
# copy needs the full `drive` scope on the source, so Google itself would allow any
# write there: this is what refuses them, before Google sees the request. A name that
# is not a Drive collection (a test fake's helper, the client's own internals) passes
# through untouched -- the real client exposes nothing else.
def _http(timeout: int = 300, service: str | None = None) -> httplib2.Http:
    """An httplib2 connection that does not take 308 for a redirect. Google's resumable
    uploads answer every chunk but the last with 308 "Resume Incomplete" and no Location,
    which httplib2 (0.19 on) raised as RedirectMissingLocation -- so any upload needing a
    second chunk failed: in download_upload, every file over the client's 100 MB chunk
    (found live, 2026-10-10). googleapiclient's own build_http() drops 308 the same way."""
    # A process may set a shorter bound for every request it makes (the verifier does: a request that
    # hangs must free its worker long before 300 s); the engines never set it.
    timeout = int(os.getenv("GOOGLE_HTTP_TIMEOUT_SEC", "") or timeout)
    # One API can need longer than the rest: a large Doc takes over 90 s to read, so under the verifier's 90 s
    # bound it timed out on every pass and stayed unverified for ever (info@ 1EsCRIcW..., ward15@, 8 Nov 2026).
    # GOOGLE_HTTP_TIMEOUT_SEC_DOCS (and _SLIDES, _SHEETS) overrides it for that API alone.
    if service:
        timeout = int(os.getenv("GOOGLE_HTTP_TIMEOUT_SEC_" + service.upper(), "") or timeout)
    http = httplib2.Http(timeout=timeout)
    http.redirect_codes = http.redirect_codes - {308}
    return http


_DRIVE_READS = {
    "about": {"get"},
    "accessproposals": {"get", "list", "list_next"},
    "apps": {"get", "list"},
    "approvals": {"get", "list", "list_next"},
    "changes": {"getStartPageToken", "list", "list_next"},
    "channels": set(),
    "comments": {"get", "list", "list_next"},
    "drives": {"get", "list", "list_next"},
    "files": {"get", "list", "list_next", "export", "export_media", "get_media", "listLabels"},
    "operations": {"get"},
    "permissions": {"get", "list", "list_next"},
    "replies": {"get", "list", "list_next"},
    "revisions": {"get", "get_media", "list", "list_next"},
    "teamdrives": {"get", "list", "list_next"},
}


class ReadOnlyDrive:
    """The SOURCE Drive, reads only -- plus, when `copy_into` is set, files.copy into
    that one folder: a server-side run's own staging drive on the target, the single
    write a migration makes on the source. With `moves` too (a move run, and only
    one), files.update that moves a file INTO that drive and changes nothing else.
    Anything else raises SourceWriteRefused."""

    def __init__(self, svc, copy_into: str | None = None, moves: bool = False,
                 keys_db: str | None = None):
        object.__setattr__(self, "_svc", svc)
        object.__setattr__(self, "_copy_into", copy_into)
        object.__setattr__(self, "_moves", moves)
        object.__setattr__(self, "_keys_db", keys_db)

    def __setattr__(self, name, value):      # transparent: a test fake's flags land on it
        setattr(self._svc, name, value)

    def __getattr__(self, name):
        attr = getattr(self._svc, name)
        if name not in _DRIVE_READS:
            return attr
        return lambda *a, **k: _ReadOnlyCollection(attr(*a, **k), name, self._copy_into,
                                                   self._moves, self._keys_db)


class _ReadOnlyCollection:
    def __init__(self, res, name: str, copy_into: str | None, moves: bool = False,
                 keys_db: str | None = None):
        self._res, self._name, self._copy_into, self._moves = res, name, copy_into, moves
        self._keys_db = keys_db

    def __getattr__(self, method):
        fn = getattr(self._res, method)
        if method in _DRIVE_READS[self._name]:
            # Resource keys: asked for on listings, sent on every later read of the file
            # (resource_keys.py) -- without them Google answers 404 for older link-shared files.
            import resource_keys
            return resource_keys.keyed(fn, self._name, method, self._keys_db)
        if self._name == "files" and method == "copy" and self._copy_into:
            # The copy carries the file's resource key too: an older link-shared file is "not found"
            # without it (dharan.gov.np, ward6, 23 Nov 2026: key on file, copy refused 404).
            import resource_keys
            keyed_copy = resource_keys.keyed(fn, "files", "copy", self._keys_db)

            def copy(*a, **kw):
                parents = (kw.get("body") or {}).get("parents")
                if parents != [self._copy_into]:
                    raise SourceWriteRefused(
                        f"source Drive files.copy into {parents} refused: a migration "
                        f"copies only into its own staging drive ({self._copy_into})")
                return keyed_copy(*a, **kw)
            return copy
        if self._name == "files" and method == "update" and self._moves and self._copy_into:
            def move(*a, **kw):
                if (kw.get("addParents") != self._copy_into or kw.get("body")
                        or kw.get("media_body")):
                    raise SourceWriteRefused(
                        "source Drive files.update refused: a move run only moves a file "
                        f"into its own staging drive ({self._copy_into}), changing nothing else")
                return fn(*a, **kw)
            return move

        def refused(*a, **k):
            raise SourceWriteRefused(f"source Drive {self._name}.{method} refused: a "
                                     "migration only reads the source")
        return refused


def allow_copy_into(svc, staging: str | None, moves: bool = False):
    """The same source Drive, also allowed to copy into `staging` -- and with `moves`,
    to move a file into it."""
    if isinstance(svc, ReadOnlyDrive) and staging:
        _COPY_TARGETS.add(staging)
        if moves:
            _MOVE_TARGETS.add(staging)
        return ReadOnlyDrive(svc._svc, staging, moves, svc._keys_db)
    return svc


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
        self._settle_optional_passes()
        return (source_scopes(self.settings) if tenant == "source"
                else target_scopes(self.settings))

    _settled = False
    _settle_lock = threading.Lock()

    def _settle_optional_passes(self) -> None:
        """Every optional pass is on by default, and one scope a tenant has not
        granted would fail every call this process makes. So before the first
        credential: one combined token mint per side, and only if that fails,
        each ungranted pass switched off (in this process's settings only) and
        named in the log. Key auth only -- the probe mints with the key file --
        and never when there is no key to probe with (tests, a half-set-up
        account), which behave exactly as configured."""
        if self._settled:
            return
        with self._settle_lock:
            if self._settled:
                return
            self._settled = True
            if getattr(self.settings, "auth_mode", "key") != "key":
                return
            # Either side is enough: the probe skips a side with no key. Requiring
            # both skipped the check in reset_target, which carries only the
            # target's key -- every scope was requested and the token refused.
            if not (os.path.isfile(self.settings.source_sa_key or "")
                    or os.path.isfile(self.settings.target_sa_key or "")):
                return
            try:
                import fidelity
                for note in fidelity.drop_ungranted(self.settings,
                                                    fidelity.probe_for(self.settings)):
                    log.warning("%s", note)
            except Exception as exc:      # noqa: BLE001 - never blocks a credential
                log.warning("could not check which optional passes are granted: %s", exc)

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
        subjects = getattr(self.settings, "_follow_subjects", None)
        if subjects is not None:
            key_user = user.lower()
            if (tenant, key_user) in subjects:
                user = subjects[(tenant, key_user)]
            elif key_user not in {value for (side, _), value in subjects.items() if side == tenant} and key_user != getattr(self.settings, tenant + "_admin", "").lower():
                raise RuntimeError("Domain-following run refused an unpinned delegated user")
        import domain_guard
        # Another account's protected user (d-*@a., account 8's real users): no client at all,
        # to read or to write. Its owner's migration passes.
        why = domain_guard.protected_subject(user, domain_guard.acting_account(self.settings), migration=True)
        if why:
            raise PermissionError(why)
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

    def _service(self, tenant: str, api: str, user: str, writable: bool = False):
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
        key = (tenant, api, user, writable)
        svc = cache.get(key)
        if svc is not None:
            cache[key] = cache.pop(key)      # keep it fresh for LRU eviction
            return svc

        creds = self._credentials(tenant, user)
        http = google_auth_httplib2.AuthorizedHttp(creds, http=_http(service=api))
        if tenant == "source" and not writable:
            http = SourceReadOnlyHttp(http)
        elif tenant == "source":
            http._bitport_source_writes_ok = True      # source_drive(writable=True): domain_guard passed first
        else:
            http._bitport_tenant = "target"            # after a rename the target may carry a protected name
        # static_discovery is left at its default: with no discoveryServiceUrl
        # set, the client already resolves that to True and uses the bundled
        # document, so there is no network fetch to avoid here.
        doc = shared_discovery_doc(api, _API_VERSIONS[api])
        svc = (build_from_document(doc, http=http) if doc is not None
               else build(api, _API_VERSIONS[api], http=http, cache_discovery=False))

        while len(cache) >= self._SERVICE_CACHE_MAX:
            # dicts are insertion-ordered, and a hit re-inserts its key above,
            # so the first key is the least recently used.
            evicted = cache.pop(next(iter(cache)))
            try:
                evicted.close()          # releases the pooled TLS connection
            except Exception:            # noqa: BLE001 - eviction must not fail a call
                logging.getLogger(__name__).debug("ignored an error", exc_info=True)
        cache[key] = svc
        return svc

    def api(self, tenant: str, name: str, user: str):
        """Any API in _API_VERSIONS, for either tenant, as one user.

        The shorthands below cover the fixed set the engines were written
        around. native_api needs sheets and docs on BOTH sides -- read on
        the source, write on the target -- and adding two more pairs of
        shorthands for that would be six methods to say one thing.
        """
        if tenant == "source" and name == "drive":
            return self.source_drive(user)
        return self._service(tenant, name, user)

    # -- shorthands mirrored by tests/fakes.FakeAuth ------------------------
    def source_drive(self, user: str, writable: bool = False):
        """Read-only unless the caller says otherwise, like directory(writable=).
        Only link_flip and the contract probe ever do."""
        if writable:
            import domain_guard
            domain_guard.require_not_source(self.settings.source_domain,
                                            self.settings.source_domain)
        svc = self._service("source", "drive", user, writable=writable)
        if writable:
            # Past the guard above: this source is a declared sandbox. Returning the wrapper here
            # (as a98ead0 did) left every deliberate writer -- the move check, move back, link flip,
            # the contract probe -- unable to write a byte, with "a migration only reads the source".
            return svc
        return ReadOnlyDrive(svc, keys_db=getattr(self.settings, "db_path", None))

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

    def directory(self, tenant: str, writable: bool = False,
                  groups: bool = False):
        """
        Directory API for either tenant.

        `writable` swaps in admin.directory.user (create) in place of the
        read-only scope. Target provisioning and user_profiles reconciliation
        request it explicitly. Migration's automatic profile path is gated by
        settings.auto_provision_users; source-domain guards run before any
        write client is constructed.

        `groups` adds admin.directory.group, for the group migration. Kept
        separate from `writable` so a run that creates groups cannot also
        create users by accident.
        """
        from config import DIRECTORY_WRITE_SCOPE, GROUP_WRITE_SCOPE

        if writable or groups:
            import domain_guard
            domain = (self.settings.source_domain if tenant == "source"
                      else self.settings.target_domain)
            domain_guard.require_not_source(domain, self.settings.source_domain)

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
        if groups:
            scopes.append(GROUP_WRITE_SCOPE)
        creds = service_account.Credentials.from_service_account_file(
            self._key_path(tenant), scopes=scopes
        ).with_subject(admin)
        http = google_auth_httplib2.AuthorizedHttp(creds, http=_http())
        if tenant == "source" and not (writable or groups):
            http = SourceReadOnlyHttp(http)
        elif tenant == "source":
            http._bitport_source_writes_ok = True      # domain_guard.require_not_source passed above
        else:
            http._bitport_tenant = "target"
        return build("admin", "directory_v1", http=http, cache_discovery=False)

    def source_directory(self):
        """
        Directory API as the source admin.

        Chat identifies senders as `users/{id}` and never returns an address,
        so replaying a message as its original author needs this to turn the
        id into something `identity_map` can be looked up with.
        """
        creds = self._credentials("source", self.settings.source_admin)
        http = SourceReadOnlyHttp(google_auth_httplib2.AuthorizedHttp(creds, http=_http()))
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
        http = google_auth_httplib2.AuthorizedHttp(creds, http=_http())
        http._bitport_tenant = "target"
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
        http = google_auth_httplib2.AuthorizedHttp(creds, http=_http())
        if tenant == "source":
            http = SourceReadOnlyHttp(http)
        else:
            http._bitport_tenant = "target"
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
    http = google_auth_httplib2.AuthorizedHttp(creds, http=_http())
    if tenant == "source":
        http = SourceReadOnlyHttp(http)
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
            import domain_guard
            aid = domain_guard.acting_account(auth.settings)
            return [u for u in users if not domain_guard.hidden(u, aid)]
