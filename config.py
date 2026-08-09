"""
config.py
=========
Central tunables, MIME constants, and the OAuth scope lists for both tenants.

Everything here is either a plain constant or read from the environment so
that `README.md` section 1.2's `export FOO=bar` lines are the single source of
truth for how a run is configured — nothing else in the engine hardcodes a
domain, a key path, or a scope string.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# ======================================================================
# MIME constants
# ======================================================================
TRANSFER_MODES = ("download_upload", "server_side", "link_flip")

# How a Chat space gets built on the target.
#
#   import   the space is created with importMode=True, filled, then released
#            with completeImport. Members never see a half-built space, and
#            no notification fires for a three-year-old message. Needs the
#            chat.import scope.
#   direct   an ordinary space, created and posted into live. Works without
#            chat.import, which is the point -- but members are added before
#            the backfill, so every replayed message notifies them. Fine for
#            a handful of spaces, unkind across a tenant.
CHAT_SPACE_MODES = ("import", "direct")

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"

# Native Google types this engine knows how to round-trip through an OOXML
# export/import, mapped to (export mimeType, file extension). Anything not in
# this map (Forms, Sites, Jamboard, Maps, Fusion Tables, ...) has no export
# representation and is logged SKIPPED_UNEXPORTABLE.
EXPORT_MIME_MAP: dict[str, tuple[str, str]] = {
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx",
    ),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptx",
    ),
    "application/vnd.google-apps.drawing": ("image/png", ".png"),
    "application/vnd.google-apps.script": ("application/vnd.google-apps.script+json", ".json"),
}

# Fields requested on every files.list/files.get call. Keep this the single
# definition so discovery, drive_engine and verify all see the same shape.
# `shared` is here to avoid a call, not to record anything: a file Drive
# reports as unshared has no grants but the owner's, and _sync_acls skips
# owner rows, so listing its permissions can only ever return nothing to do.
# Measured on a live tenant: populated on 504/504 files, and 372 of those
# (74%) were unshared -- so this skips two round trips, the permissions.list
# and the modifiedTime restore behind it, on roughly three files in four.
#
# Deliberately NOT `permissions(...)` inline. Drive does return the grants
# inline and the counts match permissions.list exactly, but it does not
# populate permissionDetails there even when asked -- verified against a
# folder-inherited share, where inline reported 0 inherited and
# permissions.list reported 2. _sync_acls reads that flag to honour
# recreate_inherited_acls, so adopting the inline list would silently ignore
# that setting rather than fail.
DRIVE_FILE_FIELDS = (
    "id,name,mimeType,parents,modifiedTime,size,md5Checksum,owners,shared,"
    "capabilities(canDownload),shortcutDetails,trashed,description,starred"
)

# ======================================================================
# OAuth scopes — see README.md section 1.2. These strings must match the
# Domain-Wide Delegation grant in each Admin Console *exactly*.
# ======================================================================
# The baseline grant. Everything here is needed for a default migration, and
# the source side is deliberately read-only: a source credential that cannot
# write is a structural guarantee, not just a policy.
SOURCE_SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/admin.directory.user.readonly",
    "https://www.googleapis.com/auth/admin.directory.group.readonly",
]

TARGET_SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/gmail.insert",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/admin.directory.user.readonly",
]

# Opt-in extras. These are kept out of the baseline on purpose: adding a scope
# the Admin Console has not authorised makes *every* call fail with
# `unauthorized_client`, so a feature nobody asked for must never widen the
# grant a working deployment depends on.
GMAIL_SETTINGS_SCOPE = "https://www.googleapis.com/auth/gmail.settings.basic"
DRIVE_WRITE_SCOPE = "https://www.googleapis.com/auth/drive"
DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
CALENDAR_WRITE_SCOPE = "https://www.googleapis.com/auth/calendar"
CALENDAR_READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
# Creating accounts. Never added by a migration flag -- only by the explicit
# provision-users command, because this is the one scope that can cost money.
DIRECTORY_WRITE_SCOPE = "https://www.googleapis.com/auth/admin.directory.user"
CHAT_SCOPES = [
    "https://www.googleapis.com/auth/chat.spaces",
    "https://www.googleapis.com/auth/chat.messages",
]

# Contacts and Tasks. Both have a readonly variant, so the source keeps its
# read-only property; the target needs the write scope to create anything.
CONTACTS_READONLY_SCOPE = "https://www.googleapis.com/auth/contacts.readonly"
CONTACTS_WRITE_SCOPE = "https://www.googleapis.com/auth/contacts"
TASKS_READONLY_SCOPE = "https://www.googleapis.com/auth/tasks.readonly"
TASKS_WRITE_SCOPE = "https://www.googleapis.com/auth/tasks"

# SSO. Read on the source, write on the target -- the source tenant's login
# configuration is the last thing that should be editable by a migration.
SSO_READONLY_SCOPE = (
    "https://www.googleapis.com/auth/cloud-identity.inboundsso.readonly")
SSO_WRITE_SCOPE = "https://www.googleapis.com/auth/cloud-identity.inboundsso"
# Listing which third-party apps a user has authorised. Read-only by nature:
# there is no counterpart that creates a grant, because a grant is a person
# consenting and an API that could forge one would be a vulnerability.
TOKENS_READONLY_SCOPE = (
    "https://www.googleapis.com/auth/admin.directory.user.security")

# Adding the original participants to a recreated space. Needed by both space
# modes, for different reasons: under `direct` a user cannot post into a space
# they are not in, so without this every message would fall back to the
# migrating user; under `import` the space would otherwise arrive correct in
# content and empty of everyone who was in the conversation.
CHAT_MEMBERSHIP_SCOPE = "https://www.googleapis.com/auth/chat.memberships"
# The source only ever reads the participant list, and unlike the other Chat
# scopes this one does have a read-only variant -- so the source credential
# keeps its read-only property instead of gaining the ability to add people
# to spaces in the tenant being migrated away from.
CHAT_MEMBERSHIP_READONLY_SCOPE = (
    "https://www.googleapis.com/auth/chat.memberships.readonly")

# Target-only, and not optional: every space is created with importMode=True,
# and Chat rejects that outright without this scope --
#   "Creating a space in import mode requires the chat.import authorization
#    scope"  (HTTP 403, observed on all 12 spaces of a live run)
# The source never creates anything, so granting it there would widen the
# read-only source credential for no reason.
CHAT_IMPORT_SCOPE = "https://www.googleapis.com/auth/chat.import"


def source_scopes(settings: "Settings") -> list[str]:
    """Scopes the source service account actually needs for this run."""
    scopes = list(SOURCE_SCOPES)
    if settings.transfer_mode in ("server_side", "link_flip"):
        # files.copy is a create call, so read-only will not do. This is the
        # trade the mode asks for, and it is why it is not the default.
        # link_flip additionally rewrites permissions on the source, which the
        # same write scope covers.
        scopes = [DRIVE_WRITE_SCOPE if s == DRIVE_READONLY_SCOPE else s
                 for s in scopes]
    if settings.migrate_gmail_settings:
        # No read-only variant of this scope exists, so reading filters
        # necessarily grants the ability to write them too.
        scopes.append(GMAIL_SETTINGS_SCOPE)
    if settings.migrate_chat:
        # No read-only variant exists for either scope.
        scopes.extend(CHAT_SCOPES)
        scopes.append(CHAT_MEMBERSHIP_READONLY_SCOPE)
    if settings.migrate_contacts:
        scopes.append(CONTACTS_READONLY_SCOPE)
    if settings.migrate_tasks:
        scopes.append(TASKS_READONLY_SCOPE)
    if settings.migrate_sso:
        scopes.extend([SSO_READONLY_SCOPE, TOKENS_READONLY_SCOPE])
    if settings.migrate_calendar_acls:
        # acl.list is rejected under calendar.readonly (verified: 403
        # insufficient authentication scopes), so reading sharing rules
        # requires the write scope.
        scopes = [CALENDAR_WRITE_SCOPE if s == CALENDAR_READONLY_SCOPE else s
                 for s in scopes]
    return scopes


def target_scopes(settings: "Settings") -> list[str]:
    """Scopes the target service account actually needs for this run."""
    scopes = list(TARGET_SCOPES)
    if settings.migrate_gmail_settings:
        scopes.append(GMAIL_SETTINGS_SCOPE)
    if settings.migrate_chat:
        scopes.extend(CHAT_SCOPES)
        scopes.append(CHAT_MEMBERSHIP_SCOPE)
        # Only `import` needs it, and it is the scope most likely to be
        # refused, so `direct` exists precisely to not ask for it.
        if settings.chat_space_mode == "import":
            scopes.append(CHAT_IMPORT_SCOPE)
    if settings.migrate_contacts:
        scopes.append(CONTACTS_WRITE_SCOPE)
    if settings.migrate_tasks:
        scopes.append(TASKS_WRITE_SCOPE)
    if settings.migrate_sso:
        scopes.append(SSO_WRITE_SCOPE)
    return scopes


def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")



def _auto(key: str, fallback):
    """Machine-sized default from resources.py, with a safe fallback."""
    try:
        import resources
        return resources.recommend()[key]
    except Exception:  # noqa: BLE001 - probing must never break startup
        return fallback


@dataclass
class Settings:
    """Every tunable the engine needs, defaulted from the environment."""

    # -- tenants ----------------------------------------------------------
    source_domain: str = field(default_factory=lambda: os.getenv("SOURCE_DOMAIN", "tenanta.com"))
    target_domain: str = field(default_factory=lambda: os.getenv("TARGET_DOMAIN", "tenantb.com"))
    source_admin: str = field(default_factory=lambda: os.getenv("SOURCE_ADMIN", ""))
    target_admin: str = field(default_factory=lambda: os.getenv("TARGET_ADMIN", ""))
    source_sa_key: str = field(default_factory=lambda: os.getenv("SOURCE_SA_KEY", "./keys/source-sa.json"))
    target_sa_key: str = field(default_factory=lambda: os.getenv("TARGET_SA_KEY", "./keys/target-sa.json"))

    # -- how credentials are obtained -------------------------------------
    # key         : read a downloaded service-account JSON key (the default,
    #               and what README section 1 describes).
    # impersonate : mint tokens through the IAM Credentials API instead, using
    #               whatever identity is already available in the environment.
    #               No key file exists at all, which sidesteps the
    #               disableServiceAccountKeyCreation org policy that Google now
    #               enforces by default on new organisations -- and removes the
    #               only piece of long-lived secret material in the system.
    #               Needs roles/iam.serviceAccountTokenCreator on each service
    #               account, plus <TENANT>_SA_EMAIL naming it.
    # key         : downloaded service-account JSON (default)
    # impersonate : IAM Credentials signJwt -- no key file
    # oauth       : an administrator's browser consent. No GCP project work at
    #               all for the tenant owner, which is the only version a
    #               non-technical user can complete. See oauth_store.py for
    #               what it cannot do (act as arbitrary users without a
    #               Marketplace domain-install).
    auth_mode: str = field(default_factory=lambda: os.getenv("AUTH_MODE", "key"))
    # What this run is for. Only the wizard reads it -- it decides which steps
    # apply, so a real migration never presents the seeder as an outstanding
    # task and a seed-only run is not reported as 3/9 complete forever.
    run_mode: str = field(default_factory=lambda: os.getenv("RUN_MODE", "migrate_only"))
    oauth_client_secrets: str = field(
        default_factory=lambda: os.getenv("OAUTH_CLIENT_SECRETS", "./oauth/client_secret.json")
    )
    oauth_token_dir: str = field(
        default_factory=lambda: os.getenv("OAUTH_TOKEN_DIR", "./oauth")
    )
    source_sa_email: str = field(default_factory=lambda: os.getenv("SOURCE_SA_EMAIL", ""))
    target_sa_email: str = field(default_factory=lambda: os.getenv("TARGET_SA_EMAIL", ""))

    # -- persistence / scratch ---------------------------------------------
    db_path: str = field(default_factory=lambda: os.getenv("MIGRATION_DB", "migration.db"))
    scratch_dir: str = field(default_factory=lambda: os.getenv("SCRATCH_DIR", "./scratch"))
    log_file: str = field(default_factory=lambda: os.getenv("LOG_FILE", "migration.log"))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))

    # -- concurrency / pacing ------------------------------------------------
    # Sized to the machine actually running the job unless explicitly set.
    # A fixed default is how five workers ended up on an 8 GB laptop with 85%
    # of its swap already in use: the workers stalled on swap long enough for
    # the sockets to time out, which reads as a network fault and sends you
    # debugging the wrong system. See resources.py.
    user_workers: int = field(
        default_factory=lambda: int(os.getenv("USER_WORKERS", "0")) or _auto("user_workers", 6)
    )
    per_user_qps: float = field(
        default_factory=lambda: float(os.getenv("PER_USER_QPS", "0")) or _auto("per_user_qps", 4.0)
    )

    # -- retry / backoff -----------------------------------------------------
    max_retries: int = 6
    base_backoff: float = 1.0
    max_backoff: float = 60.0

    # -- traversal / export limits -------------------------------------------
    max_recursion_depth: int = 200
    export_size_limit: int = 10 * 1024 * 1024   # files.export hard ceiling
    owned_only: bool = field(default_factory=lambda: _env_bool("OWNED_ONLY", True))

    # -- transfer mode ---------------------------------------------------------
    # download_upload : stream every file through this host. Works with a
    #                   strictly read-only source service account, which is
    #                   why it stays the default.
    # server_side     : Google copies the bytes directly into a staging shared
    #                   drive in the target org, and the target user then moves
    #                   them into their My Drive. Much faster (no bytes touch
    #                   this host), keeps native Docs native instead of doing an
    #                   OOXML round trip, sidesteps the 10 MB export ceiling,
    #                   and can carry types that have no export representation
    #                   at all (Forms, Sites, Jamboard).
    #
    #                   The catch, and it is not a small one: files.copy is a
    #                   *create* call made as the source user, so the source
    #                   service account needs the full `drive` scope rather than
    #                   `drive.readonly`. That gives up the structural guarantee
    #                   that a source credential cannot write to the source
    #                   tenant. Opt in deliberately.
    # download_upload : bytes stream through this host. The default, and the
    #                   only mode that needs no extra scope -- but it is also
    #                   what makes a memory-constrained host stall until the
    #                   sockets time out.
    # server_side     : files.copy into a staging shared drive in the target
    #                   org, then move out to the target user's My Drive.
    #                   Google moves the bytes; nothing touches this machine.
    #                   Needs the write Drive scope on the source.
    # link_flip       : server_side, but each file is first published to
    #                   "anyone with the link" and restored afterwards. For
    #                   the cross-org cases server_side alone cannot read.
    #                   NOT a default and should not become one: every file in
    #                   flight is publicly reachable until it is restored.
    #                   See link_transfer.py.
    transfer_mode: str = field(
        default_factory=lambda: os.getenv("TRANSFER_MODE", "download_upload")
    )
    # files.copy() preserves content checksums by contract -- the whole reason
    # server_side never streams bytes through this host. Verifying the returned
    # md5 is nearly free (it comes back in the copy response, not a second
    # call), but it can false-positive when the source file changed between the
    # listing and the copy. The A/B compared checksums for exactly this; with
    # the comparison phase over, a mismatch is logged and the file still counts
    # as transferred rather than failed.
    verify_server_side_md5: bool = field(
        default_factory=lambda: _env_bool("VERIFY_SERVER_SIDE_MD5", False)
    )
    staging_drive_prefix: str = field(
        default_factory=lambda: os.getenv("STAGING_DRIVE_PREFIX", "MIGRATION-STAGING")
    )

    # -- optional passes -------------------------------------------------------
    # ACL grants for one file are sent in a single HTTP round trip instead of
    # one permissions.create call per grantee. Round trips dominated the A/B
    # timing (p50 ~550ms/call), and files commonly carry 2-5 grants, so this
    # cuts the ACL phase's wall clock by roughly that factor. The BatchHttpRequest
    # is built and torn down per file; batch_size is per-request-in-batch, not
    # across files (cross-file batching would need a whole-engine post-pass).
    acl_batch_size: int = field(
        default_factory=lambda: int(os.getenv("ACL_BATCH_SIZE", "20"))
    )
    # Gmail filters need gmail.settings.basic on both tenants, which the
    # baseline grant deliberately does not include. Off unless asked for.
    migrate_gmail_settings: bool = field(
        default_factory=lambda: _env_bool("MIGRATE_GMAIL_SETTINGS", False)
    )
    # Drive comments need no extra scope, but they cost an extra API call per
    # file and cannot preserve the original author, so they are opt-in too.
    migrate_comments: bool = field(
        default_factory=lambda: _env_bool("MIGRATE_COMMENTS", False)
    )
    # When a file is shared through its parent folder, Drive reports the grant
    # as inherited. Preserving the folder hierarchy already preserves that
    # access -- but it survives only so long as the file stays in that folder.
    # Setting this recreates the grant explicitly on the target file itself,
    # so every document carries its own share access independent of the tree.
    # The cost is one permissions.create per inherited grantee per file; turn
    # it off to go back to folder-derived sharing on very large tenants.
    recreate_inherited_acls: bool = field(
        default_factory=lambda: _env_bool("MIGRATE_INHERITED_ACLS", True)
    )
    # Google Chat. Needs chat.spaces/chat.messages on BOTH tenants, plus the
    # Chat service switched on for each organisation and a Chat app configured
    # in the Cloud console. Original message timestamps cannot be preserved --
    # see chat_engine.py -- so this is a deliberate, documented trade.
    migrate_chat: bool = field(
        default_factory=lambda: _env_bool("MIGRATE_CHAT", False)
    )
    chat_space_mode: str = field(
        default_factory=lambda: os.getenv("CHAT_SPACE_MODE", "import").strip().lower()
    )
    # Off by default, despite both being cheap and worth having. Turning them
    # on adds scopes, and a scope the Admin Console has not authorised makes
    # *every* call fail with unauthorized_client -- so defaulting these to True
    # would break existing deployments on upgrade, for a feature nobody asked
    # for. Enable them deliberately, alongside the scope grant.
    # Explicit, because the library default is 100 MB and that is the number
    # resources.py has to budget against per worker. 8 MB keeps per-worker peak
    # buffer predictable without making small-file transfers chatty.
    download_chunk_bytes: int = field(
        default_factory=lambda: int(os.getenv("DOWNLOAD_CHUNK_BYTES",
                                              str(8 * 1024 * 1024)))
    )
    migrate_contacts: bool = field(
        default_factory=lambda: _env_bool("MIGRATE_CONTACTS", False)
    )
    migrate_tasks: bool = field(
        default_factory=lambda: _env_bool("MIGRATE_TASKS", False)
    )
    # Off by default and deliberately separate from the per-user services:
    # writing an SSO profile changes how *everyone* signs in, including the
    # admin running the migration, and a mistake locks the tenant out.
    migrate_sso: bool = field(
        default_factory=lambda: _env_bool("MIGRATE_SSO", False)
    )
    # Secondary calendars: everything beyond 'primary'. Works with the
    # read-only baseline grant.
    migrate_secondary_calendars: bool = field(
        default_factory=lambda: _env_bool("MIGRATE_SECONDARY_CALENDARS", False)
    )
    # Calendar sharing rules. Separate flag because acl.list has no read-only
    # variant -- it needs the full `calendar` scope on the SOURCE tenant, so
    # turning this on gives up source read-only-ness for Calendar.
    migrate_calendar_acls: bool = field(
        default_factory=lambda: _env_bool("MIGRATE_CALENDAR_ACLS", False)
    )

    # -- quota governance -----------------------------------------------------
    daily_upload_cap_gb: float = field(
        default_factory=lambda: float(os.getenv("DAILY_UPLOAD_CAP_GB", "750"))
    )

    # -- run mode --------------------------------------------------------------
    dry_run: bool = False

    def effective_upload_cap(self) -> int:
        """The per-target-user daily upload ceiling, in bytes."""
        return int(self.daily_upload_cap_gb * 1024**3)

    def __post_init__(self) -> None:
        # An unrecognised transfer mode used to be accepted and silently behave
        # as download_upload, because every check was `== "server_side"`. So a
        # typo, or a mode that is documented but not yet wired, quietly
        # streamed every byte through this host -- the exact behaviour someone
        # setting TRANSFER_MODE is usually trying to avoid.
        if self.transfer_mode not in TRANSFER_MODES:
            raise ValueError(
                f"TRANSFER_MODE={self.transfer_mode!r} is not recognised. "
                f"Valid modes: {', '.join(TRANSFER_MODES)}"
            )
        # Same reasoning as above: falling back to the default would silently
        # request chat.import for someone who set CHAT_SPACE_MODE to avoid it,
        # and the run would then fail preflight for a reason they just fixed.
        if self.chat_space_mode not in CHAT_SPACE_MODES:
            raise ValueError(
                f"CHAT_SPACE_MODE={self.chat_space_mode!r} is not recognised. "
                f"Valid modes: {', '.join(CHAT_SPACE_MODES)}"
            )
        root = os.path.dirname(os.path.abspath(__file__))
        for attr in ("source_sa_key", "target_sa_key", "db_path", "scratch_dir", "log_file", "oauth_client_secrets", "oauth_token_dir"):
            val = getattr(self, attr)
            if val and not os.path.isabs(val):
                setattr(self, attr, os.path.abspath(os.path.join(root, val)))

