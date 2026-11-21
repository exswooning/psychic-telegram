"""The change each `changed_after` scenario makes to a migrated twin (fableplan2 P2.6), one function per kind.
Every one touches a single item, on the side its name says (a. = source, b. = target), and returns a sentence
saying what it did. Source mail and contacts are written with the seeder's own key (the migration's key is
read-only there); Drive on the source needs only the migration key's `drive` scope."""
from __future__ import annotations

import io
import sys
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))


class NoItem(RuntimeError):
    """The twin holds nothing of the shape this change needs."""


class Refused(RuntimeError):
    """The campaign will not do this: a domain it is not set up for, a source that is not a declared sandbox, a
    government domain, or a destructive step the operator did not allow."""


def _guard(address, configured, what):
    """Only the domain this account's pair is configured for, and only a declared sandbox (never government)."""
    import domain_guard
    domain = address.rsplit("@", 1)[-1].lower()
    if domain != (configured or "").lower():
        raise Refused(f"{address} is not on {configured or 'a configured domain'}: the campaign only {what} its own pair")
    if domain_guard.is_government(domain):
        raise Refused(f"{domain} is a government domain: {what} it is refused, always")
    why = domain_guard.refuse_reason(domain, what.capitalize())
    if why:
        raise Refused(why)


def _mapped(db, src, type_, limit=80):
    return [(r[0], r[1]) for r in db.conn.execute(
        "SELECT source_id, target_id FROM id_mapping WHERE source_user=? AND type=? LIMIT ?", (src, type_, limit))]


def _plain_file(drive, pairs, side_fields="id,name,mimeType,size"):
    """The first mapped file on `drive` (the side given) that is not a Google-native one."""
    for sid, tid in pairs:
        try:
            f = drive.files().get(fileId=tid, fields=side_fields, supportsAllDrives=True).execute()
        except Exception:      # noqa: BLE001 - gone or not readable: next
            continue
        if not f["mimeType"].startswith("application/vnd.google-apps."):
            return sid, tid, f
    raise NoItem("no plain file mapped")


def _seed_services(settings, user):
    sys.path.insert(0, os.path.join(HERE, "data-generator"))
    import seed_sandbox as ss
    ss._use_own_key(settings)
    return ss, ss._retry_factory(settings)


def mtime(ctx):
    drive = ctx.auth.target_drive(ctx.tgt)
    _, tid, f = _plain_file(drive, _mapped(ctx.db, ctx.src, "file"))
    drive.files().update(fileId=tid, body={"modifiedTime": "2019-01-01T00:00:00.000Z"}, supportsAllDrives=True).execute()
    return f"set the modified time of {f['name']!r} on b. back to 2019"


def missing_on_target(ctx):
    drive = ctx.auth.target_drive(ctx.tgt)
    _, tid, f = _plain_file(drive, _mapped(ctx.db, ctx.src, "file"))
    drive.files().delete(fileId=tid, supportsAllDrives=True).execute()
    return f"deleted the copy of {f['name']!r} on b."


def grant_missing(ctx):
    drive = ctx.auth.target_drive(ctx.tgt)
    for _, tid in _mapped(ctx.db, ctx.src, "file"):
        try:
            perms = drive.permissions().list(fileId=tid, supportsAllDrives=True,
                                             fields="permissions(id,type,role,emailAddress,permissionDetails)").execute().get("permissions", [])
        except Exception:      # noqa: BLE001
            continue
        for p in perms:
            inherited = any(d.get("inherited") for d in p.get("permissionDetails") or [])
            if (p.get("type") == "user" and p.get("role") in ("reader", "writer", "commenter")
                    and p.get("emailAddress") != ctx.tgt and not inherited):
                try:
                    drive.permissions().delete(fileId=tid, permissionId=p["id"], supportsAllDrives=True).execute()
                except Exception:      # noqa: BLE001 - a grant the owner cannot remove: next
                    continue
                return f"removed {p['emailAddress']}'s {p['role']} grant on a file on b."
    raise NoItem("no file with a direct user grant on the target")


def content_changed(ctx):
    from googleapiclient.http import MediaIoBaseUpload
    drive = ctx.auth.source_drive(ctx.src, writable=True)
    sid, _, f = _plain_file(drive, [(s, s) for s, _ in _mapped(ctx.db, ctx.src, "file")])
    drive.files().update(fileId=sid, media_body=MediaIoBaseUpload(
        io.BytesIO(f"edited on the source {time.time()}".encode()), mimetype=f["mimeType"]), supportsAllDrives=True).execute()
    return f"rewrote the bytes of {f['name']!r} on a."


def labels_missing(ctx):
    """Take a label OFF a copied message: one of the user's own, else STARRED, IMPORTANT or the read state."""
    g = ctx.auth.target_gmail(ctx.tgt)
    for _, tid in _mapped(ctx.db, ctx.src, "message", limit=200):
        try:
            labels = g.users().messages().get(userId="me", id=tid, format="minimal").execute().get("labelIds", [])
        except Exception:      # noqa: BLE001
            continue
        if "TRASH" in labels or "DRAFT" in labels:
            continue
        take = ([l for l in labels if l.startswith("Label_")] + [l for l in ("STARRED", "IMPORTANT", "UNREAD") if l in labels])[:1]
        if take:
            g.users().messages().modify(userId="me", id=tid, body={"removeLabelIds": take}).execute()
            return f"took the label {take[0]} off one message on b."
    raise NoItem("no mapped message with a label to take off")


