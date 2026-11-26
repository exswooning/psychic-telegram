"""Read-only evidence for FablePlan2 fixture and target freshness checks."""
from __future__ import annotations

import re
from email.utils import getaddresses


# Workspace provisions two welcome notices. Bound metadata and source lookups
# even if a reset left many messages with its sender from earlier campaigns.
_MAX_WELCOME_NOTICES = 8
_MAX_FRESHNESS_PAGES = 16


def _gmail_page(page, collection):
    if not isinstance(page, dict) or page.keys() - {collection, "nextPageToken"}:
        raise ValueError("unexpected Gmail list response")
    items, token = page.get(collection, []), page.get("nextPageToken")
    if not isinstance(items, list) or (token is not None and not isinstance(token, str)):
        raise ValueError("unexpected Gmail page")
    ids = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
            raise ValueError("Gmail page item is missing its ID")
        ids.append(item["id"])
    return ids, token


def _gmail_ids(items, collection, page_size):
    token, seen_tokens, seen_ids = None, set(), set()
    for _ in range(_MAX_FRESHNESS_PAGES):
        page = items.list(userId="me", includeSpamTrash=True, maxResults=page_size, pageToken=token,
                          fields=f"nextPageToken,{collection}(id)").execute()
        ids, token = _gmail_page(page, collection)
        for item_id in ids:
            if item_id in seen_ids:
                raise ValueError("Gmail inventory repeated an item")
            seen_ids.add(item_id)
            yield item_id
        if not token:
            return
        if token in seen_tokens:
            raise ValueError("Gmail inventory repeated a page token")
        seen_tokens.add(token)
    raise ValueError("Gmail freshness inventory exceeded its page budget")


def _trashed_welcome_id(messages, target_id):
    from verify_sample import _google_welcome

    item = messages.get(userId="me", id=target_id, format="metadata", metadataHeaders=["From", "Message-ID"],
                        fields="id,labelIds,payload(headers)").execute()
    labels = item.get("labelIds", [])
    if item.get("id") != target_id or not isinstance(labels, list) or "TRASH" not in labels or "DRAFT" in labels:
        return None
    headers = item.get("payload", {}).get("headers", [])
    sender = [h["value"] for h in headers if h["name"].lower() == "from"]
    message_ids = [h["value"].strip() for h in headers if h["name"].lower() == "message-id"]
    if len(sender) != 1 or len(getaddresses(sender)) != 1 or not _google_welcome(sender[0]):
        return None
    # Only an intact, bounded Message-ID can go into the exact-ID search.
    if len(message_ids) == 1 and re.fullmatch(r"<[A-Za-z0-9._+%=-]{1,190}@[A-Za-z0-9.-]{1,63}>", message_ids[0]):
        return message_ids[0]
    return None


def target_gmail_freshness_problem(auth, source_user: str, target_user: str) -> str | None:
    """Check the target inventory and drafts. Only target-only, trashed welcome mail can remain."""
    target = auth.target_gmail(target_user).users()
    if next(_gmail_ids(target.drafts(), "drafts", 1), None) is not None:
        return "target Gmail still contains a draft"
    messages = target.messages()
    for count, target_id in enumerate(_gmail_ids(messages, "messages", 500), 1):
        if count > _MAX_WELCOME_NOTICES:
            return "target Gmail exceeds the bounded welcome-mail check"
        message_id = _trashed_welcome_id(messages, target_id)
        if message_id is None:
            return "target Gmail still contains an unexplained message"
        source_page = auth.source_gmail(source_user).users().messages().list(
            userId="me", q=f"rfc822msgid:{message_id}", includeSpamTrash=True, maxResults=1,
            fields="nextPageToken,messages(id)").execute()
        source_ids, next_page = _gmail_page(source_page, "messages")
        if source_ids or next_page:
            return "target Gmail contains a message whose absence from source Gmail was not established"
    return None


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
