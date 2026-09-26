"""Which clouds this tool can move data between, and what a new one has to provide.

ONLY GOOGLE WORKSPACE IS IMPLEMENTED. Everything else here is scaffolding: it says what a
provider needs, and refuses to pretend. None of the engines (drive_engine.py, gmail_engine.py, ...)
call through this module yet -- they talk to Google's client directly, and moving them behind
`DriveProvider` is the first real piece of work for a second provider, not something this file has
done. The interface below is what the Drive engine actually calls today (see
tests/test_providers_scaffold.py, which pins that), so it is a starting point, not a design.

Adding a provider means: implement DriveProvider, set `implemented=True` on its entry in PROVIDERS
in the same change, and give it a sandbox tenant to be tested against -- the guard test fails
otherwise, so a stub cannot ship as if it worked.

What each scaffold below records is what it will need, from the vendors' public documentation as
of writing. Treat every endpoint and scope as a lead to VERIFY against a real test tenant, not as
something already exercised: the Google equivalents were each found wrong at least once by running
them (see CLAUDE.md), and these have never been run at all.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    implemented: bool
    services: tuple[str, ...]           # what it could move: files, mail, calendar, contacts, tasks
    auth: str                           # how a tenant grants this tool access
    api: str                            # the API the Drive leg would talk to
    notes: str = ""


PROVIDERS: dict[str, Provider] = {p.id: p for p in (
    Provider(
        "google", "Google Workspace", True,
        ("drive", "gmail", "calendar", "contacts", "tasks", "chat"),
        "service account with domain-wide delegation (per client id, granted in the Admin console)",
        "https://www.googleapis.com/drive/v3",
    ),
    Provider(
        "onedrive", "OneDrive for Business (Microsoft 365)", False,
        ("drive",),          # Exchange Online mail/calendar/contacts/tasks are separate Graph resources
        "Microsoft Entra app registration; application permissions (Files.Read.All to read, "
        "Files.ReadWrite.All to write) consented once by a tenant admin -- the closest thing to "
        "domain-wide delegation, so no per-user password or token is held",
        "https://graph.microsoft.com/v1.0",
        "Items are addressed by drive-item id, not a path. Files over ~4 MB need an upload session "
        "(createUploadSession). Modified time is set through fileSystemInfo.lastModifiedDateTime on "
        "create -- no post-hoc restore like Drive's. Sharing is /invite and /permissions, and an "
        "external grantee gets a sharing link rather than a direct grant. Throttling is 429 with "
        "Retry-After, and it is per app and per tenant. Native Office files are ordinary binaries "
        "here, so there is no export/rebuild path to design.",
    ),
    Provider(
        "zoho_workdrive", "Zoho WorkDrive", False,
        ("drive",),
        "OAuth 2.0 (a Zoho 'server-based' or 'self client' application) with WorkDrive scopes; "
        "acting for other members of the team needs a team-admin grant. There is no known "
        "equivalent of domain-wide delegation -- confirm whether one exists before promising "
        "per-user migration",
        "https://workdrive.zoho.com/api/v1",
        "Regional data centres: the API host and the accounts host differ by region (.com, .eu, "
        ".in, ...), and a token from one is not valid on another. The hierarchy is team > "
        "workspace > folder > file, and a workspace has its own membership, which has no Google "
        "counterpart to map onto. Downloads use a separate host.",
    ),
)}


def get(provider_id: str) -> Provider:
    try:
        return PROVIDERS[provider_id]
    except KeyError:
        raise ValueError(f"unknown provider {provider_id!r}; known: {', '.join(sorted(PROVIDERS))}") from None


def implemented() -> list[Provider]:
    return [p for p in PROVIDERS.values() if p.implemented]


class NotImplementedProvider(NotImplementedError):
    """Raised by a scaffold. Carries the provider so a caller can say which one, and why."""

    def __init__(self, provider: Provider, operation: str):
        super().__init__(f"{provider.label} is not implemented yet ({operation}); only "
                         f"{', '.join(p.label for p in implemented())} can be migrated")
        self.provider = provider


class DriveProvider:
    """What the Drive leg of a migration needs from a cloud, taken from what drive_engine.py calls.

    `Item` is a plain dict with at least: id, name, mimeType-like `kind` ('file' | 'folder' |
    'native'), size, modified (RFC 3339), parent ids, and `shared`. `Grant` is (kind, who, role):
    kind 'user' | 'group' | 'domain' | 'anyone'.
    """
    provider: Provider

    def _no(self, operation: str) -> Any:
        raise NotImplementedProvider(self.provider, operation)

    # -- reading the source -------------------------------------------------
    def list_children(self, user: str, folder_id: str | None = None) -> Iterator[dict]:
        """Everything directly inside a folder (the root when None), following pagination."""
        return self._no("list_children")

    def get_item(self, user: str, item_id: str) -> dict:
        return self._no("get_item")

    def download(self, user: str, item_id: str) -> bytes:
        """The bytes of a file. A provider with native documents also says how they export."""
        return self._no("download")

    def list_grants(self, user: str, item_id: str) -> list[tuple[str, str, str]]:
        """Who can see an item, as (kind, who, role)."""
        return self._no("list_grants")

    def list_comments(self, user: str, item_id: str) -> list[dict]:
        return self._no("list_comments")

    # -- writing the target -------------------------------------------------
    def create_folder(self, user: str, name: str, parent_id: str | None, modified: str | None = None) -> str:
        return self._no("create_folder")

    def upload(self, user: str, name: str, parent_id: str | None, data: bytes, modified: str | None = None) -> str:
        """Create a file and return its id. Large files need the provider's resumable route."""
        return self._no("upload")

    def set_modified(self, user: str, item_id: str, modified: str) -> None:
        return self._no("set_modified")

    def create_grant(self, user: str, item_id: str, grant: tuple[str, str, str]) -> None:
        return self._no("create_grant")

    def create_comment(self, user: str, item_id: str, text: str) -> None:
        return self._no("create_comment")


class OneDriveProvider(DriveProvider):
    provider = PROVIDERS["onedrive"]


class ZohoWorkDriveProvider(DriveProvider):
    provider = PROVIDERS["zoho_workdrive"]


DRIVE_PROVIDERS: dict[str, type[DriveProvider]] = {
    "onedrive": OneDriveProvider,
    "zoho_workdrive": ZohoWorkDriveProvider,
}