def unmapped_source(ctx):
    ss, retry = _seed_services(ctx.settings, ctx.src)
    _, gmail, _ = ss.build_services(ctx.settings, ctx.src)
    raw = (f"From: someone@example.test\r\nTo: {ctx.src}\r\nSubject: campaign: new mail after the run {time.time():.0f}\r\n"
           f"Message-ID: <campaign-{time.time():.0f}@seed.test>\r\nDate: Tue, 17 Nov 2026 09:00:00 +0000\r\n\r\nnew\r\n").encode()
    ss.insert_message(gmail, retry, raw, ["INBOX"])
    return "put one new message in the source mailbox"


def contacts_unmapped_source(ctx):
    ss, retry = _seed_services(ctx.settings, ctx.src)
    people, _ = ss.build_people_tasks(ctx.settings, ctx.src)
    people.people().createContact(body={"names": [{"givenName": "Campaign", "familyName": f"Added{int(time.time())}"}]}).execute()
    return "added one contact on a."


def contacts_missing_on_target(ctx):
    people = ctx.auth.target_people(ctx.tgt)
    for _, tid in _mapped(ctx.db, ctx.src, "contact"):
        try:
            people.people().deleteContact(resourceName=tid if tid.startswith("people/") else f"people/{tid}").execute()
            return "deleted one copied contact on b."
        except Exception:      # noqa: BLE001
            continue
    raise NoItem("no mapped contact")


def tasks_list_missing(ctx):
    tasks = ctx.auth.target_tasks(ctx.tgt)
    for _, tid in reversed(_mapped(ctx.db, ctx.src, "task_list")):
        try:
            tasks.tasklists().delete(tasklist=tid).execute()
            return "deleted one task list on b."
        except Exception:      # noqa: BLE001 - the default list cannot be deleted
            continue
    raise NoItem("no deletable mapped task list")


def grant_extra(ctx):
    """Give the control twin's b. address a reader grant on one copied file: a grant the source never had."""
    _guard(ctx.tgt, ctx.settings.target_domain, "changes sharing on")
    drive = ctx.auth.target_drive(ctx.tgt)
    _, tid, f = _plain_file(drive, _mapped(ctx.db, ctx.src, "file"))
    who = f"t01@{ctx.settings.target_domain}"
    drive.permissions().create(fileId=tid, body={"type": "user", "role": "reader", "emailAddress": who},
                               sendNotificationEmails=False, supportsAllDrives=True).execute()
    return f"gave {who} a reader grant the source never had on {f['name']!r} on b."


def suspend(ctx, suspended=True):
    """Directory users.update suspended=True/False on the target, as its admin."""
    _guard(ctx.tgt, ctx.settings.target_domain, "suspends users on")
    ctx.auth.directory("target", writable=True).users().update(userKey=ctx.tgt, body={"suspended": suspended}).execute()
    return f"{'suspended' if suspended else 'unsuspended'} {ctx.tgt} on b."


def source_delete_file(ctx):
    """Delete the user's newest own file on a. (a declared-sandbox source only; a government one is refused)."""
    _guard(ctx.src, ctx.settings.source_domain, "deletes from")
    drive = ctx.auth.source_drive(ctx.src, writable=True)
    files = drive.files().list(q="'me' in owners and trashed=false and mimeType!='application/vnd.google-apps.folder'",
                               orderBy="createdTime desc", pageSize=1, fields="files(id,name)").execute().get("files", [])
    if not files:
        raise NoItem("the source user owns no file")
    drive.files().delete(fileId=files[0]["id"], supportsAllDrives=True).execute()
    return f"deleted {files[0]['name']!r} on a."


def source_delete_message(ctx):
    """Trash the user's newest message on a. with the seeder's key (the migration's is read-only there)."""
    _guard(ctx.src, ctx.settings.source_domain, "deletes from")
    ss, _ = _seed_services(ctx.settings, ctx.src)
    _, gmail, _ = ss.build_services(ctx.settings, ctx.src)
    msgs = gmail.users().messages().list(userId="me", maxResults=1, q="-in:trash").execute().get("messages", [])
    if not msgs:
        raise NoItem("the source mailbox holds no message")
    gmail.users().messages().trash(userId="me", id=msgs[0]["id"]).execute()      # ponytail: trash, not purge (that needs mail.google.com)
    return "moved one message on a. to the trash"


# what the `directory` and `source` steps call
DIRECTORY = {"suspend": lambda ctx: suspend(ctx, True), "unsuspend": lambda ctx: suspend(ctx, False)}
SOURCE = {"delete_file": source_delete_file, "delete_message": source_delete_message}

# kind -> (what it does, the service its check is for)
CHANGES = {
    "drive.mtime": (mtime, "drive"), "drive.missing_on_target": (missing_on_target, "drive"),
    "drive.grant_missing": (grant_missing, "drive"), "drive.content_changed": (content_changed, "drive"),
    "gmail.labels_missing": (labels_missing, "gmail"), "gmail.unmapped_source": (unmapped_source, "gmail"),
    "contacts.unmapped_source": (contacts_unmapped_source, "contacts"),
    "contacts.missing_on_target": (contacts_missing_on_target, "contacts"),
    "tasks.list_missing": (tasks_list_missing, "tasks"), "drive.grant_extra": (grant_extra, "drive"),
}
