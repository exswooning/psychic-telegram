"""
tools/seed_sandbox.py
=====================
Seeds a **sandbox** source tenant with a realistic organisation: department
trees, project folders, an archive, personal folders, a cross-user sharing
graph, mail between the users, and shared calendar meetings.

By default this targets every user account the tenant actually has, read
live via the Directory API (see `discover_tenant_entries()`) -- not a fixed
5. If SOURCE_ADMIN is not set or the Directory scope has not been granted
yet, it falls back to a fixed 5-user default (`corpus.ORG`) with a printed
note explaining why, so an ordinary run never hard-fails on that alone.
`--users a,b,c` overrides with an explicit list; `--all-users` asks for the
same live discovery but fails loudly instead of falling back, for when you
want to be sure it worked.

Then tears it all down again, so the rehearsal is repeatable.

The sharing graph is the point
------------------------------
Every user owns their own department and project and shares outward. So the
users collectively *see* far more than they collectively *own*, and the union
of what they own equals the corpus exactly once. With `OWNED_ONLY=true` (the
default), a correct migration reproduces that union — no more.

    total files across N target users == total files OWNED across N source users

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

    # Create accounts and fill every licence the trial provides. Needs
    # admin.directory.user (for --create-users) and
    # admin.reports.usage.readonly (for --fit-to-licenses) granted to
    # SEED_SA_KEY's client ID in the sandbox source tenant.
    python tools/seed_sandbox.py --confirm-domain sandbox-src.example \\
        --create-users --fit-to-licenses
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

# The labels seed_gmail() creates. Reset removes exactly these -- deleting
# every user label would take labels the account's owner made themselves.
#
# Not "Archive": Gmail's labels().create() rejects that exact name outright
# with a 400 invalidArgument on every account, 100% reproducible -- observed
# live on a real tenant, not a permissions or quota issue. Gmail reserves it
# even though it is not a documented system label ID (unlike INBOX, SENT,
# TRASH, etc.), presumably because it collides with the "Archive" action's
# own internal handling. "Archived" (past tense) is accepted.
SEED_LABELS = ["Clients", "Clients/Acme", "Clients/Acme/2024",
               "Projects", "Projects/Apollo", "Archived", "Receipts"]

SEED_SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/gmail.insert",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/chat.spaces",
    "https://www.googleapis.com/auth/chat.messages",
    # build_people_tasks() builds the People and Tasks clients with this same
    # list, so without these two `seed_contacts` and `seed_tasks` fail with
    # unauthorized_client no matter what the Admin Console has authorised --
    # and both are written to swallow their own exceptions into a `note`, so
    # the run reports success and simply produces no contacts and no tasks.
    # That is precisely the shape of gap coverage_audit exists to surface.
    "https://www.googleapis.com/auth/contacts",
    "https://www.googleapis.com/auth/tasks",
]

# Not part of SEED_SCOPES on purpose. `--fit-to-licenses` is opt-in, and
# adding a scope the Admin Console has not authorised makes *every* delegated
# call fail with `unauthorized_client` (see config.py's scope comments). So
# the reports service is built with this scope only, and only when the user
# asks for licence fitting.
REPORTS_SCOPE = "https://www.googleapis.com/auth/admin.reports.usage.readonly"

# Same isolation reasoning as REPORTS_SCOPE: `--all-users` is opt-in and reads
# the Directory API only, so it is built with this scope alone rather than
# folded into SEED_SCOPES or DIRECTORY_WRITE_SCOPE.
DIRECTORY_READONLY_SCOPE = "https://www.googleapis.com/auth/admin.directory.user.readonly"

# customerUsageReports `accounts:` parameters, one (total, used) pair per
# edition. Google has kept these legacy edition names for current Google
# Workspace editions, and an org holds exactly one edition -- so only one pair
# is ever populated, and summing across all of them yields the org's numbers.
SEAT_PARAMS = [
    ("apps_total_licenses", "apps_used_licenses"),
    ("gsuite_basic_total_licenses", "gsuite_basic_used_licenses"),
    ("gsuite_enterprise_total_licenses", "gsuite_enterprise_used_licenses"),
    ("gsuite_unlimited_total_licenses", "gsuite_unlimited_used_licenses"),
]

# Localparts for the generated users that fill unused licence seats.
GENERATED_LOCALPARTS = [
    "fiona", "george", "hannah", "ivan", "jane", "kyle", "lara", "mike",
    "nina", "oliver", "priya", "quinn", "ravi", "sarah", "tom", "uma",
    "victor", "wendy", "xander", "yara", "zane",
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


# ======================================================================
# Storage top-up — filler files, purely to reach a target quota usage.
#
# Nothing about realistic document variety helps here: the office-document
# corpus above averages tens of KB per file (confirmed by measurement against
# a live tenant this session), so reaching tens of GB through that content
# alone would mean hundreds of thousands of files per user -- impractical to
# generate, upload, and later migrate within any reasonable time, and it
# would say nothing about storage limits that a much smaller number of large
# files does not already say better.
# ======================================================================
_FILLER_CHUNK_BYTES = 50 * 1024 * 1024   # 50 MB per filler file
_filler_blob_cache: bytes | None = None


def _filler_blob() -> bytes:
    """
    50 MB of random bytes, generated once and reused for every filler file
    across every user.

    Was 200 MB; a real run reproducibly hit "Redirected but the response is
    missing a Location: header" (a resumable-upload protocol error) on
    every single user's top-up pass, on a modest, likely bandwidth-limited
    VPS. A smaller resumable session spends less time in flight and is
    less exposed to whatever timeout or connection issue triggers that --
    see seed_one_user()'s comment on the connection-reuse half of this fix.

    The content has to exist to occupy storage; what it contains does not
    matter, since nothing reads it back. Generating a fresh 200 MB via
    os.urandom() per file would mean ~150 GB worth of RNG output for a
    30 GB x 5-user run, which costs real CPU time for a property (randomness)
    nothing here needs. Immutable bytes are cheap to share.
    """
    global _filler_blob_cache
    if _filler_blob_cache is None:
        _filler_blob_cache = os.urandom(_FILLER_CHUNK_BYTES)
    return _filler_blob_cache


def top_up_storage(drive, settings: Settings, user: str, target_gb: float,
                   media_fn=None) -> dict:
    """
    Adds large filler files until this user's total Workspace storage
    (Gmail + Drive + Photos, pooled -- storageQuota.usage) reaches target_gb.

    Filler lives inside a folder named exactly "MIGRATION-TEST" -- the same
    name reset_drive() already matches on -- so resetting the seeded corpus
    removes the filler too, with no separate reset path to write or maintain.

    The one caveat worth stating rather than discovering later: Gmail's
    contribution to storageQuota.usage does not update in real time (Google's
    own accounting can lag by hours), so a target computed immediately after
    seeding a large mailbox will overshoot how much filler is actually needed
    once Gmail's count catches up. Re-run with --top-up-only once it has --
    that flag skips every other seeding step and only checks and tops up
    storage, so it is safe to run repeatedly without duplicating mail, drive
    content, or anything else.
    """
    from config import FOLDER_MIME

    media_fn = media_fn or _media
    retry = _retry_factory(settings)
    m = {"filler_files": 0, "filler_bytes": 0, "usage_before_gb": 0.0,
        "usage_after_gb": 0.0, "note": ""}
    try:
        about = retry(lambda: drive.about().get(
            fields="storageQuota").execute())()
        quota = about.get("storageQuota", {})
        usage = int(quota.get("usage") or 0)
        limit = quota.get("limit")
        m["usage_before_gb"] = round(usage / 1e9, 2)

        target_bytes = int(target_gb * 1e9)
        if limit and int(limit) < target_bytes:
            # The account's own licence ceiling is lower than what was asked
            # for. Filling further would just fail partway through with
            # storageQuotaExceeded once the real limit is hit.
            target_bytes = int(limit)
            m["note"] = (f"target capped at the account's own licence limit "
                        f"({int(limit) / 1e9:.1f} GB)")

        remaining = target_bytes - usage
        if remaining <= 0:
            m["usage_after_gb"] = m["usage_before_gb"]
            return m

        root = retry(lambda: drive.files().create(
            body={"name": "MIGRATION-TEST", "mimeType": FOLDER_MIME,
                 "parents": ["root"]}, fields="id").execute())()
        folder_id = root["id"]

        # One grant each on the folder, inherited by every filler file
        # created under it -- domain-wide already covers every org member
        # (>= 10 with the tenant's real headcount), so this is not repeated
        # per file, which would multiply API calls by however many filler
        # files this run creates for no additional coverage.
        try:
            retry(lambda: drive.permissions().create(
                fileId=folder_id, sendNotificationEmail=False,
                body={"type": "domain", "role": "reader",
                     "domain": settings.source_domain,
                     "allowFileDiscovery": True}).execute())()
            retry(lambda: drive.permissions().create(
                fileId=folder_id, sendNotificationEmail=False,
                body={"type": "user", "role": "reader",
                     "emailAddress": "aryan@nestnepal.com.np"}).execute())()
        except Exception as exc:  # noqa: BLE001
            print(f"  ! could not share filler folder for {user}: {exc}")

        blob = _filler_blob()
        chunk = len(blob)
        n_full, remainder = divmod(remaining, chunk)
        i = -1
        for i in range(int(n_full)):
            retry(lambda i=i: drive.files().create(
                body={"name": f"filler-{i:04d}.bin", "parents": [folder_id]},
                media_body=media_fn(blob, "application/octet-stream"),
                fields="id").execute())()
            m["filler_files"] += 1
            m["filler_bytes"] += chunk
        if remainder > 1024 * 1024:      # skip a leftover under 1 MB
            retry(lambda: drive.files().create(
                body={"name": f"filler-{i + 1:04d}.bin", "parents": [folder_id]},
                media_body=media_fn(blob[:int(remainder)], "application/octet-stream"),
                fields="id").execute())()
            m["filler_files"] += 1
            m["filler_bytes"] += int(remainder)

        m["usage_after_gb"] = round((usage + m["filler_bytes"]) / 1e9, 2)
    except Exception as exc:  # noqa: BLE001
        m["note"] = f"storage top-up failed: {exc}"
        print(f"  ! top-up for {user}: {exc}")
    return m


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
    for name in SEED_LABELS:
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
            break
    return deleted


# ======================================================================
# Contacts / Tasks — realistic coverage for contacts_engine.py / tasks_engine.py
#
# Neither existed in the seeder before these engines did. Each is written the
# same way seed_chat() is: wrapped in a single try/except that records a note
# and returns rather than raising, because contacts.write/tasks scopes are
# typically granted on a different schedule than drive/gmail/calendar/chat --
# verified live: the SEED_SCOPES credential mints today, a separate
# contacts+tasks-only credential does not yet. One user's missing grant must
# not abort the whole seeding run for every other user.
# ======================================================================
_CONTACT_GROUP_NAMES = ("Clients", "Vendors")

# Marks every seeded contact so reset_contacts() deletes only what this
# script created -- the same discipline reset_drive()/reset_chat() already
# follow via a name match, done here through a custom People field since
# contacts have no free-text "owner" concept to match on.
_SEED_MARKER = {"key": "seed_sandbox", "value": "true"}


def seed_contacts(people, settings: Settings, user: str, peers: list[str],
                  external: str, count: int = 25) -> dict:
    retry = _retry_factory(settings)
    m = {"contacts": 0, "groups": 0, "note": ""}
    first_names = ("Priya", "Jordan", "Wei", "Fatima", "Sam", "Elena", "Kwame",
                  "Noor", "Diego", "Aisling", "Ravi", "Chloe")
    last_names = ("Nakamura", "Silva", "Okafor", "Kowalski", "Haddad",
                  "Lindqvist", "Reyes", "Achebe", "Bianchi", "Petrov")
    domains = [external, "partner-co.example", "vendor-services.example"]
    try:
        group_ids = []
        for name in _CONTACT_GROUP_NAMES:
            created = retry(lambda n=name: people.contactGroups().create(
                body={"contactGroup": {"name": n}}).execute())()
            group_ids.append(created["resourceName"])
            m["groups"] += 1

        rng = random.Random(hash(user) & 0xFFFFFFFF)
        for i in range(count):
            given, family = rng.choice(first_names), rng.choice(last_names)
            email = f"{given.lower()}.{family.lower()}{i}@{rng.choice(domains)}"
            body = {
                "names": [{"givenName": given, "familyName": family}],
                "emailAddresses": [{"value": email}],
                "phoneNumbers": [{"value": f"+1-555-{rng.randint(100,999)}-"
                                          f"{rng.randint(1000,9999)}"}],
                "organizations": [{"name": rng.choice(
                    ["Acme Logistics", "Northwind Traders", "Globex", "Initech"])}],
                "userDefined": [_SEED_MARKER],
            }
            created = retry(lambda b=body: people.people().createContact(
                body=b).execute())()
            m["contacts"] += 1
            group = group_ids[i % len(group_ids)]
            retry(lambda rn=created["resourceName"], g=group:
                 people.contactGroups().members().modify(
                     resourceName=g,
                     body={"resourceNamesToAdd": [rn]}).execute())()
    except Exception as exc:  # noqa: BLE001
        m["note"] = f"contacts failed (People API enabled? scopes granted?): {exc}"
        print(f"  ! contacts for {user}: {exc}")
    return m


def reset_contacts(people, settings: Settings) -> int:
    """Delete only contacts carrying _SEED_MARKER, and the two groups this
    seeder creates -- never "every contact this user has"."""
    retry = _retry_factory(settings)
    deleted = 0
    try:
        token = None
        while True:
            resp = retry(lambda t=token: people.people().connections().list(
                resourceName="people/me", pageSize=200, pageToken=t,
                personFields="userDefined").execute())()
            for p in resp.get("connections", []):
                marked = any(
                    d.get("key") == _SEED_MARKER["key"]
                    and d.get("value") == _SEED_MARKER["value"]
                    for d in (p.get("userDefined") or []))
                if marked:
                    try:
                        retry(lambda rn=p["resourceName"]:
                             people.people().deleteContact(
                                 resourceName=rn).execute())()
                        deleted += 1
                    except Exception:  # noqa: BLE001
                        pass
            token = resp.get("nextPageToken")
            if not token:
                break
        for name in _CONTACT_GROUP_NAMES:
            resp = retry(lambda: people.contactGroups().list(
                pageSize=100).execute())()
            for g in resp.get("contactGroups", []):
                if g.get("name") == name:
                    try:
                        retry(lambda rn=g["resourceName"]:
                             people.contactGroups().delete(
                                 resourceName=rn, deleteContacts=False).execute())()
                    except Exception:  # noqa: BLE001
                        pass
    except Exception:  # noqa: BLE001
        pass
    return deleted


_TASK_LIST_NAME = "MIGRATION-TEST Tasks"


def seed_tasks(tasks, settings: Settings, count: int = 20) -> dict:
    retry = _retry_factory(settings)
    m = {"lists": 0, "tasks": 0, "note": ""}
    verbs = ("Follow up on", "Review", "Draft", "Approve", "Schedule",
            "Close out", "Escalate", "File")
    subjects = ("the Q3 budget", "vendor contract", "onboarding checklist",
               "security review", "the migration runbook", "client renewal",
               "the roadmap doc", "expense report")
    try:
        created = retry(lambda: tasks.tasklists().insert(
            body={"title": _TASK_LIST_NAME}).execute())()
        list_id = created["id"]
        m["lists"] += 1
        rng = random.Random(count)
        for i in range(count):
            body = {
                "title": f"{rng.choice(verbs)} {rng.choice(subjects)}",
                "notes": "Seeded test data.",
            }
            if i % 3 == 0:
                body["status"] = "completed"
            if i % 4 == 0:
                body["due"] = _iso(rng.randint(1, 30))
            retry(lambda b=body: tasks.tasks().insert(
                tasklist=list_id, body=b).execute())()
            m["tasks"] += 1
    except Exception as exc:  # noqa: BLE001
        m["note"] = f"tasks failed (Tasks API enabled? scopes granted?): {exc}"
        print(f"  ! tasks: {exc}")
    return m


def reset_tasks(tasks, settings: Settings) -> int:
    """Delete only the task list this seeder creates, by name -- deleting a
    list deletes its tasks with it, so there is nothing else to clean up."""
    retry = _retry_factory(settings)
    deleted = 0
    try:
        resp = retry(lambda: tasks.tasklists().list(maxResults=100).execute())()
        for tl in resp.get("items", []):
            if tl.get("title") == _TASK_LIST_NAME:
                try:
                    retry(lambda i=tl["id"]: tasks.tasklists().delete(
                        tasklist=i).execute())()
                    deleted += 1
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass
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
            break

    # Drafts are separate objects. Trashing a draft's underlying message does
    # not remove the draft, so without this they survive every reset and
    # accumulate -- a mailbox reset repeatedly was still holding 201 of them.
    token = None
    while True:
        try:
            resp = retry(lambda t=token: gmail.users().drafts().list(
                userId="me", maxResults=500, pageToken=t).execute())()
        except Exception:  # noqa: BLE001
            break
        for d in resp.get("drafts", []):
            try:
                retry(lambda did=d["id"]: gmail.users().drafts().delete(
                    userId="me", id=did).execute())()
                deleted += 1
            except Exception:  # noqa: BLE001
                pass
        token = resp.get("nextPageToken")
        if not token:
            break

    # Seeded labels, and only those -- deleting every user label would take
    # ones the account's owner created. Left behind they collide on the next
    # run with "Label name exists or conflicts", which is what every label
    # error in the previous reseed actually was.
    try:
        existing = retry(lambda: gmail.users().labels().list(
            userId="me").execute())()
        wanted = {n.lower() for n in SEED_LABELS}
        for lab in existing.get("labels", []):
            if lab.get("type") == "user" and lab.get("name", "").lower() in wanted:
                try:
                    retry(lambda lid=lab["id"]: gmail.users().labels().delete(
                        userId="me", id=lid).execute())()
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass

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


def build_people_tasks(settings: Settings, user: str):
    """
    Delegated People/Tasks clients, with their own credentials object.

    Not appended to SEED_SCOPES -- same reasoning as build_reports() just
    above. contacts/tasks write scopes are typically granted later than
    drive/gmail/calendar/chat (verified live: the SEED_SCOPES credential
    mints fine today; a separate credential requesting only
    contacts+tasks fails with unauthorized_client because those two scopes
    are not yet authorised). Requesting them as part of the same combined
    scope-set as SEED_SCOPES would fail that ENTIRE token exchange the
    moment either is missing -- breaking drive/gmail/calendar/chat seeding
    too, not just contacts/tasks. A separate Credentials object confines the
    failure to exactly the two services that are not ready yet.
    """
    import google_auth_httplib2
    import httplib2
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    from config import CONTACTS_WRITE_SCOPE, TASKS_WRITE_SCOPE

    creds = service_account.Credentials.from_service_account_file(
        _resolve_key_path(settings),
        scopes=[CONTACTS_WRITE_SCOPE, TASKS_WRITE_SCOPE],
    ).with_subject(user)

    def svc(api, version):
        http = google_auth_httplib2.AuthorizedHttp(
            creds, http=httplib2.Http(timeout=300)
        )
        return build(api, version, http=http, cache_discovery=False)

    return svc("people", "v1"), svc("tasks", "v1")


def build_reports(settings: Settings, user: str):
    """A delegated Reports client, for reading licence seat usage.

    Built with REPORTS_SCOPE on top of the seed scopes, and only ever called
    by `--fit-to-licenses`; see the comment above REPORTS_SCOPE for why that
    scope is not just added to SEED_SCOPES.
    """
    import google_auth_httplib2
    import httplib2
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_file(
        _resolve_key_path(settings), scopes=SEED_SCOPES + [REPORTS_SCOPE]
    ).with_subject(user)
    http = google_auth_httplib2.AuthorizedHttp(
        creds, http=httplib2.Http(timeout=120)
    )
    return build("admin", "reports_v1", http=http, cache_discovery=False)


def build_directory_readonly(settings: Settings, user: str):
    """A delegated Directory client, read-only, for `--all-users`.

    Isolated to DIRECTORY_READONLY_SCOPE alone -- same pattern as
    build_reports() and build_people_tasks() -- so a tenant that has not
    granted directory read still lets every other seeding call succeed.
    """
    import google_auth_httplib2
    import httplib2
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_file(
        _resolve_key_path(settings), scopes=[DIRECTORY_READONLY_SCOPE]
    ).with_subject(user)
    http = google_auth_httplib2.AuthorizedHttp(
        creds, http=httplib2.Http(timeout=120)
    )
    return build("admin", "directory_v1", http=http, cache_discovery=False)


def _build_directory_readwrite(settings: Settings, user: str):
    """A delegated Directory client that can create accounts -- for
    --create-users and --create-until-full. Needs
    provision.DIRECTORY_WRITE_SCOPE granted alongside the seeder's own
    scopes, not just the read-only one build_directory_readonly() uses.
    """
    import google_auth_httplib2
    import httplib2
    import provision
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_file(
        _resolve_key_path(settings),
        scopes=SEED_SCOPES + [provision.DIRECTORY_WRITE_SCOPE],
    ).with_subject(user)
    http = google_auth_httplib2.AuthorizedHttp(
        creds, http=httplib2.Http(timeout=120)
    )
    return build("admin", "directory_v1", http=http, cache_discovery=False)


# ======================================================================
# Licence capacity
# ======================================================================
def _parse_seats(parameters: list[dict]) -> dict:
    """Turn `usageReports[0].parameters` into total/used/available seats.

    `intValue` arrives as a string ("10" not 10), and any edition the tenant
    does not hold is simply absent from the report -- so both defaults are
    zero and the sums are over the edition the org actually has.
    """
    values = {}
    for p in parameters:
        name = p.get("name", "")
        raw = p.get("intValue")
        if raw is not None:
            values[name] = int(raw)
    total = sum(values.get(f"accounts:{t}", 0) for t, _ in SEAT_PARAMS)
    used = sum(values.get(f"accounts:{u}", 0) for _, u in SEAT_PARAMS)
    return {"total": total, "used": used, "available": total - used,
            "parameters": values}


def seat_report(settings: Settings, reports=None) -> dict:
    """Read Google Workspace seat usage from the Reports API.

    Runs as SOURCE_ADMIN because the Reports API only answers to an
    administrator. `reports` is injectable for tests; the default builds a
    delegated client. Reports lag up to a day, so the most recent non-empty
    report in the last week is used.
    """
    admin = os.getenv("SOURCE_ADMIN")
    if not admin:
        raise RuntimeError(
            "SOURCE_ADMIN must be set to a super admin of the source domain "
            "to read licence usage."
        )
    if reports is None:
        reports = build_reports(settings, admin)
    params = ",".join(f"accounts:{p}" for pair in SEAT_PARAMS for p in pair)
    for back in range(1, 8):
        d = (datetime.now(timezone.utc) - timedelta(days=back)).strftime("%Y-%m-%d")
        resp = reports.customerUsageReports().get(date=d, parameters=params).execute()
        rows = (resp.get("usageReports") or [{}])[0].get("parameters") or []
        if rows:
            return _parse_seats(rows)
    raise RuntimeError("no licence usage report was available in the last week")


def _list_users(directory) -> set[str]:
    """Every primary email in the source domain, paged."""
    emails = set()
    token = None
    while True:
        resp = directory.users().list(
            customer="my_customer", pageToken=token, maxResults=500,
            fields="users(primaryEmail),nextPageToken",
        ).execute()
        for u in resp.get("users", []):
            emails.add(u["primaryEmail"])
        token = resp.get("nextPageToken")
        if not token:
            break
    return emails


def entries_from_existing_users(existing_emails: set[str], domain: str) -> list[dict]:
    """Build seeding entries from every real account already in `domain`.

    Backs `--all-users`: unlike fit_entries() (which pads out to unused
    licence capacity with generated accounts), this never invents a user --
    it is exactly the tenant's real, current headcount, sorted for a stable
    seeding order across re-runs.
    """
    emails = sorted(e for e in existing_emails if e.endswith("@" + domain))
    entries = []
    for i, email in enumerate(emails):
        template = ORG[i % len(ORG)]
        entries.append({
            "local": email.split("@")[0], "email": email,
            "dept": template["dept"], "project": template["project"],
        })
    return entries


def discover_tenant_entries(settings: Settings) -> tuple[list[dict], str]:
    """
    The seeder's default source of users: every real account already in the
    tenant, discovered live via the Directory API. Returns
    (entries, warning) -- `warning` is empty on success, and non-empty (with
    `entries` falling back to the fixed 5-name ORG default) when discovery
    could not run at all, so a sandbox without SOURCE_ADMIN set or without
    the Directory scope granted yet still seeds something instead of hard
    failing on every ordinary invocation.

    `--all-users` (below) calls entries_from_existing_users() directly
    instead, precisely because it wants the opposite: a hard failure with a
    clear scope-to-grant message, not a silent fallback, when someone has
    explicitly asked to seed the real headcount.
    """
    admin = os.getenv("SOURCE_ADMIN")
    if not admin:
        return ([e | {"email": f"{e['local']}@{settings.source_domain}"}
                for e in ORG],
               "SOURCE_ADMIN is not set, so the real tenant headcount could "
               "not be read; falling back to the fixed 5-user default. Set "
               "SOURCE_ADMIN to a super admin to seed every real account.")
    try:
        directory = build_directory_readonly(settings, admin)
        existing = _list_users(directory)
    except Exception as exc:  # noqa: BLE001
        return ([e | {"email": f"{e['local']}@{settings.source_domain}"}
                for e in ORG],
               f"could not read the tenant's user list ({exc}); falling "
               f"back to the fixed 5-user default. Grant "
               f"{DIRECTORY_READONLY_SCOPE} to SEED_SA_KEY's client ID in "
               f"{settings.source_domain} to seed every real account.")
    entries = entries_from_existing_users(existing, settings.source_domain)
    if not entries:
        return ([e | {"email": f"{e['local']}@{settings.source_domain}"}
                for e in ORG],
               f"the directory returned no users in {settings.source_domain}; "
               f"falling back to the fixed 5-user default.")
    return entries, ""


def _generated_localpart(i: int, taken: set[str]) -> str:
    base = GENERATED_LOCALPARTS[i] if i < len(GENERATED_LOCALPARTS) \
        else f"seeduser{i + 1}"
    candidate, n = base, 1
    while candidate in taken:
        n += 1
        candidate = f"{base}{n}"
    return candidate


def fit_entries(entries: list[dict], available: int,
                existing_emails: set[str], domain: str) -> list[dict]:
    """Fit the requested user list to the tenant's licence headroom.

    * Users that already exist are always kept: they occupy seats already
      counted in the `used` figure, so keeping them never over-subscribes.
    * New users are admitted up to `available`; the list is truncated if it
      asks for more than the tenant can seat.
    * Unused headroom is filled with generated localparts, so a trial's
      licences are actually exercised -- the point of seeding a sandbox up
      to capacity.
    """
    present = [e for e in entries if e["email"] in existing_emails]
    missing = [e for e in entries if e["email"] not in existing_emails]
    room = max(available, 0)
    kept = missing[:room]
    used_local = {e["local"] for e in present} | {e["local"] for e in kept}
    i = 0
    while len(kept) < room:
        lp = _generated_localpart(i, used_local)
        used_local.add(lp)
        template = ORG[(len(present) + len(kept)) % len(ORG)]
        kept.append({"local": lp, "email": f"{lp}@{domain}",
                     "dept": template["dept"], "project": template["project"]})
        i += 1
    return present + kept


# ======================================================================
# Per-user worker
# ======================================================================
def seed_one_user(settings: Settings, entry: dict, all_users: list[str],
                  external: str, scale: str, mail_count: int,
                  event_count: int, edge_cases: bool,
                  target_gb_per_user: float | None = None) -> dict:
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

    # Separate credential (build_people_tasks, not build_services): contacts
    # and tasks write scopes are commonly granted on a different schedule
    # than drive/gmail/calendar/chat, and a missing grant here must not touch
    # anything already seeded successfully above.
    people, tasks = build_people_tasks(settings, user)
    contacts_m = seed_contacts(people, settings, user, peers, external)
    tasks_m = seed_tasks(tasks, settings)

    # Last: every other pass has to finish first so storageQuota.usage
    # reflects everything they wrote, not just some of it.
    fill_m = {"filler_files": 0, "filler_bytes": 0, "note": ""}
    if target_gb_per_user:
        # A fresh client, not the `drive` object above: that one has by now
        # handled hundreds of small requests over several minutes on the
        # same httplib2 connection, and a large (100MB+) resumable upload
        # on a connection in that state is exactly the shape of request
        # that surfaced "Redirected but the response is missing a
        # Location: header" in production -- confirmed reproducible on
        # every single user in a real run, always at this step, never
        # during the small-file corpus pass on the same connection.
        # httplib2's connection reuse is the documented culprit for this
        # class of failure; a new connection sidesteps it entirely.
        fresh_drive, _, _ = build_services(settings, user)
        fill_m = top_up_storage(fresh_drive, settings, user, target_gb_per_user)

    elapsed = round(time.time() - t0, 1)
    print(f"  [{user}] done in {elapsed}s: {drive_m['total_files']} files, "
          f"{drive_m['folders']} folders, {drive_m.get('comments', 0)} comments, "
          f"{gmail_m['messages']} messages, {gmail_m.get('drafts', 0)} drafts, "
          f"{cal_m['events']} events, "
          f"{cal_m.get('calendars', 0)} secondary calendars, "
          f"{chat_m['messages']} chat messages in {chat_m['spaces']} spaces"
          + (f" ({chat_m['note']})" if chat_m["note"] else "")
          + f", {contacts_m['contacts']} contacts"
          + (f" ({contacts_m['note']})" if contacts_m["note"] else "")
          + f", {tasks_m['tasks']} tasks"
          + (f" ({tasks_m['note']})" if tasks_m["note"] else "")
          + (f", {fill_m['filler_files']} filler file(s) "
             f"({fill_m.get('usage_before_gb', 0):.1f}GB -> "
             f"{fill_m.get('usage_after_gb', 0):.1f}GB)"
             if target_gb_per_user else "")
          + (f" ({fill_m['note']})" if fill_m.get("note") else ""))
    return {"user": user, "dept": entry["dept"], "project": entry["project"],
            "drive": drive_m, "gmail": gmail_m, "calendar": cal_m,
            "chat": chat_m, "contacts": contacts_m, "tasks": tasks_m,
            "storage": fill_m, "elapsed_sec": elapsed}


def top_up_one_user(settings: Settings, user: str, target_gb_per_user: float) -> dict:
    """The --top-up-only path: check and fill storage only, safe to re-run
    any number of times without duplicating mail, drive content, contacts or
    tasks -- see top_up_storage()'s docstring for why a second pass is
    sometimes needed (Gmail's usage accounting lags real time)."""
    drive, _gmail, _cal = build_services(settings, user)
    t0 = time.time()
    m = top_up_storage(drive, settings, user, target_gb_per_user)
    elapsed = round(time.time() - t0, 1)
    print(f"  [{user}] top-up in {elapsed}s: {m['usage_before_gb']:.1f}GB -> "
         f"{m['usage_after_gb']:.1f}GB ({m['filler_files']} filler file(s))"
         + (f" -- {m['note']}" if m.get("note") else ""))
    return {"user": user, "storage": m, "elapsed_sec": elapsed}


def reset_one_user(settings: Settings, user: str) -> dict:
    drive, gmail, cal = build_services(settings, user)
    chat = build_chat(settings, user)
    people, tasks = build_people_tasks(settings, user)
    return {
        "user": user,
        "drive": reset_drive(drive, settings),
        "gmail": reset_gmail(gmail, settings),
        "calendar": reset_calendar(cal, settings),
        "chat": reset_chat(chat, settings, user.split("@")[0]),
        "contacts": reset_contacts(people, settings),
        "tasks": reset_tasks(tasks, settings),
    }


# ======================================================================
# CLI
# ======================================================================
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Seed or reset a sandbox organisation."
    )
    ap.add_argument("--confirm-domain", required=True)
    ap.add_argument("--users", help="comma-separated localparts. Default: "
                                    "every user the tenant already has (live "
                                    "Directory lookup), falling back to "
                                    "alice,bob,carol,dave,erin if that "
                                    "lookup cannot run.")
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
    ap.add_argument("--fit-to-licenses", action="store_true",
                    help="query the tenant's Google Workspace licence usage "
                         "and seed up to the available seats: the requested "
                         "list is truncated if it exceeds capacity, users "
                         "that already exist are always kept, and unused "
                         "seats are filled with generated users. Requires "
                         "--create-users and admin.reports.usage.readonly "
                         "granted to SEED_SA_KEY")
    ap.add_argument("--all-users", action="store_true",
                    help="same live discovery the default already does, but "
                         "fails loudly (with the scope to grant) instead of "
                         "silently falling back to the 5-user default if "
                         "SOURCE_ADMIN or the Directory scope is missing -- "
                         "for when you need to be sure it actually read the "
                         "tenant. Not a licence-capacity guess like "
                         "--fit-to-licenses (which creates new accounts up "
                         "to unused seats; this only ever seeds accounts "
                         "that already exist). Requires "
                         "admin.directory.user.readonly granted to "
                         "SEED_SA_KEY, and SOURCE_ADMIN set.")
    ap.add_argument("--create-until-full", action="store_true",
                    help="ignore --users/--all-users/--fit-to-licenses; "
                         "generate and create accounts one at a time until "
                         "the Directory API itself refuses one (out of "
                         "licences, typically), then seed data for exactly "
                         "the ones that succeeded. The empirical answer to "
                         "'how many licences are actually free' when "
                         "--fit-to-licenses's Reports API is lagging. "
                         "Requires --create-users.")
    ap.add_argument("--reset", action="store_true", help="DELETE everything")
    ap.add_argument("--target-gb-per-user", type=float, default=None,
                    help="after normal seeding, add large filler files until "
                         "each user's total Workspace storage (Gmail+Drive+"
                         "Photos, pooled) reaches this many GB. Filler lives "
                         "under the same MIGRATION-TEST root --reset already "
                         "cleans up. Gmail's own usage accounting lags real "
                         "time, so a run right after heavy mail seeding can "
                         "undershoot; re-run with --top-up-only afterwards.")
    ap.add_argument("--top-up-only", action="store_true",
                    help="skip every seeding step and only check/top up "
                         "storage toward --target-gb-per-user. Safe to run "
                         "repeatedly -- it never touches mail, Drive "
                         "documents, contacts or tasks.")
    args = ap.parse_args(argv)

    if args.top_up_only and not args.target_gb_per_user:
        sys.exit("--top-up-only needs --target-gb-per-user")
    if args.top_up_only and args.reset:
        sys.exit("--top-up-only makes no sense with --reset")

    settings = Settings()
    assert_sandbox(settings, args.confirm_domain)

    if args.fit_to_licenses and not args.create_users:
        sys.exit("--fit-to-licenses requires --create-users: it decides how "
                 "many accounts to create, so it only acts on that path.")
    if args.fit_to_licenses and args.reset:
        sys.exit("--fit-to-licenses makes no sense with --reset.")
    if args.create_until_full and not args.create_users:
        sys.exit("--create-until-full requires --create-users.")
    if args.create_until_full and args.reset:
        sys.exit("--create-until-full makes no sense with --reset.")
    if args.create_until_full and (args.fit_to_licenses or args.all_users or args.users):
        sys.exit("--create-until-full replaces --users/--all-users/"
                 "--fit-to-licenses -- it generates its own candidates, "
                 "pick one approach.")
    if args.all_users and args.users:
        sys.exit("--all-users and --users are mutually exclusive: "
                 "--all-users already means every existing user.")
    if args.all_users and args.fit_to_licenses:
        sys.exit("--all-users and --fit-to-licenses are mutually exclusive: "
                 "--all-users seeds accounts that already exist; "
                 "--fit-to-licenses creates new ones up to unused seats.")

    if args.create_until_full:
        # Handles both entry-building AND account creation in one branch,
        # unlike every other mode below (which builds `entries` first,
        # then optionally creates them in the shared --create-users block
        # further down): there is no fixed candidate list to build entries
        # from ahead of time here, since candidates are generated one at a
        # time until the API stops accepting them.
        import provision

        admin = os.getenv("SOURCE_ADMIN")
        if not admin:
            sys.exit("SOURCE_ADMIN must be set to a super admin of "
                     f"{settings.source_domain} to create accounts.")
        directory = _build_directory_readwrite(settings, admin)
        existing = _list_users(directory)
        taken = {e.split("@")[0] for e in existing
                if e.endswith("@" + settings.source_domain)}

        def _candidates():
            i = 0
            while True:
                lp = _generated_localpart(i, taken)
                taken.add(lp)
                yield f"{lp}@{settings.source_domain}"
                i += 1

        print(f"\n{len(existing)} account(s) already exist in "
              f"{settings.source_domain}. Creating generated accounts "
              f"until the API refuses one ...")
        res = provision.create_until_full(directory, _candidates())
        print(f"\nCreated {len(res['created'])} new account(s).")
        print(f"Stopped: {res['stopped_reason']}")
        if not res["created"]:
            sys.exit("No new accounts were created; nothing to seed.")
        entries = []
        for i, email in enumerate(res["created"]):
            template = ORG[i % len(ORG)]
            entries.append({
                "local": email.split("@")[0], "email": email,
                "dept": template["dept"], "project": template["project"],
            })
        # Freshly created accounts take a moment before delegation works
        # -- same wait the shared --create-users block below uses.
        print("\nWaiting 20s for new accounts to become usable ...")
        time.sleep(20)
    elif args.users:
        locals_ = [u.strip() for u in args.users.split(",")]
        entries = []
        for i, lp in enumerate(locals_):
            template = ORG[i % len(ORG)]
            entries.append({
                "local": lp, "email": f"{lp}@{settings.source_domain}",
                "dept": template["dept"], "project": template["project"],
            })
    elif args.all_users:
        # Explicit ask: fail loudly with the scope to grant, rather than
        # silently seeding the 5-user default -- see
        # discover_tenant_entries()'s docstring for why this differs from
        # the default (no-flag) path below.
        admin = os.getenv("SOURCE_ADMIN")
        if not admin:
            sys.exit("SOURCE_ADMIN must be set to a super admin of "
                     f"{settings.source_domain} to read the tenant's user list.")
        directory = build_directory_readonly(settings, admin)
        try:
            existing = _list_users(directory)
        except Exception as exc:  # noqa: BLE001
            sys.exit(f"REFUSING --all-users: could not read the directory "
                     f"({exc}). Grant this scope to SEED_SA_KEY's client ID "
                     f"in {settings.source_domain} (Admin Console > API "
                     f"controls > Domain-wide delegation), then re-run:\n"
                     f"    {DIRECTORY_READONLY_SCOPE}")
        entries = entries_from_existing_users(existing, settings.source_domain)
        if not entries:
            sys.exit(f"REFUSING --all-users: the directory returned no users "
                     f"in {settings.source_domain}.")
        print(f"\n--all-users: found {len(entries)} existing user(s) in "
              f"{settings.source_domain}; seeding all of them.")
    else:
        # Default: check every account the tenant actually has, not the
        # fixed 5 -- with a graceful, clearly-explained fallback to the
        # 5-user default when discovery cannot run (see
        # discover_tenant_entries()'s docstring).
        entries, warning = discover_tenant_entries(settings)
        if warning:
            print(f"\nNote: {warning}")
        else:
            print(f"\nFound {len(entries)} existing user(s) in "
                  f"{settings.source_domain}; seeding all of them.")
    all_users = [e["email"] for e in entries]

    # --- Optionally create the accounts first ----------------------------
    # --create-until-full already created its accounts (and built `entries`
    # from exactly what succeeded) in its own branch above -- this block is
    # the shared path for the other create-first modes only.
    if args.create_users and not args.reset and not args.create_until_full:
        import provision

        admin = os.getenv("SOURCE_ADMIN")
        if not admin:
            sys.exit("SOURCE_ADMIN must be set to a super admin of "
                     f"{settings.source_domain} to create accounts.")
        directory = _build_directory_readwrite(settings, admin)

        if args.fit_to_licenses:
            existing = _list_users(directory)
            try:
                seats = seat_report(settings)
            except Exception as exc:  # noqa: BLE001
                print(f"REFUSING --fit-to-licenses: could not read licence "
                      f"usage ({exc}).")
                print(f"Grant this scope to SEED_SA_KEY's client ID in "
                      f"{settings.source_domain} (Admin Console > API "
                      f"controls > Domain-wide delegation), then re-run:")
                print(f"    {REPORTS_SCOPE}")
                return 1
            print(f"\nLicence usage: {seats['total']} total, "
                  f"{seats['used']} used, {seats['available']} available")
            entries = fit_entries(entries, seats["available"], existing,
                                  settings.source_domain)
            all_users = [e["email"] for e in entries]
            if not all_users:
                print("No users to seed: the tenant has no free licences "
                      "and none of the requested users already exist.")
                return 1
            present = len(existing & set(all_users))
            print(f"  users to seed: {len(all_users)} "
                  f"({present} already exist, "
                  f"{len(all_users) - present} new)")

        print(f"\nEnsuring {len(all_users)} account(s) exist in "
              f"{settings.source_domain} ...")
        res = provision.ensure_users(directory, all_users)
        provision.report(res)
        if res["failed"]:
            sys.exit("Some accounts could not be created; not seeding.")
        # Freshly created accounts take a moment before delegation works.
        print("\nWaiting 20s for new accounts to become usable ...")
        time.sleep(20)

    # Resolve the pool size before either branch. --workers 0 means "size it
    # to this machine"; the reset path used to run before that resolution and
    # died with max_workers must be greater than 0.
    if not args.workers:
        try:
            import resources
            rec = resources.recommend()
            args.workers = rec["seed_workers"]
            print(f"Workers: {args.workers} ({rec['reason']})")
        except Exception:  # noqa: BLE001
            args.workers = 3

    # --- Top-up only -------------------------------------------------------
    if args.top_up_only:
        print(f"\nTopping up storage for {len(all_users)} user(s) toward "
             f"{args.target_gb_per_user:.1f} GB each ...")
        with futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            list(pool.map(
                lambda u: top_up_one_user(settings, u, args.target_gb_per_user),
                all_users))
        return 0

    # --- Reset -----------------------------------------------------------
    if args.reset:
        print(f"About to DELETE all Drive files, mail, events and Chat for:")
        for u in all_users:
            print(f"    {u}")
        # --yes satisfies this because --confirm-domain is already a typed
        # match against SOURCE_DOMAIN, checked by assert_sandbox before we get
        # here. Without this, an unattended reset blocks forever on input()
        # with no terminal to type into -- and a hung wipe looks identical to
        # a slow one.
        if not args.yes:
            if input("Type the domain to confirm: ").strip() != settings.source_domain:
                print("Aborted.")
                return 1
        else:
            print(f"  (--yes, and --confirm-domain already matched "
                  f"{settings.source_domain})")
        with futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            for r in pool.map(lambda u: reset_one_user(settings, u), all_users):
                print(f"  {r['user']}: {r['drive']} files, {r['gmail']} "
                      f"messages, {r['calendar']} events, "
                      f"{r['chat']} chat spaces, {r['contacts']} contacts, "
                      f"{r['tasks']} task list(s) deleted")
        for f in (args.manifest,):
            if os.path.exists(f):
                os.remove(f)
        return 0

    # --- Seed ------------------------------------------------------------
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
                args.target_gb_per_user,
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
        "contacts": sum(r.get("contacts", {}).get("contacts", 0) for r in ok),
        "tasks": sum(r.get("tasks", {}).get("tasks", 0) for r in ok),
        "filler_files": sum(r.get("storage", {}).get("filler_files", 0) for r in ok),
        "filler_gb": round(sum(
            r.get("storage", {}).get("filler_bytes", 0) for r in ok) / 1e9, 2),
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
    print(f"  Contacts    : {totals['contacts']:,}")
    print(f"  Tasks       : {totals['tasks']:,}")
    if args.target_gb_per_user:
        print(f"  Filler      : {totals['filler_files']:,} file(s), "
              f"{totals['filler_gb']:.2f} GB added toward "
              f"{args.target_gb_per_user:.1f} GB/user")
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
