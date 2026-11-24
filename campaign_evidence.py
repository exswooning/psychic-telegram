"""Source-side proof that a seeded case exists (fableplan2 P2.6): what the fixture user holds on a., read
through the same clients the engine uses. A scenario passes on this only with the user reading IDENTICAL on b.
Each probe returns True (the case is there), False (it is not: the fixture was never made) -- never a guess."""
from __future__ import annotations


def _owned(auth, user, q, fields="id,name,mimeType"):
    d = auth.source_drive(user)
    out, tok = [], None
    while True:
        r = d.files().list(q=f"'me' in owners and trashed=false and {q}", fields=f"nextPageToken,files({fields})",
                           pageSize=200, pageToken=tok).execute()
        out += r.get("files", [])
        tok = r.get("nextPageToken")
        if not tok:
            return out


def _untitled_task(auth, user, _s):
    tasks = auth.api("source", "tasks", user)
    for lst in tasks.tasklists().list(maxResults=100).execute().get("items", []):
        for t in tasks.tasks().list(tasklist=lst["id"], showCompleted=True, showHidden=True, maxResults=100).execute().get("items", []):
            if not (t.get("title") or "").strip():
                return True
    return False


def _comments(auth, user, _s):
    d = auth.source_drive(user)
    for f in _owned(auth, user, "(mimeType='application/vnd.google-apps.document' or mimeType='application/vnd.google-apps.spreadsheet')"):
        if d.comments().list(fileId=f["id"], fields="comments(id)", pageSize=1).execute().get("comments"):
            return True
    return False


def _quote_in_comment(auth, user, _s):
    d = auth.source_drive(user)
    for f in _owned(auth, user, "mimeType='application/vnd.google-apps.document'"):
        for c in d.comments().list(fileId=f["id"], fields="comments(content)", pageSize=100).execute().get("comments", []):
            if '"' in (c.get("content") or ""):
                return True
    return False


def _mail(subject):
    def probe(auth, user, _s):
        g = auth.source_gmail(user)
        return bool(g.users().messages().list(userId="me", q=f'subject:"{subject}"', includeSpamTrash=True,
                                              maxResults=1).execute().get("messages"))
    return probe


def _by_name(name, mime=None):
    def probe(auth, user, _s):
        q = f"name='{name}'" + (f" and mimeType='{mime}'" if mime else "")
        return bool(_owned(auth, user, q))
    return probe


def _group_grant(auth, user, _s):
    d = auth.source_drive(user)
    for f in _owned(auth, user, "name='inherits the folder\\'s grant.txt'"):
        perms = d.permissions().list(fileId=f["id"], fields="permissions(type)").execute().get("permissions", [])
        if any(p.get("type") == "group" for p in perms):
            return True
    return False


_FORM = "application/vnd.google-apps.form"


def _shortcut_to_colleague(auth, user, _s):
    """A shortcut whose target a colleague owns: its copy may not exist yet when the shortcut is reached."""
    d = auth.source_drive(user)
    for f in _owned(auth, user, "mimeType='application/vnd.google-apps.shortcut'", "id,shortcutDetails(targetId)"):
        target = (f.get("shortcutDetails") or {}).get("targetId")
        try:
            owners = d.files().get(fileId=target, fields="owners(emailAddress)", supportsAllDrives=True).execute().get("owners", [])
        except Exception:      # noqa: BLE001 - a target this user cannot read proves nothing
            continue
        if any((o.get("emailAddress") or "").lower() != user.lower() for o in owners):
            return True
    return False


def _form_comments_refused(auth, user, _s):
    """A Form answers its comment list with 403/404 (it has none): what known_issues calls unreadable comments."""
    d = auth.source_drive(user)
    for f in _owned(auth, user, f"name='fableplan2 ward survey' and mimeType='{_FORM}'"):
        try:
            d.comments().list(fileId=f["id"], fields="comments(id)", pageSize=1).execute()
        except Exception as exc:      # noqa: BLE001
            if getattr(getattr(exc, "resp", None), "status", None) in (403, 404):
                return True
            raise
    return False


def _action_only_reply(auth, user, _s):
    d = auth.source_drive(user)
    for f in _owned(auth, user, "name='fableplan2 comments action doc'"):
        for c in d.comments().list(fileId=f["id"], fields="comments(replies(action,content))", pageSize=100).execute().get("comments", []):
            if any(r.get("action") and not (r.get("content") or "").strip() for r in c.get("replies", [])):
                return True
    return False


def _outsider_file(auth, user, _s):
    """A file in the user's own folder that someone outside the org owns (a person put it there)."""
    d = auth.source_drive(user)
    org = "@" + user.split("@")[1].lower()
    for folder in _owned(auth, user, "name='fableplan2 outside folder' and mimeType='application/vnd.google-apps.folder'"):
        kids = d.files().list(q=f"'{folder['id']}' in parents and trashed=false", fields="files(owners(emailAddress))",
                              pageSize=100).execute().get("files", [])
        if any(not (o.get("emailAddress") or "").lower().endswith(org) for k in kids for o in k.get("owners", [])):
            return True
    return False


PROBES = {
    "drive.sheet_too_large": _by_name("fableplan2 big sheet", "application/vnd.google-apps.spreadsheet"),
    "drive.embedded_images": _by_name("fableplan2 doc with image", "application/vnd.google-apps.document"),
    "run.form_rebuild": _by_name("fableplan2 ward survey", "application/vnd.google-apps.form"),
    "drive.group_grant": _group_grant,
    "gmail.no_message_id": _mail("Old mail with no Message-ID"),
    "gmail.body_link_copy": _mail("Re: link"),
    "gmail.link_owner_not_migrated": _mail("Link: a file of a user outside the run"),
    "tasks.untitled": _untitled_task,
    "drive.comments": _comments,
    "drive.shortcut_target": lambda a, u, s: bool(_owned(a, u, "mimeType='application/vnd.google-apps.shortcut'")),
    "run.malware_flag": lambda a, u, s: bool(_owned(a, u, "name='eicar-test.txt'")),
    "drive.comment_quote_escaped": _quote_in_comment,
    "drive.item_absent": _shortcut_to_colleague,
    "drive.comments_unreadable": _form_comments_refused,
    "run.comment_differs": _action_only_reply,
    "drive.unmapped_source": _outsider_file,
}


def source_has(kind: str, user: str, auth, settings):
    """True / False, or None when there is no probe for this kind."""
    probe = PROBES.get(kind)
    return None if probe is None else bool(probe(auth, user, settings))
