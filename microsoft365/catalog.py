"""Versioned workload capabilities. Native service availability is not our implementation."""
from __future__ import annotations

CATALOG_VERSION = "2026-10-29.1"
CLOUDS = {
    "commercial": {"authority": "https://login.microsoftonline.com", "graph": "https://graph.microsoft.com"},
    "usgov": {"authority": "https://login.microsoftonline.us", "graph": "https://graph.microsoft.us"},
    "dod": {"authority": "https://login.microsoftonline.us", "graph": "https://dod-graph.microsoft.us"},
    "china": {"authority": "https://login.chinacloudapi.cn", "graph": "https://microsoftgraph.chinacloudapi.cn"},
}
PROFILE_FIELDS = ("displayName", "givenName", "surname", "jobTitle", "department",
                  "companyName", "officeLocation", "mobilePhone", "businessPhones", "preferredLanguage")
USER_FIELDS = ("id", "userPrincipalName", "mail", "userType", "accountEnabled", "proxyAddresses",
               "assignedLicenses", "onPremisesSyncEnabled", "usageLocation", "preferredDataLocation", *PROFILE_FIELDS)


def entry(key, label, route, scopes=(), *, discovery=False, moves=False, delta=False, note="", docs=""):
    return {"id": key, "label": label, "proposedMethod": route, "readPermissions": list(scopes),
            "discoveryImplemented": discovery, "transferQualified": False,
            "sourceChanges": moves, "supportsDelta": delta, "note": note, "documentation": docs}


WORKLOADS = [
    entry("directory", "People and profiles", "Graph profile reconciliation", ("User.Read.All",), discovery=True,
          note="Profile patches are sandbox-only. Passwords, MFA and licenses are not copied.", docs="https://learn.microsoft.com/en-us/graph/api/user-update"),
    entry("domains", "Domains and handover", "Approved domain cutover", ("Domain.Read.All",), discovery=True,
          note="Verified domain state is evidence, not permission to change DNS or release a domain."),
    entry("organization", "Organization and licenses", "License and geography preflight", ("Organization.Read.All",), discovery=True,
          note="Organization address and usageLocation do not prove workload data residency."),
    entry("groups", "Groups, owners and members", "Graph recreation and principal mapping", ("Group.Read.All",), discovery=True),
    entry("mail", "Exchange mailboxes", "Native orchestrator / qualified Graph item copy", ("Mail.Read",), discovery=True,
          moves=True, delta=True, note="Graph message inventory is not a complete archive/recoverable-items inventory. Native moves remove source mailboxes.",
          docs="https://learn.microsoft.com/en-us/microsoft-365/migration/cross-tenant-mailbox-migration"),
    entry("calendar", "Calendars and events", "Mailbox move with separate behaviour checks", ("Calendars.Read",), discovery=True,
          note="Recurrence masters are inventoried; expansion and meeting behaviour remain separate checks."),
    entry("contacts", "Contacts and folders", "Mailbox move / qualified Graph copy", ("Contacts.Read",), discovery=True),
    entry("tasks", "To Do task lists", "Qualified Graph recreation", ("Tasks.Read.All",), discovery=True,
          note="To Do API availability and application access must be confirmed for this cloud."),
    entry("onedrive", "OneDrive files and structure", "Native move / sandbox content copy", ("Files.Read.All",), discovery=True,
          moves=True, note="Content-copy pilot does not preserve version history or sharing. Native route requires absent target site.",
          docs="https://learn.microsoft.com/en-us/microsoft-365/migration/cross-tenant-onedrive-migration"),
    entry("sharepoint", "SharePoint sites, libraries and lists", "Native site move / qualified package import", ("Sites.Read.All",), discovery=True,
          moves=True, note="Native route requires EA eligibility and absent target site. Pages, taxonomy, workflows and applications need separate qualification.",
          docs="https://learn.microsoft.com/en-us/microsoft-365/migration/cross-tenant-sharepoint-migration"),
    entry("permissions", "File sharing and versions", "Principal mapping and access verification", ("Files.Read.All",), discovery=True,
          note="Explicit per-item permission/version reads. API visibility is not proof of every effective access path."),
    entry("teams", "Teams and channels", "Structure recreation plus qualified message import", ("Team.ReadBasic.All", "Channel.ReadBasic.All", "TeamMember.Read.All", "ChannelMessage.Read.All"), discovery=True,
          note="Channel content and private/shared channel dependencies are separate from team discovery."),
    entry("chats", "Teams chats and meetings", "Microsoft Migration Orchestrator", (),
          moves=True, note="Adapter not implemented. Meetings depend on mailbox and chat migration.",
          docs="https://learn.microsoft.com/en-us/microsoft-365/migration/migration-orchestrator-1-overview"),
    entry("planner", "Planner", "Qualified plans and tasks recreation", ("Tasks.Read.All",), discovery=True,
          note="Group plans only; Premium plans and conversation history need separate qualification."),
    entry("onenote", "OneNote", "Qualified notebook migration", note="Storage copying does not prove notebooks open correctly."),
    entry("forms", "Forms and Bookings", "Reconstruction and response archive"),
    entry("collaboration", "Stream, Loop, Whiteboard and Viva", "Service-specific migration or archive"),
    entry("powerplatform", "Power Platform and Dynamics", "Environment/solution transfer plus geography move",
          moves=True, note="Tenant reassociation does not change environment region.",
          docs="https://learn.microsoft.com/en-us/power-platform/admin/move-environment-tenant"),
    entry("fabric", "Power BI and Fabric", "Qualified export and recreation"),
    entry("applications", "Entra apps and service principals", "Recreation and re-consent", ("Application.Read.All",), discovery=True,
          note="Inventory excludes credentials. New secrets, consent and application tests are required."),
    entry("devices", "Intune devices and policies", "Policy recreation and guided re-enrollment", ("DeviceManagementManagedDevices.Read.All", "DeviceManagementConfiguration.Read.All"), discovery=True,
          note="No automatic device wipe or unenrollment."),
    entry("compliance", "Purview and Defender", "Reviewed policy recreation and evidence export",
          note="Holds and protected content are blockers, never automatically removed."),
    entry("copilot", "Copilot agents and connectors", "Solutions and dependency reconnection"),
]
BY_ID = {w["id"]: w for w in WORKLOADS}
