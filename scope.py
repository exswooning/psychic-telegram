"""
scope.py
========
The authoritative answer to "what, exactly, does this engine migrate?"

This module is deliberately data-first: the scope matrix below is a declarative
table, not prose buried in a README. It is rendered by the CLI (`main.py scope`)
and by the TUI's Scope screen, and it exports to Markdown for the change-approval
document that any real migration needs signed off before cutover.

Status values
-------------
FULL     Migrated with high fidelity. Losses, if any, are cosmetic.
PARTIAL  Migrated, but with a specific and named loss of fidelity. Every
         PARTIAL row is a conversation to have with stakeholders *before*
         cutover, not an incident to explain after it.
NONE     Not migrated by this engine. Either the API makes it impossible, or it
         needs a separate tool/pass. Named here so nobody discovers it on
         Monday morning.

The honest framing: a "100% migration" does not exist for Google Workspace
tenant-to-tenant. Commercial tools in this category (CloudM, BitTitan) carry
substantially the same NONE list. What distinguishes a good migration is that
the NONE list was agreed in advance.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from typing import Iterable, Optional

from config import SOURCE_SCOPES, TARGET_SCOPES

FULL, PARTIAL, NONE = "FULL", "PARTIAL", "NONE"


@dataclass(frozen=True)
class ScopeItem:
    service: str      # drive | gmail | calendar | identity | other
    item: str         # human-readable data element
    status: str       # FULL | PARTIAL | NONE
    note: str         # why, and what the operator should do about it


# ======================================================================
# DRIVE
# ======================================================================
DRIVE_SCOPE = [
    ScopeItem("drive", "Folder hierarchy (full depth)", FULL,
              "Mirrored depth-first; cycle- and depth-guarded"),
    ScopeItem("drive", "Binary files (PDF, video, Office, images, archives)", FULL,
              "Byte-for-byte; source md5Checksum verified against target"),
    ScopeItem("drive", "File names, descriptions, starred flag", FULL, ""),
    ScopeItem("drive", "modifiedTime on files and folders", FULL,
              "Set explicitly so 'sort by modified' stays meaningful"),
    ScopeItem("drive", "Google Docs / Sheets / Slides / Drawings", PARTIAL,
              "download_upload mode round-trips these through OOXML: content "
              "and formatting survive, comments and revision history do not. "
              "TRANSFER_MODE=server_side copies them natively instead, with no "
              "round trip and no format degradation at all"),
    ScopeItem("drive", "Native Docs larger than 10 MB", PARTIAL,
              "files.export hard-fails at 10 MB, so download_upload logs "
              "SKIPPED_EXPORT_TOO_LARGE. TRANSFER_MODE=server_side never "
              "exports, so the ceiling does not apply and these migrate fully"),
    ScopeItem("drive", "Revision / version history", NONE,
              "No API to write revisions. Target files start at revision 1"),
    ScopeItem("drive", "Comments, replies, suggestions", PARTIAL,
              "Migrated when MIGRATE_COMMENTS=true. The API cannot author a "
              "comment as someone else, so every migrated comment is written "
              "by the target user with the original author and date prefixed "
              "into the text. Suggestions (tracked changes) are not covered"),
    ScopeItem("drive", "Shortcuts", FULL,
              "Two-pass: deferred until the shortcut's target has migrated"),
    ScopeItem("drive", "Direct ACLs (reader / commenter / writer / organizer)", FULL,
              "Identity-translated via identity_map; applied with "
              "sendNotificationEmail=False"),
    ScopeItem("drive", "Domain-wide ACLs", FULL,
              "@tenantA.com rewritten to @tenantB.com; other domains pass through"),
    ScopeItem("drive", "'Anyone with the link' ACLs", FULL,
              "Preserved including allowFileDiscovery"),
    ScopeItem("drive", "External collaborator ACLs", PARTIAL,
              "Preserved verbatim, but fail if the target tenant's sharing "
              "policy forbids external sharing (403 domainPolicy)"),
    ScopeItem("drive", "ACLs for unmapped internal users", NONE,
              "Dropped and logged SKIPPED_UNMAPPED_IDENTITY. A high count "
              "usually means an incomplete identity_map, not a real change"),
    ScopeItem("drive", "Inherited (folder-derived) permissions", FULL,
              "Not copied per-file; re-derived from the migrated parent folder"),
    ScopeItem("drive", "File ownership", PARTIAL,
              "The target user genuinely owns everything that lands, in both "
              "modes. What is NOT possible is transferring the *original* "
              "file object across tenants — Drive refuses to move a file "
              "between organisations in either direction (verified directly: "
              "403 insufficientFilePermissions), so target files are new "
              "objects with new IDs. Any link to the old file ID stays "
              "pointed at the source tenant"),
    ScopeItem("drive", "Google Forms, Sites, Jamboard, Maps, Fusion Tables", PARTIAL,
              "No export representation, so download_upload logs "
              "SKIPPED_UNEXPORTABLE. TRANSFER_MODE=server_side copies them "
              "server-side instead, which does work — verified against live "
              "tenants for Forms. Linked response sheets and site publishing "
              "state still need checking by hand after cutover"),
    ScopeItem("drive", "Apps Script projects", PARTIAL,
              "Exported as JSON only. Container-bound scripts lose their binding"),
    ScopeItem("drive", "Shared Drives (Team Drives)", NONE,
              "Out of scope. Needs corpora='drive' traversal and a membership "
              "model rather than per-file ACLs — a separate engine"),
    ScopeItem("drive", "Files shared with the user but not owned", NONE,
              "Skipped when OWNED_ONLY=true (default). They arrive via their "
              "real owner's own migration"),
    ScopeItem("drive", "Trashed items", NONE,
              "Excluded by the trashed=false query filter"),
    ScopeItem("drive", "DLP / copy-protected files", NONE,
              "capabilities.canDownload=false. Logged SKIPPED_NO_DOWNLOAD"),
    ScopeItem("drive", "Drive labels and custom metadata", NONE,
              "Drive Labels API not implemented"),
    ScopeItem("drive", "Activity log / audit trail", NONE,
              "Source-tenant history is not transferable"),
]

# ======================================================================
# GMAIL
# ======================================================================
GMAIL_SCOPE = [
    ScopeItem("gmail", "All messages including Spam and Trash", FULL,
              "Raw RFC-822 blob copied untouched; narrow via GMAIL_QUERY if "
              "policy requires excluding Trash"),
    ScopeItem("gmail", "All headers (Received chain, DKIM, Message-ID)", FULL,
              "format='raw' means nothing is reconstructed"),
    ScopeItem("gmail", "Attachments and MIME structure", FULL,
              "Inside the raw blob; never re-encoded"),
    ScopeItem("gmail", "Original timestamps", FULL,
              "internalDateSource='dateHeader' — without this every message "
              "would appear to arrive on migration day"),
    ScopeItem("gmail", "Read / unread state", FULL,
              "Gmail models unread as the UNREAD label, so copying the label "
              "set carries state across. No message is spuriously marked unread"),
    ScopeItem("gmail", "Starred, Important, and Category assignments", FULL,
              "System labels share immutable IDs across all mailboxes"),
    ScopeItem("gmail", "User labels, nesting and colours", FULL,
              "Created parent-first so 'Clients/Acme/2024' resolves correctly"),
    ScopeItem("gmail", "Conversation threading", PARTIAL,
              "Reassembles from Message-ID / In-Reply-To / References headers. "
              "Near-identical in practice, but grouping is not guaranteed "
              "byte-identical to the source"),
    ScopeItem("gmail", "Spam re-classification avoided", FULL,
              "messages.insert bypasses the delivery pipeline entirely — no "
              "spam scoring, no filters firing, no forwarding rules"),
    ScopeItem("gmail", "Drafts", FULL,
              "Migrated via users.drafts, same raw-copy approach as messages. "
              "Needs no scope beyond the baseline"),
    ScopeItem("gmail", "Google Chat history via Gmail", NONE,
              "Gmail rejects CHAT as a label on messages.insert (verified: "
              "400 Invalid label). Chat is migrated through the Chat API "
              "instead -- see the 'other' section"),
    ScopeItem("gmail", "Filters and rules", PARTIAL,
              "Criteria (from/to/subject/query) copied verbatim, not identity- "
              "mapped — a criteria string can combine conditions in ways that "
              "aren't safe to pattern-match and rewrite. Label add/remove "
              "actions are remapped the same way message labels are"),
    ScopeItem("gmail", "Signatures", PARTIAL,
              "Migrated when MIGRATE_GMAIL_SETTINGS=true. Addresses inside the "
              "signature that have an identity_map entry are rewritten to their "
              "target equivalents, so a signature no longer advertises a "
              "mailbox on the tenant being decommissioned; addresses without a "
              "mapping are left verbatim rather than guessed at. Signatures on "
              "send-as aliases that do not exist on the target are logged "
              "SKIPPED_ALIAS_NOT_ON_TARGET"),
    ScopeItem("gmail", "Send-as aliases themselves", NONE,
              "Google requires the owner to confirm an alias by email before "
              "it can be used, which a migration cannot do on their behalf. "
              "Recreate aliases in the target tenant, then re-run with "
              "MIGRATE_GMAIL_SETTINGS=true to attach their signatures"),
    ScopeItem("gmail", "Vacation responder", NONE,
              "settings.vacation pass not implemented; the scope it needs "
              "(gmail.settings.basic) is already covered by "
              "MIGRATE_GMAIL_SETTINGS if this is added later"),
    ScopeItem("gmail", "Delegates and forwarding addresses", NONE,
              "Must be reconfigured in the target tenant"),
    ScopeItem("gmail", "POP / IMAP settings", NONE, "Not implemented"),
]

# ======================================================================
# CALENDAR
# ======================================================================
CALENDAR_SCOPE = [
    ScopeItem("calendar", "Primary calendar events", FULL,
              "events.import — never notifies attendees, unlike events.insert"),
    ScopeItem("calendar", "Original iCalUID", FULL,
              "Preserved, so the event stays the *same* event across systems "
              "and de-duplicates against attendees' existing copies"),
    ScopeItem("calendar", "Original organizer", FULL,
              "Preserved and identity-mapped rather than reassigned"),
    ScopeItem("calendar", "Recurring series (RRULE)", FULL,
              "Listed with singleEvents=false so the rule survives intact "
              "instead of expanding into thousands of standalone events"),
    ScopeItem("calendar", "Modified instances of a series", PARTIAL,
              "Reconciled by matching originalStartTime against the new target "
              "series, then patched with sendUpdates='none'. Instances whose "
              "master failed are logged SKIPPED_ORPHAN_EXCEPTION"),
    ScopeItem("calendar", "Attendees and their RSVP status", FULL,
              "responseStatus preserved — nobody is re-asked to accept a "
              "meeting they already accepted"),
    ScopeItem("calendar", "Start/end times, time zones, all-day events", FULL, ""),
    ScopeItem("calendar", "Reminders, visibility, transparency, colour", FULL, ""),
    ScopeItem("calendar", "extendedProperties (third-party app metadata)", FULL, ""),
    ScopeItem("calendar", "Drive attachments on events", PARTIAL,
              "Remapped through id_mapping. Dropped if the underlying file has "
              "not migrated — a dead link is worse than none. Migrate Drive first"),
    ScopeItem("calendar", "Secondary calendars owned by the user", FULL,
              "Migrated when MIGRATE_SECONDARY_CALENDARS=true: each owned "
              "calendar is recreated on the target and its events imported "
              "into it. Only calendars the user OWNS -- a subscribed calendar "
              "belongs to someone else and is left to be re-subscribed"),
    ScopeItem("calendar", "Subscribed / shared calendars", NONE,
              "Belong to another principal; re-subscribe post-cutover"),
    ScopeItem("calendar", "Calendar sharing ACLs", PARTIAL,
              "Migrated when MIGRATE_CALENDAR_ACLS=true, identity-mapped the "
              "same way Drive ACLs are and inserted with "
              "sendNotifications=false; unmapped internal users are dropped "
              "and logged rather than leaked. Costs read-only-ness on the "
              "source: acl.list is rejected under calendar.readonly, so this "
              "flag upgrades the source grant to the full calendar scope"),
    ScopeItem("calendar", "Room and equipment resources", NONE,
              "Resource addresses are tenant-specific. Dropped from attendee "
              "lists; migrate calendar resources separately and re-book"),
    ScopeItem("calendar", "Google Meet conference data", NONE,
              "Source-tenant Meet links do not resolve for target users. "
              "Stripped deliberately (conferenceDataVersion=0)"),
    ScopeItem("calendar", "Out-of-office / focus-time event types", PARTIAL,
              "Imported as ordinary events; eventType is a read-only field"),
    ScopeItem("calendar", "Working hours, appointment schedules", NONE,
              "Not exposed for write via the Calendar API"),
    ScopeItem("calendar", "Cancelled events", NONE, "Excluded by design"),
]

# ======================================================================
# IDENTITY / DIRECTORY
# ======================================================================
IDENTITY_SCOPE = [
    ScopeItem("identity", "Identity mapping (source -> target address)", FULL,
              "identity_map drives ACL, attendee and organizer translation"),
    ScopeItem("identity", "User account provisioning", NONE,
              "This engine MAPS identities, it does not CREATE them. Every "
              "target_email must already exist and be unsuspended before the "
              "run. Use GCDS, the Directory API, or your IdP"),
    ScopeItem("identity", "Passwords and 2FA enrolment", NONE,
              "Never transferable. Plan a credential-reset communication"),
    ScopeItem("identity", "Groups and group membership", NONE,
              "Provision separately. Group ACLs still translate correctly once "
              "the group exists in the target"),
    ScopeItem("identity", "Org units, aliases, admin roles, licences", NONE,
              "Directory provisioning concern, not a data-migration concern"),
]

# ======================================================================
# EVERYTHING ELSE
# ======================================================================
OTHER_SCOPE = [
    ScopeItem("other", "Google Contacts (personal + directory)", NONE,
              "People API pass not implemented"),
    ScopeItem("other", "Google Tasks", NONE, "Tasks API pass not implemented"),
    ScopeItem("other", "Google Keep", NONE, "Keep API is admin-export only"),
    ScopeItem("other", "Google Chat spaces and messages", PARTIAL,
              "Migrated when MIGRATE_CHAT=true. Named spaces are recreated in "
              "import mode and each message is replayed as its ORIGINAL "
              "sender, so a group conversation stays attributable rather than "
              "collapsing into one voice. What is lost is timestamps: a "
              "historical createTime needs app authentication with "
              "chat.import, which is rejected at token-mint (verified), so "
              "every message is stamped at migration time. Order is "
              "preserved, dates are not. Direct messages are skipped (a DM is "
              "its participants, not a name), as are card/attachment-only "
              "messages. Needs the Chat service switched ON for both "
              "organisations plus a configured Chat app in each project"),
    ScopeItem("other", "Google Vault holds, exports, retention rules", NONE,
              "Legal-hold obligations need their own export path. Confirm with "
              "counsel before decommissioning the source tenant"),
    ScopeItem("other", "Google Groups archived conversations", NONE,
              "Groups Migration API not implemented"),
    ScopeItem("other", "Looker Studio, AppSheet, Marketplace app data", NONE,
              "Each vendor has its own transfer path"),
    ScopeItem("other", "Meet recordings", PARTIAL,
              "Only insofar as they live as ordinary video files in Drive, in "
              "which case they migrate as binaries"),
]

SCOPE_MATRIX: list[ScopeItem] = (
    DRIVE_SCOPE + GMAIL_SCOPE + CALENDAR_SCOPE + IDENTITY_SCOPE + OTHER_SCOPE
)

SERVICES = ["drive", "gmail", "calendar", "identity", "other"]


# ======================================================================
# Queries and renderers
# ======================================================================
def filter_scope(services: Optional[Iterable[str]] = None,
                 statuses: Optional[Iterable[str]] = None) -> list[ScopeItem]:
    svc = {s.lower() for s in services} if services else None
    sts = {s.upper() for s in statuses} if statuses else None
    return [
        i for i in SCOPE_MATRIX
        if (svc is None or i.service in svc) and (sts is None or i.status in sts)
    ]


def counts() -> dict[str, dict[str, int]]:
    """Per-service tally of FULL/PARTIAL/NONE."""
    out: dict[str, dict[str, int]] = {}
    for i in SCOPE_MATRIX:
        out.setdefault(i.service, {FULL: 0, PARTIAL: 0, NONE: 0})
        out[i.service][i.status] += 1
    return out


def oauth_scopes(settings=None) -> dict[str, list[str]]:
    """
    The OAuth scopes each tenant's service account must be granted.

    Pass a Settings to see what the *current configuration* actually needs --
    server_side transfer mode and the optional Gmail-settings pass each widen
    the grant. With no argument this reports the baseline.
    """
    if settings is None:
        return {"source": list(SOURCE_SCOPES), "target": list(TARGET_SCOPES)}
    from config import source_scopes, target_scopes
    return {"source": source_scopes(settings), "target": target_scopes(settings)}


def as_text(services: Optional[Iterable[str]] = None,
            statuses: Optional[Iterable[str]] = None,
            width: int = 100) -> list[str]:
    """Render the matrix as plain lines (shared by CLI and TUI)."""
    marks = {FULL: "[+]", PARTIAL: "[~]", NONE: "[-]"}
    lines: list[str] = []
    items = filter_scope(services, statuses)
    note_width = max(30, width - 56)

    for svc in SERVICES:
        rows = [i for i in items if i.service == svc]
        if not rows:
            continue
        lines.append("")
        lines.append(f"== {svc.upper()} " + "=" * max(0, width - len(svc) - 4))
        for r in rows:
            lines.append(f" {marks[r.status]} {r.item[:48]:<48} {r.status:<8}")
            if r.note:
                # Wrap the note under the item.
                words, cur = r.note.split(), ""
                for w in words:
                    if len(cur) + len(w) + 1 > note_width:
                        lines.append(f"       {cur}")
                        cur = w
                    else:
                        cur = f"{cur} {w}".strip()
                if cur:
                    lines.append(f"       {cur}")
    return lines


def as_markdown() -> str:
    """Export for the change-approval document."""
    marks = {FULL: "Full", PARTIAL: "Partial", NONE: "Not migrated"}
    out = ["# Migration Scope\n",
           "Legend: **Full** = high fidelity · **Partial** = named fidelity "
           "loss · **Not migrated** = out of scope for this engine.\n"]
    tally = counts()
    out.append("| Service | Full | Partial | Not migrated |")
    out.append("|---|---:|---:|---:|")
    for svc in SERVICES:
        t = tally.get(svc, {})
        out.append(f"| {svc} | {t.get(FULL,0)} | {t.get(PARTIAL,0)} "
                   f"| {t.get(NONE,0)} |")
    for svc in SERVICES:
        rows = [i for i in SCOPE_MATRIX if i.service == svc]
        if not rows:
            continue
        out.append(f"\n## {svc.title()}\n")
        out.append("| Data element | Status | Notes |")
        out.append("|---|---|---|")
        for r in rows:
            out.append(f"| {r.item} | {marks[r.status]} | {r.note} |")

    out.append("\n## OAuth scopes required\n")
    for tenant, scopes in oauth_scopes().items():
        out.append(f"\n**{tenant.title()} tenant service account:**\n")
        out.append("```")
        out.append(",\n".join(scopes))
        out.append("```")
    return "\n".join(out)


def as_json() -> str:
    return json.dumps(
        {
            "scope": [asdict(i) for i in SCOPE_MATRIX],
            "counts": counts(),
            "oauth_scopes": oauth_scopes(),
        },
        indent=2,
    )


def planned_volume(db, source_users: Optional[list[str]] = None) -> dict:
    """
    Concrete per-tenant volume from the latest discovery rows — the "how much"
    that complements the "what" above. Returns zeros if discovery has not run.
    """
    rows = db.conn.execute(
        """SELECT d.* FROM discovery d
           JOIN (SELECT source_user, MAX(scanned_at) ts FROM discovery
                 GROUP BY source_user) m
             ON d.source_user = m.source_user AND d.scanned_at = m.ts"""
    ).fetchall()
    if source_users:
        want = {u.lower() for u in source_users}
        rows = [r for r in rows if r["source_user"] in want]

    agg = {"users": len(rows), "files": 0, "folders": 0, "native": 0,
           "bytes": 0, "messages": 0, "max_depth": 0}
    for r in rows:
        agg["files"] += r["file_count"]
        agg["folders"] += r["folder_count"]
        agg["native"] += r["native_count"]
        agg["bytes"] += r["total_bytes"]
        agg["max_depth"] = max(agg["max_depth"], r["max_depth"])
        try:
            agg["messages"] += r["messages_total"] or 0
        except (IndexError, KeyError):
            pass
    return agg
