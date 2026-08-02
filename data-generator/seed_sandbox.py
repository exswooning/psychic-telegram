"""
tools/seed_sandbox.py
=====================
Seeds a **sandbox** source tenant with a realistic five-user organisation:
department trees, project folders, an archive, personal folders, a cross-user
sharing graph, mail between the users, and shared calendar meetings.

Then tears it all down again, so the rehearsal is repeatable.

The sharing graph is the point
------------------------------
Every user owns their own department and project and shares outward. So the five
users collectively *see* far more than they collectively *own*, and the union of
what they own equals the corpus exactly once. With `OWNED_ONLY=true` (the
default), a correct migration reproduces that union — no more.

    total files across 5 target users == total files OWNED across 5 source users

If the target ends up larger, the engine is duplicating shared-in files once per
recipient, which on a real tenant means paying to store the same deck four times
and confusing everyone about which copy is authoritative.
`tools/rehearsal.py` asserts this equality.

Credentials — read this before running
--------------------------------------
The production source service account is **read-only by design** (see
`config.SOURCE_SCOPES`). Seeding writes, so it needs a different grant. That
friction is deliberate: it makes seeding a production tenant structurally
impossible.

Set `SEED_SA_KEY` to a service account whose client ID has been granted the
write scopes below **in the sandbox source tenant only**.

Safety rails (all three required)
---------------------------------
  1. `--confirm-domain` must exactly match `SOURCE_DOMAIN`
  2. `SANDBOX_MODE=true` must be set
  3. The domain must not appear in `PROTECTED_DOMAINS`

Usage
-----
    export SANDBOX_MODE=true PROTECTED_DOMAINS=yourcompany.com
    python tools/seed_sandbox.py --confirm-domain sandbox-src.example \\
        --scale medium --external-email you@gmail.com
    python tools/seed_sandbox.py --confirm-domain sandbox-src.example --reset
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures as futures
import io
import json
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Settings  # noqa: E402
from resilience import retry_on_google_error  # noqa: E402
from corpus import ORG, SCALES, CorpusBuilder  # noqa: E402

SEED_SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/gmail.insert",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/chat.spaces",
    "https://www.googleapis.com/auth/chat.messages",
]


# ======================================================================
# Safety
# ======================================================================
def assert_sandbox(settings: Settings, confirm_domain: str) -> None:
    protected = {
        d.strip().lower()
        for d in os.getenv("PROTECTED_DOMAINS", "").split(",") if d.strip()
    }
    domain = settings.source_domain.lower()

    if os.getenv("SANDBOX_MODE", "").lower() != "true":
        sys.exit("REFUSING: set SANDBOX_MODE=true to run destructive seeding.")
    if confirm_domain.lower() != domain:
        sys.exit(
            f"REFUSING: --confirm-domain '{confirm_domain}' does not match "
            f"SOURCE_DOMAIN '{settings.source_domain}'."
        )
    if domain in protected:
        sys.exit(f"REFUSING: {domain} is listed in PROTECTED_DOMAINS.")
    print(f"Sandbox guard passed for {domain}.")


# ======================================================================
# Media + retry helpers (module level so tests can substitute them)
# ======================================================================
def _media(data: bytes, mimetype: str):
    from googleapiclient.http import MediaIoBaseUpload

    return MediaIoBaseUpload(io.BytesIO(data), mimetype=mimetype,
                             resumable=len(data) > 5 * 1024 * 1024)


def _retry_factory(settings: Settings):
    def wrap(fn):
        return retry_on_google_error(
            max_retries=settings.max_retries,
            base_delay=settings.base_backoff,
            max_delay=settings.max_backoff,
        )(fn)

    return wrap


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


# ======================================================================
# Gmail — mail between the five users
# ======================================================================
def _rfc822(subject: str, sender: str, to: str, days_ago: int,
            body: str = "", msg_id: str = "", in_reply_to: str = "",
            cc: str = "", attachment_kb: int = 0) -> bytes:
    date = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime(
        "%a, %d %b %Y %H:%M:%S +0000"
    )
    headers = [
        f"Message-ID: <{msg_id or abs(hash(subject + sender))}@seed.test>",
        f"From: {sender}", f"To: {to}", f"Date: {date}",
        f"Subject: {subject}", "MIME-Version: 1.0",
    ]
    if cc:
        headers.append(f"Cc: {cc}")
    if in_reply_to:
        headers += [f"In-Reply-To: <{in_reply_to}@seed.test>",
                    f"References: <{in_reply_to}@seed.test>"]

    body = body or "Recorded for audit. See the linked document for detail."
    if attachment_kb:
        b = "----seedboundary"
        blob = base64.b64encode(os.urandom(attachment_kb * 1024)).decode()
        headers.append(f'Content-Type: multipart/mixed; boundary="{b}"')
        parts = [f"--{b}", "Content-Type: text/plain; charset=UTF-8", "", body,
                 f"--{b}", "Content-Type: application/octet-stream",
                 'Content-Disposition: attachment; filename="payload.bin"',
                 "Content-Transfer-Encoding: base64", "", blob, f"--{b}--", ""]
        return ("\r\n".join(headers + [""] + parts)).encode()

    headers.append("Content-Type: text/plain; charset=UTF-8")
    return ("\r\n".join(headers + ["", body, ""])).encode()


SUBJECTS = [
    "Q{q} budget review", "Deploy window for {p}", "Vendor contract — {a}",
    "Headcount plan FY{y}", "Incident postmortem {n}", "Offsite logistics",
    "Renewal for {a}", "Design review: {p}", "Policy update — expenses",
    "Weekly status {p}", "Invoice query {a}", "Interview debrief",
]


def seed_gmail(gmail, settings: Settings, user: str, peers: list[str],
               external: str, count: int) -> dict:
    """Seed a mailbox with mail from the other four users, in every state."""
    retry = _retry_factory(settings)
    rng = random.Random(hash(user) & 0xFFFF)
    m = {"messages": 0, "unread": 0, "starred": 0, "in_spam": 0, "in_trash": 0,
         "with_attachment": 0, "labels": [], "thread_size": 3}

    def make_label(name):
        return retry(lambda: gmail.users().labels().create(
            userId="me",
            body={"name": name, "labelListVisibility": "labelShow",
                  "messageListVisibility": "show"},
        ).execute())()

    def insert(raw: bytes, labels: list[str]):
        return retry(lambda: gmail.users().messages().insert(
            userId="me",
            body={"raw": base64.urlsafe_b64encode(raw).decode(),
                  "labelIds": labels},
            internalDateSource="dateHeader",
        ).execute())()

    label_ids = {}
    for name in ["Clients", "Clients/Acme", "Clients/Acme/2024",
                 "Projects", "Projects/Apollo", "Archive", "Receipts"]:
        try:
            label_ids[name] = make_label(name)["id"]
            m["labels"].append(name)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! label {name}: {exc}")

    user_labels = [v for k, v in label_ids.items() if "/" in k]

    for i in range(count):
        sender = rng.choice(peers + [external])
        subj = rng.choice(SUBJECTS).format(
            q=rng.randint(1, 4), y=2023 + rng.randint(0, 2),
            p=rng.choice(["Apollo", "Borealis", "Cygnus", "Draco"]),
            a=rng.choice(["Acme Corp", "Globex", "Initech", "Umbrella"]),
            n=f"{rng.randint(100, 999)}",
        )
        labels = ["INBOX"]
        r = rng.random()
        if r < 0.30:
            labels.append("UNREAD")
        if r > 0.88:
            labels.append("STARRED")
        if rng.random() < 0.25 and user_labels:
            labels.append(rng.choice(user_labels))
        if rng.random() < 0.04:
            labels = ["SPAM"]
        elif rng.random() < 0.04:
            labels = ["TRASH"]
        elif rng.random() < 0.12:
            labels = ["SENT"]

        att = 2048 if rng.random() < 0.05 else 0
        raw = _rfc822(subj, sender, user, rng.randint(1, 2000),
                      cc=rng.choice(peers) if rng.random() < 0.3 else "",
                      attachment_kb=att)
        try:
            insert(raw, labels)
            m["messages"] += 1
            m["unread"] += "UNREAD" in labels
            m["starred"] += "STARRED" in labels
            m["in_spam"] += "SPAM" in labels
            m["in_trash"] += "TRASH" in labels
            m["with_attachment"] += bool(att)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! message: {exc}")

    # A deterministic three-message thread, so threading can be checked by hand.
    root_id = f"thread-root-{user.split('@')[0]}"
    for i, (subj, sender, reply_to) in enumerate([
        ("Q2 numbers", peers[0], ""),
        ("Re: Q2 numbers", user, root_id),
        ("Re: Q2 numbers", peers[0], root_id),
    ]):
        raw = _rfc822(subj, sender, user, 60 - i,
                      msg_id=root_id if i == 0 else f"{root_id}-{i}",
                      in_reply_to=reply_to)
        try:
            insert(raw, ["INBOX"])
            m["messages"] += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ! thread message {i}: {exc}")

    return m


# ======================================================================
# Gmail — drafts
# ======================================================================
def seed_drafts(gmail, settings: Settings, user: str, peers: list[str],
                count: int = 4) -> dict:
    """
    Unsent drafts.

    Worth seeding because drafts live outside the ordinary message list --
    a migration that copies every message can still silently drop everything
    a user had half-written, and nobody notices until they go looking for it.
    """
    retry = _retry_factory(settings)
    rng = random.Random((hash(user) & 0xFFFF) + 7)
    m = {"drafts": 0}
    subjects = ["Re: budget question (WIP)", "Draft — team update",
                "Notes for Monday", "Reply to vendor — needs review",
                "Half-finished handover doc"]
    for i in range(count):
        raw = _rfc822(rng.choice(subjects), user, rng.choice(peers),
                      rng.randint(1, 400),
                      body="Still drafting this. Not sent yet.\n")
        try:
            retry(lambda r=raw: gmail.users().drafts().create(
                userId="me",
                body={"message": {"raw": base64.urlsafe_b64encode(r).decode()}},
            ).execute())()
            m["drafts"] += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ! draft {i}: {exc}")
    return m


# ======================================================================
# Google Chat — SPACE-type rooms for the migrator to import
# ======================================================================
# Uses only 'SPACE' (group) rooms: Direct Messages are defined by their two
# participants rather than a display name, and recreating one as a named
# space silently changes what it is, so chat_engine.py deliberately skips
# them. Each seeded space is posted to as the impersonated user -- a single
# author, but enough to exercise the import-space path end to end.
#
# Needs chat.spaces/chat.messages on the source service account, plus the
# Chat service switched on for the org. Seeding is best-effort: an org that
# has not enabled Chat fails the first spaces.create, and seeding must not
# take the rest of the corpus down with it, so the failure is surfaced as a
# printed note while Drive/Gmail/Calendar continue.
CHAT_SEED_NAMES = ("team", "standup")


def _chat_names(local: str) -> list[str]:
    return [f"{local} {n}" for n in CHAT_SEED_NAMES]


def seed_chat(chat, settings: Settings, user: str, peers: list[str],
              external: str, local: str) -> dict:
    retry = _retry_factory(settings)
    m = {"spaces": 0, "messages": 0, "note": ""}
    lines = [
        "Morning — where did that land?",
        "Shipped it. Track is on the keyclients note.",
        "Raising this with the platform team today.",
        f"Can we pull in {peers[0] if peers else 'the team'} on Monday?",
        "Closing. Thanks everyone.",
        f"Tagging {external} on the contract.",
        "Reviewed at standup — looks good to go.",
    ]
    try:
        for name in _chat_names(local):
            created = retry(lambda n=name: chat.spaces().create(
                body={"spaceType": "SPACE", "displayName": n}
            ).execute())()
            space = created["name"]
            m["spaces"] += 1
            for text in lines[:5]:
                body = {"text": text}
                retry(lambda s=space, b=body: chat.spaces().messages()
                      .create(parent=s, body=b).execute())()
                m["messages"] += 1
    except Exception as exc:  # noqa: BLE001
        m["note"] = f"chat failed (Chat switched on? scopes granted?): {exc}"
        print(f"  ! chat for {user}: {exc}")
    return m


def reset_chat(chat, settings: Settings, local: str) -> int:
    """Delete only the SPACE rooms this seeder created for this user.

    Matched by display name (the same rule that makes reset_drive delete
    only the MIGRATION-TEST tree), so real spaces are left untouched."""
    retry = _retry_factory(settings)
    wanted = set(_chat_names(local))
    deleted = 0
    token = None
    while True:
        try:
            resp = retry(lambda t=token: chat.spaces().list(
                pageSize=100, pageToken=t).execute())()
        except Exception:  # noqa: BLE001
            return deleted
        for sp in resp.get("spaces", []):
            if (sp.get("displayName") or "") in wanted:
                try:
                    retry(lambda n=sp["name"]: chat.spaces().delete(
                        name=n).execute())()
                    deleted += 1
                except Exception:  # noqa: BLE001
                    pass
        token = resp.get("nextPageToken")
        if not token:
            return deleted


# ======================================================================
# Calendar — meetings across the org
# ======================================================================
def seed_calendar(cal, settings: Settings, user: str, peers: list[str],
                  external: str, count: int) -> dict:
    """Seed events using import (not insert), so seeding is itself silent."""
    retry = _retry_factory(settings)
    rng = random.Random(hash(user) & 0xFFFF)
    m = {"events": 0, "recurring": 0, "with_external": 0, "all_day": 0}

    def imp(body):
        return retry(lambda: cal.events().import_(
            calendarId="primary", body=body, conferenceDataVersion=0
        ).execute())()

    def window(days_ago: int, hour: int):
        d = (datetime.now(timezone.utc) - timedelta(days=days_ago)).replace(
            hour=hour, minute=0, second=0, microsecond=0
        )
        return (d.strftime("%Y-%m-%dT%H:%M:%SZ"),
                (d + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))

    titles = ["Weekly sync", "Design review", "Budget check-in", "1:1",
              "Sprint planning", "Customer call", "Retro", "All-hands prep"]

    for i in range(count):
        days = rng.randint(1, 700)
        start, end = window(days, rng.randint(8, 17))
        attendees = [{"email": p, "responseStatus": rng.choice(
            ["accepted", "tentative", "declined", "needsAction"])}
            for p in rng.sample(peers, rng.randint(1, min(3, len(peers))))]
        has_ext = rng.random() < 0.15
        if has_ext:
            attendees.append({"email": external, "responseStatus": "tentative"})

        body = {
            "iCalUID": f"seed-{user.split('@')[0]}-{i}@seed.test",
            "summary": f"{rng.choice(titles)} #{i+1}",
            "description": "Seeded by seed_sandbox.py",
            "location": f"Room {rng.randint(1, 9)}{rng.choice('ABC')}",
            "start": {"dateTime": start, "timeZone": "UTC"},
            "end": {"dateTime": end, "timeZone": "UTC"},
            "organizer": {"email": user, "self": True},
            "attendees": attendees,
            "reminders": {"useDefault": True},
        }
        if rng.random() < 0.12:
            body["recurrence"] = ["RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=12"]
            m["recurring"] += 1
        try:
            imp(body)
            m["events"] += 1
            m["with_external"] += has_ext
        except Exception as exc:  # noqa: BLE001
            print(f"  ! event {i}: {exc}")

    # One recurring series with a stable UID, for the hand-edited exception.
    start, end = window(30, 10)
    try:
        imp({
            "iCalUID": f"weekly-team-sync-{user.split('@')[0]}@seed.test",
            "summary": "Weekly team sync",
            "start": {"dateTime": start, "timeZone": "UTC"},
            "end": {"dateTime": end, "timeZone": "UTC"},
            "organizer": {"email": user, "self": True},
            "attendees": [{"email": p, "responseStatus": "accepted"}
                          for p in peers],
            "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=20"],
        })
        m["events"] += 1
        m["recurring"] += 1
    except Exception as exc:  # noqa: BLE001
        print(f"  ! weekly series: {exc}")

    day = (datetime.now(timezone.utc) - timedelta(days=45)).strftime("%Y-%m-%d")
    nxt = (datetime.now(timezone.utc) - timedelta(days=44)).strftime("%Y-%m-%d")
    try:
        imp({
            "iCalUID": f"offsite-{user.split('@')[0]}@seed.test",
            "summary": "All-day offsite",
            "start": {"date": day}, "end": {"date": nxt},
            "organizer": {"email": user, "self": True},
        })
        m["events"] += 1
        m["all_day"] += 1
    except Exception as exc:  # noqa: BLE001
        print(f"  ! all-day event: {exc}")

    return m


# ======================================================================
# Calendar — secondary calendars
# ======================================================================
def seed_secondary_calendars(cal, settings: Settings, user: str,
                             peers: list[str], count: int = 2) -> dict:
    """
    Calendars beyond 'primary'.

    Note the attendee trick: importing into a secondary calendar is refused
    unless that calendar is the organizer or an attendee ("The owner of the
    calendar must either be the organizer or an attendee"). Adding it as an
    attendee satisfies Google while leaving the real organizer intact --
    making it the organizer instead also works, but rewrites who owned the
    meeting, which is exactly the fidelity loss this corpus exists to catch.
    """
    retry = _retry_factory(settings)
    rng = random.Random((hash(user) & 0xFFFF) + 13)
    m = {"calendars": 0, "calendar_events": 0}
    names = ["Team Roadmap", "On-Call Rota", "Recruiting Pipeline",
             "Release Schedule", "Budget Reviews"]

    for i in range(count):
        name = f"{rng.choice(names)} ({user.split('@')[0]})"
        try:
            cal_id = retry(lambda n=name: cal.calendars().insert(
                body={"summary": n, "timeZone": "UTC"},
            ).execute())()["id"]
            m["calendars"] += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ! secondary calendar {i}: {exc}")
            continue

        # A sharing rule, so calendar-ACL translation has something to chew on.
        try:
            retry(lambda c=cal_id: cal.acl().insert(
                calendarId=c, sendNotifications=False,
                body={"scope": {"type": "user", "value": rng.choice(peers)},
                      "role": "reader"},
            ).execute())()
        except Exception:  # noqa: BLE001
            pass

        for j in range(3):
            days = rng.randint(1, 500)
            start, end = _cal_window(days, rng.randint(9, 16))
            try:
                retry(lambda c=cal_id, jj=j, st=start, en=end: cal.events().import_(
                    calendarId=c, conferenceDataVersion=0,
                    body={
                        "iCalUID": f"sec-{user.split('@')[0]}-{i}-{jj}@seed.test",
                        "summary": f"{name} item {jj+1}",
                        "start": {"dateTime": st, "timeZone": "UTC"},
                        "end": {"dateTime": en, "timeZone": "UTC"},
                        "organizer": {"email": user},
                        # required, see docstring
                        "attendees": [{"email": c, "responseStatus": "accepted"}],
                    },
                ).execute())()
                m["calendar_events"] += 1
            except Exception as exc:  # noqa: BLE001
                print(f"  ! secondary event {i}/{j}: {exc}")
    return m


def _cal_window(days_ago: int, hour: int):
    d = (datetime.now(timezone.utc) - timedelta(days=days_ago)).replace(
        hour=hour, minute=0, second=0, microsecond=0)
    return (d.strftime("%Y-%m-%dT%H:%M:%SZ"),
            (d + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))


# ======================================================================
# Reset
# ======================================================================
def reset_drive(drive, settings: Settings) -> int:
    """
    Delete the seeded corpus -- and only that.

    Deliberately NOT "everything this user owns". A tenant nominally set aside
    for testing can still hold real content (an admin account reused as a test
    user, a folder someone actually cares about), and a reset that takes it
    out is unrecoverable. Everything the seeder creates lives under a
    MIGRATION-TEST root, so removing those roots removes exactly the corpus
    and nothing else.
    """
    retry = _retry_factory(settings)
    deleted = 0
    while True:
        resp = retry(lambda: drive.files().list(
            q=("'root' in parents and 'me' in owners and trashed = false "
               "and name = 'MIGRATION-TEST'"),
            pageSize=100, fields="files(id,name)", spaces="drive",
        ).execute())()
        roots = resp.get("files", [])
        if not roots:
            break
        for f in roots:
            try:
                retry(lambda fid=f["id"]: drive.files().delete(
                    fileId=fid, supportsAllDrives=True).execute())()
                deleted += 1
            except Exception:  # noqa: BLE001
                pass
    return deleted


def reset_gmail(gmail, settings: Settings) -> int:
    """
    Trash only the mail this seeder inserted.

    Every seeded message carries a Message-ID ending in `@seed.test`, so the
    seeded set is identifiable without guessing. Real mail in the mailbox is
    left alone -- the previous behaviour (batchDelete over the entire
    mailbox) would empty an account that happened to have any.

    trash(), not delete(): permanent deletion needs the full
    https://mail.google.com/ scope, while the seeder only asks for
    gmail.modify. Gmail purges Trash after 30 days.
    """
    retry = _retry_factory(settings)
    deleted = 0
    token = None
    while True:
        resp = retry(lambda t=token: gmail.users().messages().list(
            userId="me", maxResults=500, pageToken=t,
            includeSpamTrash=True).execute())()
        msgs = resp.get("messages", [])
        for m in msgs:
            try:
                meta = retry(lambda mid=m["id"]: gmail.users().messages().get(
                    userId="me", id=mid, format="metadata",
                    metadataHeaders=["Message-ID"]).execute())()
                headers = {h["name"].lower(): h["value"]
                          for h in (meta.get("payload") or {}).get("headers", [])}
                if "@seed.test" not in headers.get("message-id", ""):
                    continue
                retry(lambda mid=m["id"]: gmail.users().messages().trash(
                    userId="me", id=mid).execute())()
                deleted += 1
            except Exception:  # noqa: BLE001
                pass
        token = resp.get("nextPageToken")
        if not token:
            return deleted


def reset_calendar(cal, settings: Settings) -> int:
    retry = _retry_factory(settings)
    deleted = 0

    # Secondary calendars first -- deleting the calendar takes its events
    # with it, and leaving them behind makes every re-seed accumulate more.
    try:
        for entry in retry(lambda: cal.calendarList().list(
                minAccessRole="owner").execute())().get("items", []):
            if entry.get("primary"):
                continue
            try:
                retry(lambda c=entry["id"]: cal.calendars().delete(
                    calendarId=c).execute())()
                deleted += 1
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    while True:
        resp = retry(lambda: cal.events().list(
            calendarId="primary", maxResults=2500, singleEvents=False
        ).execute())()
        items = resp.get("items", [])
        if not items:
            break
        for ev in items:
            # Same principle as Drive and Gmail: seeded events carry a
            # @seed.test iCalUID, so a real meeting in the calendar survives.
            if "seed.test" not in (ev.get("iCalUID") or ""):
                continue
            try:
                retry(lambda eid=ev["id"]: cal.events().delete(
                    calendarId="primary", eventId=eid, sendUpdates="none"
                ).execute())()
                deleted += 1
            except Exception:  # noqa: BLE001
                pass
        if len(items) < 2500:
            break
    return deleted


# ======================================================================
# Service construction
# ======================================================================
def _resolve_key_path(settings: Settings) -> str:
    key = os.getenv("SEED_SA_KEY", settings.source_sa_key)
    if key and not os.path.isabs(key):
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        key = os.path.abspath(os.path.join(repo_root, key))
    return key


def build_services(settings: Settings, user: str):
    """Delegated clients with WRITE scopes against the sandbox source tenant."""
    import google_auth_httplib2
    import httplib2
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    key = _resolve_key_path(settings)
    creds = service_account.Credentials.from_service_account_file(
        key, scopes=SEED_SCOPES
    ).with_subject(user)

    def svc(api, version):
        http = google_auth_httplib2.AuthorizedHttp(
            creds, http=httplib2.Http(timeout=300)
        )
        return build(api, version, http=http, cache_discovery=False)

    return svc("drive", "v3"), svc("gmail", "v1"), svc("calendar", "v3")


def build_chat(settings: Settings, user: str):
    """A delegated Chat client with the seed scopes, for the sandbox source."""
    import google_auth_httplib2
    import httplib2
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_file(
        _resolve_key_path(settings), scopes=SEED_SCOPES
    ).with_subject(user)
    http = google_auth_httplib2.AuthorizedHttp(
        creds, http=httplib2.Http(timeout=300)
    )
    return build("chat", "v1", http=http, cache_discovery=False)


# ======================================================================
# Per-user worker
# ======================================================================
def seed_one_user(settings: Settings, entry: dict, all_users: list[str],
                  external: str, scale: str, mail_count: int,
                  event_count: int, edge_cases: bool) -> dict:
    user = entry["email"]
    peers = [u for u in all_users if u != user]
    t0 = time.time()
    print(f"  [{user}] starting ({entry['dept']}, {entry['project']})")

    drive, gmail, cal = build_services(settings, user)
    chat = build_chat(settings, user)
    retry = _retry_factory(settings)

    builder = CorpusBuilder(drive, settings, user, peers, external, scale,
                            _media, retry)
    drive_m = builder.build(entry["dept"], entry["project"], edge_cases)
    gmail_m = seed_gmail(gmail, settings, user, peers, external, mail_count)
    gmail_m.update(seed_drafts(gmail, settings, user, peers))
    cal_m = seed_calendar(cal, settings, user, peers, external, event_count)
    cal_m.update(seed_secondary_calendars(cal, settings, user, peers))
    chat_m = seed_chat(chat, settings, user, peers, external,
                       user.split("@")[0])

    elapsed = round(time.time() - t0, 1)
    print(f"  [{user}] done in {elapsed}s: {drive_m['total_files']} files, "
          f"{drive_m['folders']} folders, {drive_m.get('comments', 0)} comments, "
          f"{gmail_m['messages']} messages, {gmail_m.get('drafts', 0)} drafts, "
          f"{cal_m['events']} events, "
          f"{cal_m.get('calendars', 0)} secondary calendars, "
          f"{chat_m['messages']} chat messages in {chat_m['spaces']} spaces"
          + (f" ({chat_m['note']})" if chat_m["note"] else ""))
    return {"user": user, "dept": entry["dept"], "project": entry["project"],
            "drive": drive_m, "gmail": gmail_m, "calendar": cal_m,
            "chat": chat_m, "elapsed_sec": elapsed}


def reset_one_user(settings: Settings, user: str) -> dict:
    drive, gmail, cal = build_services(settings, user)
    chat = build_chat(settings, user)
    return {
        "user": user,
        "drive": reset_drive(drive, settings),
        "gmail": reset_gmail(gmail, settings),
        "calendar": reset_calendar(cal, settings),
        "chat": reset_chat(chat, settings, user.split("@")[0]),
    }


# ======================================================================
# CLI
# ======================================================================
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Seed or reset a five-user sandbox organisation."
    )
    ap.add_argument("--confirm-domain", required=True)
    ap.add_argument("--users", help="comma-separated localparts "
                                    "(default: alice,bob,carol,dave,erin)")
    ap.add_argument("--scale", default="medium", choices=list(SCALES))
    ap.add_argument("--external-email", default="external.tester@example.com")
    ap.add_argument("--mail", type=int, help="messages per user "
                                             "(default scales with --scale)")
    ap.add_argument("--events", type=int, help="events per user")
    ap.add_argument("--workers", type=int, default=0,
                    help="0 (default) sizes the pool to this machine; "
                         "see resources.py")
    ap.add_argument("--manifest", default="sandbox_manifest.json")
    ap.add_argument("--identities-out", default="identities.csv")
    ap.add_argument("--edge-cases", default="first",
                    choices=["first", "all", "none"],
                    help="full edge-case set on user 1, on everyone, or nobody")
    ap.add_argument("--yes", action="store_true",
                    help="skip the interactive 'long run?' confirmation. Meant "
                         "for callers that already enforce their own safety "
                         "gates (the web UI requires the source domain typed "
                         "back) -- without it, a non-interactive stdin makes "
                         "input() hang or EOF, and nothing gets seeded.")
    ap.add_argument("--create-users", action="store_true",
                    help="create the test accounts first if they do not exist "
                         "(needs admin.directory.user granted to SEED_SA_KEY)")
    ap.add_argument("--reset", action="store_true", help="DELETE everything")
    args = ap.parse_args(argv)

    settings = Settings()
    assert_sandbox(settings, args.confirm_domain)

    locals_ = ([u.strip() for u in args.users.split(",")] if args.users
               else [e["local"] for e in ORG])
    entries = []
    for i, lp in enumerate(locals_):
        template = ORG[i % len(ORG)]
        entries.append({
            "local": lp, "email": f"{lp}@{settings.source_domain}",
            "dept": template["dept"], "project": template["project"],
        })
    all_users = [e["email"] for e in entries]

    # --- Optionally create the accounts first ----------------------------
    if args.create_users and not args.reset:
        import provision
        from google.oauth2 import service_account
        import google_auth_httplib2, httplib2
        from googleapiclient.discovery import build

        admin = os.getenv("SOURCE_ADMIN")
        if not admin:
            sys.exit("SOURCE_ADMIN must be set to a super admin of "
                     f"{settings.source_domain} to create accounts.")
        creds = service_account.Credentials.from_service_account_file(
            _resolve_key_path(settings),
            scopes=SEED_SCOPES + [provision.DIRECTORY_WRITE_SCOPE],
        ).with_subject(admin)
        directory = build(
            "admin", "directory_v1",
            http=google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=120)),
            cache_discovery=False,
        )
        print(f"\nEnsuring {len(all_users)} account(s) exist in "
              f"{settings.source_domain} ...")
        res = provision.ensure_users(directory, all_users)
        provision.report(res)
        if res["failed"]:
            sys.exit("Some accounts could not be created; not seeding.")
        # Freshly created accounts take a moment before delegation works.
        print("\nWaiting 20s for new accounts to become usable ...")
        time.sleep(20)

    # --- Reset -----------------------------------------------------------
    if args.reset:
        print(f"About to DELETE all Drive files, mail, events and Chat for:")
        for u in all_users:
            print(f"    {u}")
        if input(f"Type the domain to confirm: ").strip() != settings.source_domain:
            print("Aborted.")
            return 1
        with futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            for r in pool.map(lambda u: reset_one_user(settings, u), all_users):
                print(f"  {r['user']}: {r['drive']} files, {r['gmail']} "
                      f"messages, {r['calendar']} events, "
                      f"{r['chat']} chat spaces deleted")
        for f in (args.manifest,):
            if os.path.exists(f):
                os.remove(f)
        return 0

    # --- Seed ------------------------------------------------------------
    if not args.workers:
        try:
            import resources
            rec = resources.recommend()
            args.workers = rec["seed_workers"]
            print(f"Workers: {args.workers} ({rec['reason']})")
        except Exception:  # noqa: BLE001
            args.workers = 3

    cfg = SCALES[args.scale]
    mail_count = args.mail if args.mail is not None else cfg["per_leaf"] * 12
    event_count = args.events if args.events is not None else cfg["per_leaf"] * 4

    print(f"\nSeeding {len(entries)} users in {settings.source_domain} "
          f"at scale '{args.scale}'")
    print(f"  external collaborator: {args.external_email}")
    print(f"  ~{mail_count} messages and ~{event_count} events per user")

    # Rough forecast so nobody starts a 'huge' run expecting it to take a
    # minute. Drive sustains roughly 7 successful writes/sec/user in practice.
    est_files = cfg["per_leaf"] * 60 + cfg["wide"] + cfg["archive_years"] * 4 * (
        cfg["per_leaf"] // 3 or 1)
    est_calls = (est_files + mail_count + event_count) * len(entries)
    est_min = est_calls / (min(args.workers, len(entries)) * 7) / 60
    print(f"  estimated ~{est_calls:,} API writes, roughly {est_min:.0f} minute(s) "
          f"at {args.workers} parallel users\n")
    if est_min > 5 and not args.yes:
        if input("This is a long run. Continue? [y/N] ").strip().lower() != "y":
            print("Aborted.")
            return 1

    t0 = time.time()
    results = []
    with futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        jobs = {
            pool.submit(
                seed_one_user, settings, e, all_users, args.external_email,
                args.scale, mail_count, event_count,
                args.edge_cases == "all" or (args.edge_cases == "first" and i == 0),
            ): e["email"]
            for i, e in enumerate(entries)
        }
        for fut in futures.as_completed(jobs):
            try:
                results.append(fut.result())
            except Exception as exc:  # noqa: BLE001
                print(f"  ! {jobs[fut]} FAILED: {exc}")
                results.append({"user": jobs[fut], "error": str(exc)})

    ok = [r for r in results if "error" not in r]
    totals = {
        "users": len(ok),
        "owned_files": sum(r["drive"]["total_files"] for r in ok),
        "folders": sum(r["drive"]["folders"] for r in ok),
        "docs": sum(r["drive"]["docs"] for r in ok),
        "sheets": sum(r["drive"]["sheets"] for r in ok),
        "slides": sum(r["drive"]["slides"] for r in ok),
        "binaries": sum(r["drive"]["binaries"] for r in ok),
        "shortcuts": sum(r["drive"]["shortcuts"] for r in ok),
        "messages": sum(r["gmail"]["messages"] for r in ok),
        "drafts": sum(r["gmail"].get("drafts", 0) for r in ok),
        "comments": sum(r["drive"].get("comments", 0) for r in ok),
        "events": sum(r["calendar"]["events"] for r in ok),
        "secondary_calendars": sum(r["calendar"].get("calendars", 0) for r in ok),
        "chat_spaces": sum(r["chat"].get("spaces", 0) for r in ok),
        "chat_messages": sum(r["chat"].get("messages", 0) for r in ok),
        "grants_user": sum(r["drive"]["grants"]["user"] for r in ok),
        "grants_domain": sum(r["drive"]["grants"]["domain"] for r in ok),
        "grants_anyone": sum(r["drive"]["grants"]["anyone"] for r in ok),
        "grants_external": sum(r["drive"]["grants"]["external"] for r in ok),
    }
    rejected = sorted({x for r in ok for x in r["drive"]["grants_rejected"]})

    manifest = {
        "seeded_at": datetime.now(timezone.utc).isoformat(),
        "domain": settings.source_domain,
        "scale": args.scale,
        "external": args.external_email,
        "users": all_users,
        "totals": totals,
        "grants_rejected": rejected,
        "per_user": results,
        "elapsed_sec": round(time.time() - t0, 1),
    }
    with open(args.manifest, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)

    # Write the identity map so `main.py init-db` can consume it directly.
    with open(args.identities_out, "w", encoding="utf-8") as fh:
        fh.write("source_email,target_email,entity_type\n")
        for e in entries:
            fh.write(f"{e['email']},{e['local']}@{settings.target_domain},user\n")

    print(f"\n{'='*66}")
    print(f"Seeded {totals['users']} users in {manifest['elapsed_sec']}s")
    print(f"  OWNED files : {totals['owned_files']:,}  "
          f"(docs {totals['docs']:,}, sheets {totals['sheets']:,}, "
          f"slides {totals['slides']:,}, binaries {totals['binaries']:,})")
    print(f"  Folders     : {totals['folders']:,}")
    print(f"  Messages    : {totals['messages']:,}  "
          f"(+{totals['drafts']:,} drafts)")
    print(f"  Comments    : {totals['comments']:,}")
    print(f"  Events      : {totals['events']:,}  "
          f"(+{totals['secondary_calendars']:,} secondary calendars)")
    print(f"  Chat        : {totals['chat_messages']:,} messages "
          f"in {totals['chat_spaces']:,} spaces")
    print(f"  ACL grants  : {totals['grants_user']:,} user, "
          f"{totals['grants_domain']:,} domain, "
          f"{totals['grants_external']:,} external, "
          f"{totals['grants_anyone']:,} anyone")
    print(f"{'='*66}")
    print(f"Manifest   -> {args.manifest}")
    print(f"Identities -> {args.identities_out}")

    if rejected:
        print(f"\nNOTE: the tenant rejected some grants: {rejected}")
        print("That is a real finding. Check Admin Console > Drive > Sharing "
              "settings before blaming the migration for missing permissions.")

    print("\nBy hand before rehearsing:")
    print("  1. Open 'Weekly team sync' in the Calendar UI and move ONE "
          "instance (the API cannot cleanly seed a recurrence exception).")
    print("  2. Create one Google Form, to confirm it is skipped not crashed.")
    print("\nNext:")
    print(f"  python main.py init-db --identities {args.identities_out}")
    print(f"  python tools/rehearsal.py")

    # A run that seeded nobody is a failure, and it has to say so in the exit
    # code. It previously returned 0 whatever happened: five users timing out
    # for thirty minutes rendered in the web UI as a green "exit 0" beside a
    # run that had accomplished precisely nothing.
    attempted = len(entries)
    seeded = totals["users"]
    if seeded == 0:
        print(f"\nFAILED: 0 of {attempted} users seeded. Nothing was written.")
        return 1
    if seeded < attempted:
        print(f"\nPARTIAL: {seeded} of {attempted} users seeded; "
              f"{attempted - seeded} failed (see the '!' lines above).")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
