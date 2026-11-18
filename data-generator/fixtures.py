"""The campaign cohort's named fixtures (fableplan2 P2.4): shapes the standard seed does not make.

A cohort file (data-generator/cohorts/*.json) names each user, its size and its fixtures. The standard seed
(Drive corpus, mail, calendar, contacts, tasks, mail settings) runs first for every user; the fixtures named
for that user then add what a campaign scenario needs: mail of every awkward shape, calendar event types,
awkward tasks, a malware test file, bulk volume. A fixture never raises: what Google refuses is a note in its
result, so one refused shape does not lose the rest of the user's seed.

`REGISTRY` is the one list of names. tests/test_cohort.py fails when a cohort or a campaign scenario names a
fixture that is not here.
"""
from __future__ import annotations

import base64
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

REGISTRY: dict[str, Callable] = {}
PARAMS_ALLOWED = {"bulk_messages", "bulk_files", "workers"}


def fixture(name: str):
    def register(fn):
        REGISTRY[name] = fn
        return fn
    return register


@dataclass
class Ctx:
    """What a fixture may use. `seed` is the seed_sandbox module (its helpers, not a copy of them)."""
    seed: object
    settings: object
    user: str
    peers: list
    external: str
    drive: object
    gmail: object
    cal: object
    retry: Callable
    items: dict | None
    params: dict = field(default_factory=dict)
    build_people_tasks: Callable | None = None


def _made(ctx: Ctx, kind: str, q: str) -> bool:
    """Whether a fixture's own marker item exists already: every fixture runs at most once per user, so a
    --fixtures-only pass for a new fixture never doubles the old ones (18 Nov 2026: c06's calendar shapes)."""
    if kind == "drive":
        call = lambda: ctx.drive.files().list(q=f"name='{q}' and trashed=false", pageSize=1, fields="files(id)").execute()  # noqa: E731
        return bool(ctx.retry(call)().get("files"))
    if kind == "gmail":
        call = lambda: ctx.gmail.users().messages().list(userId="me", q=q, maxResults=1, includeSpamTrash=True).execute()  # noqa: E731
        return bool(ctx.retry(call)().get("messages"))
    call = lambda: ctx.cal.events().list(calendarId="primary", q=q, maxResults=1).execute()  # noqa: E731
    return bool(ctx.retry(call)().get("items"))


def run_all(names, ctx: Ctx) -> dict:
    out = {}
    for name in names:
        try:
            out[name] = REGISTRY[name](ctx)
        except Exception as exc:      # noqa: BLE001 - one fixture must not lose the user's other content
            out[name] = {"made": 0, "notes": [f"{type(exc).__name__}: {str(exc)[:160]}"]}
            print(f"  ! fixture {name} for {ctx.user}: {exc}")
    return out


# ---- the cohort file ---------------------------------------------------------------------------------------
def load_cohort(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        cohort = json.load(fh)
    problems = []
    seen = set()
    for u in cohort.get("users", []):
        lp = (u.get("localpart") or "").strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9.-]{1,30}", lp):
            problems.append(f"bad localpart {lp!r}")
        if lp in seen:
            problems.append(f"{lp} appears twice")
        seen.add(lp)
        for name in u.get("fixtures", []):
            if name not in REGISTRY:
                problems.append(f"{lp}: unknown fixture {name!r}")
        for k in (u.get("params") or {}):
            if k not in PARAMS_ALLOWED:
                problems.append(f"{lp}: unknown param {k!r}")
    if problems:
        raise ValueError("cohort file refused: " + "; ".join(problems))
    return cohort


def entries_from_cohort(cohort: dict, domain: str) -> list[dict]:
    """seed_sandbox entries for a cohort, and each user's content key into corpus.CONTENT_KEYS."""
    import corpus
    out = []
    for i, u in enumerate(cohort["users"]):
        template = corpus.ORG[i % len(corpus.ORG)]
        email = f"{u['localpart'].lower()}@{domain}"
        if u.get("content_key"):
            corpus.CONTENT_KEYS[email] = u["content_key"]
        entry = {"local": u["localpart"].lower(), "email": email, "dept": template["dept"], "project": template["project"]}
        entry.update({k: u[k] for k in ("scale", "mail", "events", "edge_cases", "fixtures", "params") if k in u})
        out.append(entry)
    return out


