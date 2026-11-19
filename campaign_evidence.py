"""Source-side proof that a seeded case exists (fableplan2 P2.6): what the fixture user holds on a., read
through the same clients the engine uses. A scenario passes on this only with the user reading IDENTICAL on b.
Each probe returns True (the case is there), False (it is not: the fixture was never made) -- never a guess."""
from __future__ import annotations


def _owned(auth, user, q):
    d = auth.source_drive(user)
    out, tok = [], None
    while True:
        r = d.files().list(q=f"'me' in owners and trashed=false and {q}", fields="nextPageToken,files(id,name,mimeType)",
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


PROBES = {
    "tasks.untitled": _untitled_task,
    "drive.comments": _comments,
    "drive.shortcut_target": lambda a, u, s: bool(_owned(a, u, "mimeType='application/vnd.google-apps.shortcut'")),
    "run.malware_flag": lambda a, u, s: bool(_owned(a, u, "name='eicar-test.txt'")),
    "drive.comment_quote_escaped": _quote_in_comment,
}


def source_has(kind: str, user: str, auth, settings):
    """True / False, or None when there is no probe for this kind."""
    probe = PROBES.get(kind)
    return None if probe is None else bool(probe(auth, user, settings))
