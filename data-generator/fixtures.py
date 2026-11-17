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
    return ("\r\n".join(headers + [f"MIME-Version: 1.0", f"Content-Type: {ctype}", ""]) + "\r\n" + body + "\r\n").encode("utf-8")


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


# ---- calendar shapes ---------------------------------------------------------------------------------------
@fixture("calendar_shapes")
def calendar_shapes(ctx: Ctx) -> dict:
    """Every event type and layout a migration has to carry: all-day, Asia/Kathmandu, a weekly series with a
    moved instance, out-of-office, focus time, working location, a Meet link, guests, calendar sharing. No
    update is ever sent: sendUpdates=none, and guests are this run's own seed.test users."""
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
    test = "X5O!P%@AP[4\\PZX54(P^)7CC)7}$" + "EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
    try:
        ctx.retry(lambda: ctx.drive.files().create(
            body={"name": "eicar-test.txt"}, media_body=MediaIoBaseUpload(io.BytesIO(test.encode()), mimetype="text/plain"),
            fields="id").execute())()
        return {"made": 1, "notes": []}
    except Exception as exc:      # noqa: BLE001
        return {"made": 0, "notes": [f"Google refused the test file: {str(exc)[:140]}"]}


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