# ---- mail shapes -------------------------------------------------------------------------------------------
def _stamp(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%a, %d %b %Y %H:%M:%S +0000")


def _headers(ctx: Ctx, subject: str, *, message_id: str | None, days_ago: int = 20) -> list[str]:
    sender = (ctx.peers or [ctx.external])[0]
    h = [f"From: {sender}", f"To: {ctx.user}", f"Subject: {subject}", f"Date: {_stamp(days_ago)}"]
    if message_id:
        h.append(f"Message-ID: <{message_id}>")
    return h


def _raw(headers: list[str], body: str, *, ctype: str = "text/plain; charset=utf-8") -> bytes:
    return ("\r\n".join(headers + ["MIME-Version: 1.0", f"Content-Type: {ctype}", ""]) + "\r\n" + body + "\r\n").encode("utf-8")


def _with_attachment(ctx: Ctx, subject: str, size_bytes: int, tag: str) -> bytes:
    """A message whose RAW size is about `size_bytes`: one random attachment, base64 at 76 columns."""
    blob = base64.encodebytes(os.urandom(int(size_bytes * 0.73))).decode("ascii").replace("\n", "\r\n")
    boundary = f"b-{uuid.uuid4().hex}"
    head = "\r\n".join(_headers(ctx, subject, message_id=f"{tag}-{uuid.uuid4().hex[:8]}@seed.test") +
                       ["MIME-Version: 1.0", f'Content-Type: multipart/mixed; boundary="{boundary}"', ""])
    body = (f"--{boundary}\r\nContent-Type: text/plain\r\n\r\nSee the attachment.\r\n--{boundary}\r\n"
            f'Content-Type: application/octet-stream; name="data.bin"\r\nContent-Transfer-Encoding: base64\r\n'
            f'Content-Disposition: attachment; filename="data.bin"\r\n\r\n{blob}\r\n--{boundary}--\r\n')
    return (head + "\r\n" + body).encode("ascii")


@fixture("mail_shapes")
def mail_shapes(ctx: Ctx) -> dict:
    """Mail of every awkward shape the engine and the checks have met: sizes either side of the 5 MB upload
    threshold, no Message-ID, a Message-ID far down a long header block, non-ASCII headers and body, one
    Message-ID twice, an encrypted body, and each kind of Drive-link mail."""
    if _made(ctx, "gmail", 'subject:"One Message-ID, twice"'):
        return {"made": 0, "notes": ["already made"]}
    made, notes = 0, []
    s = ctx.seed

    def put(label: str, raw: bytes, labels=("INBOX",)):
        nonlocal made
        try:
            s.insert_message(ctx.gmail, ctx.retry, raw, list(labels))
            made += 1
        except Exception as exc:      # noqa: BLE001
            notes.append(f"{label}: {str(exc)[:120]}")

    pad = "\r\n ".join("a" * 900 for _ in range(11))             # about 10 KB of header before the Message-ID
    put("long header block", ("\r\n".join(_headers(ctx, "Message-ID far down the headers", message_id=None) +
                                          [f"X-Pad: {pad}", f"Message-ID: <far-down-{uuid.uuid4().hex[:8]}@seed.test>"])
                              + "\r\n\r\nbody\r\n").encode("ascii"))
    put("no message-id", _raw(_headers(ctx, "Old mail with no Message-ID", message_id=None, days_ago=900), "an old message"))
    dup = _raw(_headers(ctx, "One Message-ID, twice", message_id=f"twice-{uuid.uuid4().hex[:8]}@seed.test"), "same id")
    put("same Message-ID (1)", dup)
    put("same Message-ID (2)", dup)
    put("non-ASCII subject (encoded words) and body",
        _raw(_headers(ctx, "=?utf-8?B?" + base64.b64encode("नेपाल बैठक".encode()).decode() + "?=",
                      message_id=f"ne-{uuid.uuid4().hex[:8]}@seed.test"), "नमस्ते — बैठक भोलि १० बजे।",
             ctype="text/plain; charset=utf-8"))
    put("non-ASCII bytes in a header",
        _raw(_headers(ctx, "raw 8-bit header", message_id=f"raw8-{uuid.uuid4().hex[:8]}@seed.test") + ["X-Note: नमस्ते"], "body"))
    enc = base64.encodebytes(os.urandom(4096)).decode("ascii").replace("\n", "\r\n")
    put("encrypted body (S/MIME)", _raw(_headers(ctx, "Encrypted", message_id=f"smime-{uuid.uuid4().hex[:8]}@seed.test")
                                        + ["Content-Transfer-Encoding: base64", 'Content-Disposition: attachment; filename="smime.p7m"'],
                                        enc, ctype='application/pkcs7-mime; smime-type=enveloped-data; name="smime.p7m"'))
    put("just under 5 MB", _with_attachment(ctx, "4.9 MB message", int(4.9 * 1024 * 1024), "u5"))
    put("over 5 MB", _with_attachment(ctx, "6 MB message", 6 * 1024 * 1024, "o5"))

    ids = [v for v in (ctx.items or {}).values() if isinstance(v, str)]
    links = [("own file (document link)", f"https://docs.google.com/document/d/{ids[0]}/edit") if ids else None,
             ("own file (open?id=)", f"https://drive.google.com/open?id={ids[-1]}") if ids else None,
             ("a folder", f"https://drive.google.com/drive/folders/{ids[len(ids) // 2]}") if ids else None,
             ("a file that was deleted", "https://docs.google.com/document/d/1" + "x" * 43 + "/edit"),
             ("a file of a user outside the run", "https://drive.google.com/file/d/1" + "y" * 43 + "/view")]
    for label, url in [x for x in links if x]:
        put(f"link mail: {label}", _raw(_headers(ctx, f"Link: {label}", message_id=f"lnk-{uuid.uuid4().hex[:8]}@seed.test"),
                                        f"<p>See <a href=\"{url}\">{url}</a></p>", ctype="text/html; charset=utf-8"))
    put("link mail: in a quoted reply", _raw(_headers(ctx, "Re: link", message_id=f"q-{uuid.uuid4().hex[:8]}@seed.test"),
                                             f"ok\r\n\r\n> see {links[0][1] if ids else 'https://drive.google.com/open?id=none'}"))
    return {"made": made, "notes": notes}


@fixture("mail_threads")
def mail_threads(ctx: Ctx) -> dict:
    """A three-message conversation: Gmail threads an inserted message only with the first one's threadId,
    a matching Subject and In-Reply-To/References -- the seeder's own mail never did, so 11,554 messages held
    no conversation (coverage audit, 18 Nov 2026)."""
    subject = "Ward budget review"
    if _made(ctx, "gmail", f'subject:"{subject}"'):
        return {"made": 0, "notes": ["already made"]}
    made, notes, thread, ids = 0, [], None, []
    for i, body in enumerate(("Draft budget attached for review.", "Two lines need a second look.", "Fixed, thanks.")):
        mid = f"thread-{i}-{uuid.uuid4().hex[:8]}@seed.test"
        h = _headers(ctx, subject if i == 0 else f"Re: {subject}", message_id=mid, days_ago=30 - i)
        if ids:
            h += [f"In-Reply-To: <{ids[-1]}>", "References: " + " ".join(f"<{x}>" for x in ids)]
        payload = {"raw": base64.urlsafe_b64encode(_raw(h, body)).decode(), "labelIds": ["INBOX"]}
        if thread:
            payload["threadId"] = thread
        try:
            got = ctx.retry(lambda p=payload: ctx.gmail.users().messages().insert(
                userId="me", body=p, internalDateSource="dateHeader").execute())()
            thread, made = thread or got.get("threadId"), made + 1
            ids.append(mid)
        except Exception as exc:      # noqa: BLE001
            notes.append(f"message {i}: {str(exc)[:120]}")
    return {"made": made, "notes": notes}


# ---- calendar shapes ---------------------------------------------------------------------------------------
@fixture("calendar_shapes")
def calendar_shapes(ctx: Ctx) -> dict:
    """Every event type and layout a migration has to carry: all-day, Asia/Kathmandu, a weekly series with a
    moved instance, out-of-office, focus time, working location, a Meet link, guests, calendar sharing. No
    update is ever sent: sendUpdates=none, and guests are this run's own seed.test users."""
    if _made(ctx, "calendar", "Meeting in Kathmandu time"):
        return {"made": 0, "notes": ["already made"]}
    made, notes = 0, []
    cal = ctx.cal
    now = datetime.now(timezone.utc).replace(microsecond=0)

    def day(n: int) -> str:
        return (now + timedelta(days=n)).strftime("%Y-%m-%d")

    def at(n: int, hour: int) -> str:
        return (now + timedelta(days=n)).replace(hour=hour, minute=0, second=0).strftime("%Y-%m-%dT%H:%M:%S+00:00")

    def insert(label: str, body: dict, **kw):
        nonlocal made
        try:
            ev = ctx.retry(lambda: cal.events().insert(calendarId="primary", body=body, sendUpdates="none", **kw).execute())()
            made += 1
            return ev
        except Exception as exc:      # noqa: BLE001
            notes.append(f"{label}: {str(exc)[:140]}")
            return None

    insert("all-day", {"summary": "All-day event", "start": {"date": day(3)}, "end": {"date": day(4)}})
    insert("Asia/Kathmandu", {"summary": "Meeting in Kathmandu time", "start": {"dateTime": "2026-12-01T10:00:00", "timeZone": "Asia/Kathmandu"},
                              "end": {"dateTime": "2026-12-01T11:00:00", "timeZone": "Asia/Kathmandu"}})
    series = insert("weekly series", {"summary": "Weekly sync", "start": {"dateTime": at(2, 9), "timeZone": "UTC"},
                                      "end": {"dateTime": at(2, 10), "timeZone": "UTC"}, "recurrence": ["RRULE:FREQ=WEEKLY;COUNT=6"]})
    if series:
        try:
            inst = ctx.retry(lambda: cal.events().instances(calendarId="primary", eventId=series["id"], maxResults=4).execute())()["items"]
            moved = dict(inst[2])
            moved["start"] = {"dateTime": (datetime.fromisoformat(moved["start"]["dateTime"]) + timedelta(days=1)).isoformat()}
            moved["end"] = {"dateTime": (datetime.fromisoformat(moved["end"]["dateTime"]) + timedelta(days=1)).isoformat()}
            ctx.retry(lambda: cal.events().update(calendarId="primary", eventId=moved["id"], body=moved, sendUpdates="none").execute())()
            made += 1
        except Exception as exc:      # noqa: BLE001
            notes.append(f"moved instance: {str(exc)[:140]}")
    insert("out of office", {"eventType": "outOfOffice", "summary": "Out of office", "start": {"dateTime": at(10, 0)}, "end": {"dateTime": at(11, 0)},
                             "outOfOfficeProperties": {"autoDeclineMode": "declineNone", "declineMessage": "Away"}})
    insert("focus time", {"eventType": "focusTime", "summary": "Focus time", "start": {"dateTime": at(4, 13)}, "end": {"dateTime": at(4, 15)},
                          "focusTimeProperties": {"autoDeclineMode": "declineNone", "chatStatus": "doNotDisturb"}})
    insert("working location", {"eventType": "workingLocation", "summary": "Home", "start": {"date": day(5)}, "end": {"date": day(6)},
                                "visibility": "public", "transparency": "transparent",
                                "workingLocationProperties": {"type": "homeOffice", "homeOffice": {}}})
    insert("Meet link", {"summary": "Meeting with a Meet link", "start": {"dateTime": at(6, 11), "timeZone": "UTC"}, "end": {"dateTime": at(6, 12), "timeZone": "UTC"},
                         "conferenceData": {"createRequest": {"requestId": uuid.uuid4().hex, "conferenceSolutionKey": {"type": "hangoutsMeet"}}}},
           conferenceDataVersion=1)
    if ctx.peers:
        insert("guests", {"summary": "Meeting with guests", "start": {"dateTime": at(7, 14), "timeZone": "UTC"}, "end": {"dateTime": at(7, 15), "timeZone": "UTC"},
                          "attendees": [{"email": p} for p in ctx.peers[:3]], "guestsCanModify": False})
        try:
            ctx.retry(lambda: cal.acl().insert(calendarId="primary", sendNotifications=False,
                                               body={"role": "reader", "scope": {"type": "user", "value": ctx.peers[0]}}).execute())()
            made += 1
        except Exception as exc:      # noqa: BLE001
            notes.append(f"calendar sharing: {str(exc)[:140]}")
    return {"made": made, "notes": notes}


# ---- tasks -------------------------------------------------------------------------------------------------
@fixture("tasks_shapes")
def tasks_shapes(ctx: Ctx) -> dict:
    """Task shapes the engine has met: an empty title (Google accepts one), a subtask, notes and a due date, a
    completed task, and an empty list."""
    made, notes = 0, []
    try:
        _, tasks = ctx.build_people_tasks()
        if any(x.get("title") == "fableplan2 shapes" for x in ctx.retry(
                lambda: tasks.tasklists().list(maxResults=100).execute())().get("items", [])):
            return {"made": 0, "notes": ["already made"]}
        lst = ctx.retry(lambda: tasks.tasklists().insert(body={"title": "fableplan2 shapes"}).execute())()
        ctx.retry(lambda: tasks.tasklists().insert(body={"title": "fableplan2 empty list"}).execute())()
        made += 2
        parent = ctx.retry(lambda: tasks.tasks().insert(tasklist=lst["id"], body={"title": "Parent task", "notes": "with notes",
                                                                                   "due": datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00.000Z")}).execute())()
        for body, extra in (({"title": ""}, {}), ({"title": "Subtask"}, {"parent": parent["id"]}),
                            ({"title": "Done", "status": "completed"}, {})):
            ctx.retry(lambda b=body, e=extra: tasks.tasks().insert(tasklist=lst["id"], body=b, **e).execute())()
            made += 1
        made += 1
    except Exception as exc:      # noqa: BLE001
        notes.append(f"{type(exc).__name__}: {str(exc)[:140]}")
    return {"made": made, "notes": notes}


# ---- files only a person can move, and a malware test file -------------------------------------------------
@fixture("eicar")
def eicar(ctx: Ctx) -> dict:
    """The industry-standard EICAR antivirus test string, which Google's scanner flags as malware: the file a
    migration must name and copy with acknowledgement, not lose. Built from two parts so this source is not
    itself flagged by a scanner."""
    from googleapiclient.http import MediaIoBaseUpload
    import io
    if _made(ctx, "drive", "eicar-test.txt"):
        return {"made": 0, "notes": ["already made"]}
    test = "X5O!P%@AP[4\\PZX54(P^)7CC)7}$" + "EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
    try:
        ctx.retry(lambda: ctx.drive.files().create(
            body={"name": "eicar-test.txt"}, media_body=MediaIoBaseUpload(io.BytesIO(test.encode()), mimetype="text/plain"),
            fields="id").execute())()
        return {"made": 1, "notes": []}
    except Exception as exc:      # noqa: BLE001
        return {"made": 0, "notes": [f"Google refused the test file: {str(exc)[:140]}"]}


@fixture("apps_script")
def apps_script(ctx: Ctx) -> dict:
    """A standalone Apps Script project, made through Drive's import format (no Apps Script API scope needed).
    The one Drive type a server-side copy cannot take: it goes by download and upload. One per user."""
    from googleapiclient.http import MediaIoBaseUpload
    import io
    q = "mimeType='application/vnd.google-apps.script' and trashed=false"
    if ctx.retry(lambda: ctx.drive.files().list(q=q, pageSize=1, fields="files(id)").execute())().get("files"):
        return {"made": 0, "notes": ["already has one"]}
    project = {"files": [
        {"name": "Code", "type": "server_js", "source": "function wardReport() {\n  return 'ward report';\n}\n"},
        {"name": "appsscript", "type": "json",
         "source": json.dumps({"timeZone": "Asia/Kathmandu", "exceptionLogging": "STACKDRIVER", "runtimeVersion": "V8"})}]}
    ctx.retry(lambda: ctx.drive.files().create(
        body={"name": "Ward report script", "mimeType": "application/vnd.google-apps.script"}, fields="id",
        media_body=MediaIoBaseUpload(io.BytesIO(json.dumps(project).encode()),
                                     mimetype="application/vnd.google-apps.script+json")).execute())()
    return {"made": 1, "notes": []}


PERSON_ONLY = {"a Google Site": "application/vnd.google-apps.site", "a My Map": "application/vnd.google-apps.map",
               "a Google Vids file": "application/vnd.google-apps.vid"}


@fixture("person_only_check")
def person_only_check(ctx: Ctx) -> dict:
    """No API can create a Site, a My Map or a Vids file: a person makes them once in this user's account. This
    only counts them, and names what is still missing."""
    made, notes = 0, []
    for label, mime in PERSON_ONLY.items():
        try:
            n = len(ctx.retry(lambda m=mime: ctx.drive.files().list(q=f"mimeType='{m}' and trashed=false", pageSize=5,
                                                                    fields="files(id)").execute())().get("files", []))
        except Exception as exc:      # noqa: BLE001
            notes.append(f"{label}: could not look ({str(exc)[:100]})")
            continue
        made += n
        if not n:
            notes.append(f"missing {label}: make one by hand in {ctx.user}")
    return {"made": made, "notes": notes}


# ---- bulk volume -------------------------------------------------------------------------------------------
@fixture("bulk")
def bulk(ctx: Ctx) -> dict:
    """Paging and pacing: params bulk_messages small messages and bulk_files small files."""
    from googleapiclient.http import MediaIoBaseUpload
    import io
    if _made(ctx, "drive", "bulk-00000.txt"):
        return {"made": 0, "notes": ["already made"]}
    s, made, notes = ctx.seed, 0, []
    n_msg, n_files = int(ctx.params.get("bulk_messages", 0)), int(ctx.params.get("bulk_files", 0))
    workers = int(ctx.params.get("workers", 8))
    lock = __import__("threading").Lock()

    def one_message(i):
        nonlocal made
        try:
            s.insert_message(ctx.gmail, ctx.retry, _raw(_headers(ctx, f"Bulk message {i}", message_id=f"bulk-{i}-{uuid.uuid4().hex[:6]}@seed.test",
                                                                   days_ago=i % 700 + 1), f"bulk body {i}"), ["INBOX"])
            with lock:
                made += 1
        except Exception as exc:      # noqa: BLE001
            with lock:
                notes.append(f"message {i}: {str(exc)[:80]}") if len(notes) < 5 else None

    def one_file(i):
        nonlocal made
        try:
            ctx.retry(lambda: ctx.drive.files().create(body={"name": f"bulk-{i:05d}.txt"}, media_body=MediaIoBaseUpload(
                io.BytesIO(f"bulk file {i}\n".encode()), mimetype="text/plain"), fields="id").execute())()
            with lock:
                made += 1
        except Exception as exc:      # noqa: BLE001
            with lock:
                notes.append(f"file {i}: {str(exc)[:80]}") if len(notes) < 5 else None
    s._run_parallel(list(range(n_msg)), one_message, workers)
    s._run_parallel(list(range(n_files)), one_file, max(1, workers // 2))
    return {"made": made, "notes": notes}

# ---- sharing, mail settings and calendar extras (P2.5: a seeded instance of every scope row) ---------------
@fixture("sharing_extras")
def sharing_extras(ctx: Ctx) -> dict:
    """c02's grants the corpus does not make: one that expires, one to a group, one a file inherits from its
    folder. Idempotent: a folder of this name means it ran."""
    from googleapiclient.http import MediaIoBaseUpload
    import io
    d, made, notes = ctx.drive, 0, []
    name = "fableplan2 sharing extras"
    if ctx.retry(lambda: d.files().list(q=f"name='{name}' and trashed=false and 'me' in owners",
                                        fields="files(id)").execute())().get("files"):
        return {"made": 0, "notes": ["already made"]}
    peer = (ctx.peers or [None])[0]
    folder = ctx.retry(lambda: d.files().create(body={"name": name, "mimeType": "application/vnd.google-apps.folder"},
                                                fields="id").execute())()["id"]
    inner = ctx.retry(lambda: d.files().create(body={"name": "inherits the folder's grant.txt", "parents": [folder]},
                                               media_body=MediaIoBaseUpload(io.BytesIO(b"inherited\n"), mimetype="text/plain"),
                                               fields="id").execute())()["id"]
    made += 2
    domain = ctx.user.split("@")[1]
    grants = []
    if peer:
        grants.append(("inherited", folder, {"type": "user", "role": "writer", "emailAddress": peer}))
        until = (datetime.now(timezone.utc) + timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        grants.append(("expiring", inner, {"type": "user", "role": "reader", "emailAddress": ctx.peers[-1],
                                           "expirationTime": until}))
    grants.append(("group", inner, {"type": "group", "role": "reader",
                                    "emailAddress": f"{getattr(ctx.seed, 'GENERATED_PREFIX', '')}all-staff@{domain}"}))
    for label, fid, body in grants:
        try:
            ctx.retry(lambda f=fid, b=body: d.permissions().create(fileId=f, body=b, sendNotificationEmail=False,
                                                                   fields="id").execute())()
            made += 1
        except Exception as exc:      # noqa: BLE001
            notes.append(f"{label} grant: {str(exc)[:140]}")
    return {"made": made, "notes": notes}


@fixture("mail_settings")
def mail_settings(ctx: Ctx) -> dict:
    """c05's filters and signature (vacation, IMAP, POP, forwarding and delegates are the seeder's own).
    Filters and sendAs need gmail.settings.basic, which the corpus client does not hold: a 403 for a missing
    scope reads as transient to the retry ladder and spent 7 minutes (18 Nov 2026)."""
    g, made, notes = ctx.seed.build_gmail_settings(ctx.settings, ctx.user), 0, []
    try:
        if not ctx.retry(lambda: g.users().settings().filters().list(userId="me").execute())().get("filter"):
            lab = ctx.gmail.users().labels()
            labels = ctx.retry(lambda: lab.list(userId="me").execute())().get("labels", [])
            lid = next((x["id"] for x in labels if x["name"] == "Filtered"), None) or ctx.retry(
                lambda: lab.create(userId="me", body={"name": "Filtered"}).execute())()["id"]
            for crit in ({"from": (ctx.peers or [ctx.external])[0]}, {"subject": "invoice", "hasAttachment": True}):
                ctx.retry(lambda c=crit: g.users().settings().filters().create(
                    userId="me", body={"criteria": c, "action": {"addLabelIds": [lid]}}).execute())()
                made += 1
    except Exception as exc:      # noqa: BLE001
        notes.append(f"filters: {str(exc)[:140]}")
    try:
        ctx.retry(lambda: g.users().settings().sendAs().patch(
            userId="me", sendAsEmail=ctx.user,
            body={"signature": "<b>Ward office</b><br>Saraf Global Exim · वडा कार्यालय"}).execute())()
        made += 1
    except Exception as exc:      # noqa: BLE001
        notes.append(f"signature: {str(exc)[:140]}")
    return {"made": made, "notes": notes}


ROOM_SCOPE = "https://www.googleapis.com/auth/admin.directory.resource.calendar"


def _room(ctx: Ctx) -> str | None:
    """The tenant's first meeting room's address, made once if there is none (needs ROOM_SCOPE on the seed key)."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    admin = ctx.settings.source_admin
    creds = service_account.Credentials.from_service_account_file(
        ctx.seed._resolve_key_path(ctx.settings), scopes=[ROOM_SCOPE]).with_subject(admin)
    res = build("admin", "directory_v1", credentials=creds, cache_discovery=False).resources().calendars()
    have = ctx.retry(lambda: res.list(customer="my_customer", maxResults=10).execute())().get("items", [])
    if not have:
        have = [ctx.retry(lambda: res.insert(customer="my_customer", body={
            "resourceId": "fableplan2-room-1", "resourceName": "Ward Hall", "resourceType": "Conference room",
            "capacity": 12}).execute())()]
    return have[0].get("resourceEmail")


@fixture("calendar_extras")
def calendar_extras(ctx: Ctx) -> dict:
    """c06's event shapes calendar_shapes does not make: third-party extendedProperties, a Drive attachment, a
    booked room, and a colleague's calendar followed. Idempotent by the extras event's summary."""
    from googleapiclient.http import MediaIoBaseUpload
    import io
    cal, made, notes = ctx.cal, 0, []
    now = datetime.now(timezone.utc).replace(microsecond=0)

    def at(days: int, hour: int) -> dict:
        return {"dateTime": (now + timedelta(days=days)).replace(hour=hour, minute=0).strftime("%Y-%m-%dT%H:%M:%S+00:00")}
    if ctx.retry(lambda: cal.events().list(calendarId="primary", q="fableplan2 extras", maxResults=1).execute())().get("items"):
        return {"made": 0, "notes": ["already made"]}

    def insert(label, body, **kw):
        nonlocal made
        try:
            ctx.retry(lambda: cal.events().insert(calendarId="primary", body=body, sendUpdates="none", **kw).execute())()
            made += 1
        except Exception as exc:      # noqa: BLE001
            notes.append(f"{label}: {str(exc)[:140]}")
    insert("extendedProperties", {"summary": "fableplan2 extras: CRM meeting", "start": at(8, 9), "end": at(8, 10),
                                  "extendedProperties": {"private": {"crmId": "ACC-1042"}, "shared": {"ticket": "T-77"}}})
    try:
        f = ctx.retry(lambda: ctx.drive.files().create(body={"name": "Agenda for the attached meeting.txt"}, fields="id,webViewLink",
                                                       media_body=MediaIoBaseUpload(io.BytesIO(b"agenda\n"), mimetype="text/plain")).execute())()
        insert("Drive attachment", {"summary": "fableplan2 extras: meeting with an agenda", "start": at(9, 9), "end": at(9, 10),
                                    "attachments": [{"fileUrl": f["webViewLink"], "title": "Agenda"}]}, supportsAttachments=True)
    except Exception as exc:      # noqa: BLE001
        notes.append(f"Drive attachment: {str(exc)[:140]}")
    try:
        room = _room(ctx)
        insert("room", {"summary": "fableplan2 extras: in Ward Hall", "start": at(10, 9), "end": at(10, 10),
                        "attendees": [{"email": room, "resource": True}]})
    except Exception as exc:      # noqa: BLE001
        notes.append(f"room: {str(exc)[:140]}")
    if ctx.peers:
        try:
            ctx.retry(lambda: cal.calendarList().insert(body={"id": ctx.peers[0]}).execute())()
            made += 1
        except Exception as exc:      # noqa: BLE001
            notes.append(f"followed calendar: {str(exc)[:140]}")
    return {"made": made, "notes": notes}
