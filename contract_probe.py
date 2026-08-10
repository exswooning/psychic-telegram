"""
contract_probe.py
=================
Check the assumptions `tests/fakes.py` encodes against the real APIs.

Why this exists
---------------
The suite runs entirely against fakes, and a fake is written to match its
author's belief about the API. That makes it excellent at catching logic
errors and structurally incapable of catching a wrong belief: every assertion
about how a request is built or what a response contains is checked against
something we wrote to agree with us.

Two defects in one session came from exactly that shape and neither was
findable from inside the suite:

  * resources.py faked `_read_first`, so the cgroup *path discovery* -- where
    the bug lived -- had no coverage at all. 610 tests passed while a
    512 MB container read the host's 3.7 GB.
  * the Gmail dedup guard read Message-ID from `payload.headers`, but the
    fetch uses `format="raw"`, which returns no parsed headers. The test
    mocked the response, so it agreed.

So the assumptions get written down here and checked against Google. A failure
means the fake is lying and some number of green tests are meaningless.

Read-only by default. `--include-writes` additionally creates one scratch file
in the TARGET tenant to observe write-side behaviour, then deletes it; that
path is guarded by SANDBOX_MODE like every other tool here that writes.

    python3 contract_probe.py
    python3 contract_probe.py --include-writes
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from auth import AuthManager          # noqa: E402
from config import FOLDER_MIME, Settings  # noqa: E402

# Four outcomes, not three. SKIP used to mean both "could not test this here"
# and "tested it and the result was not what I expected", which records a
# genuine regression as a shrug -- if Drive started honouring pageSize
# uniformly, the old code reported SKIP and nobody would look.
PASS = "PASS"      # the assumption held
FAIL = "FAIL"      # the assumption is contradicted; tests resting on it are void
UNEXP = "UNEXP"    # tested, and the answer differs from what the code assumes
SKIP = "SKIP"      # could not be tested here (empty corpus, missing scope)


class Probe:
    """One assumption, its source in the fakes, and what Google says."""

    def __init__(self):
        self.results: list[tuple[str, str, str, str]] = []

    def record(self, name: str, where: str, status: str, detail: str) -> None:
        self.results.append((name, where, status, detail))

    def report(self, min_held: int = 0) -> int:
        width = max(len(n) for n, _, _, _ in self.results) + 2
        print(f"\n{'assumption':<{width}} {'result':<6} detail")
        print("-" * (width + 66))
        for name, where, status, detail in self.results:
            print(f"{name:<{width}} {status:<6} {detail}")
        failed = [r for r in self.results if r[2] == FAIL]
        unexpected = [r for r in self.results if r[2] == UNEXP]
        skipped = [r for r in self.results if r[2] == SKIP]
        print()
        for name, where, _, detail in failed + unexpected:
            print(f"  {name}\n    encoded in: {where}\n    reality:    {detail}\n")
        held = len(self.results) - len(failed) - len(unexpected) - len(skipped)
        print(f"{held} held, {len(failed)} contradicted, "
              f"{len(unexpected)} answered differently than assumed, "
              f"{len(skipped)} untestable here.")
        if failed:
            print("\nA contradicted assumption means the fake disagrees with "
                  "Google, and every test resting on it proves nothing.")
        if skipped:
            print("Untestable is not reassurance -- a corpus smaller than one "
                  "page skips the page-size checks entirely, which is the "
                  "state most first runs are in.")
        # A probe that quietly stops checking things passes forever. If the
        # sandbox corpus is reset or a scope lapses, assumptions turn into
        # SKIPs one by one and nothing ever goes red -- the same failure the
        # collected-count floor guards against in the test workflow.
        if min_held and held < min_held:
            print(f"\nFLOOR: only {held} assumptions could be checked, "
                  f"expected at least {min_held}. Something stopped being "
                  f"testable -- an empty corpus, a lapsed scope, a reset "
                  f"sandbox. Fix it or lower the floor deliberately.")
            return 1
        return 1 if failed or unexpected else 0


def probe_drive_read(p: Probe, drive) -> None:
    fields = ("files(id,name,mimeType,shared,permissionIds,"
              "permissions(id,type,role,emailAddress,permissionDetails))")
    resp = drive.files().list(
        q="trashed=false and 'me' in owners", spaces="drive", pageSize=1000,
        fields=f"nextPageToken,{fields}").execute()
    files = [f for f in resp.get("files", []) if f.get("mimeType") != FOLDER_MIME]
    if not files:
        p.record("drive.shared populated", "fakes.FakeDrive._shared_flag", SKIP,
                 "no files owned by this user")
        return

    with_shared = sum(1 for f in files if "shared" in f)
    p.record("files.list returns `shared`", "fakes.FakeDrive._shared_flag",
             PASS if with_shared == len(files) else FAIL,
             f"{with_shared}/{len(files)} files carried the field")

    # The engine skips permissions.list when shared is False, on the grounds
    # that such a file has no grant but the owner's.
    unshared = [f for f in files if f.get("shared") is False]
    owner_only = [f for f in unshared
                  if all(perm.get("role") == "owner"
                         for perm in (f.get("permissions") or []))]
    p.record("unshared => owner-only grants", "drive_engine._sync_acls skip",
             PASS if len(owner_only) == len(unshared) else FAIL,
             f"{len(owner_only)}/{len(unshared)} unshared files were owner-only")

    # Inline permissions: present, but do they carry permissionDetails? The
    # engine reads `inherited` from there to honour recreate_inherited_acls.
    inline_ok = sum(1 for f in files if f.get("permissions"))
    p.record("files.list returns permissions inline", "not relied upon",
             PASS if inline_ok == len(files) else FAIL,
             f"{inline_ok}/{len(files)}")

    shared_files = [f for f in files if f.get("shared")]
    detail_inline = sum(
        1 for f in shared_files for perm in (f.get("permissions") or [])
        if perm.get("permissionDetails"))
    if shared_files:
        probe_file = shared_files[0]
        listed = drive.permissions().list(
            fileId=probe_file["id"], pageSize=100,
            fields="permissions(id,role,permissionDetails)").execute()
        detail_listed = sum(1 for perm in listed.get("permissions", [])
                            if perm.get("permissionDetails"))
        p.record("permissionDetails absent inline",
                 "drive_engine._sync_acls uses permissions.list",
                 PASS if detail_inline == 0 and detail_listed > 0 else SKIP,
                 f"inline {detail_inline}, permissions.list {detail_listed} "
                 f"-- inline cannot answer `inherited`")

        counts_match = len(probe_file.get("permissions") or []) == len(
            listed.get("permissions", []))
        p.record("inline grant count matches list", "not relied upon",
                 PASS if counts_match else FAIL,
                 f"{len(probe_file.get('permissions') or [])} vs "
                 f"{len(listed.get('permissions', []))}")


def probe_page_sizes(p: Probe, drive) -> None:
    """
    pageSize is a ceiling, not a promise, and Drive lowers it by response cost.

    Nobody predicted this; it turned up comparing two probes that disagreed on
    how many files the same user owned. It matters twice over: it is a second,
    independent reason not to request permissions inline, and it invalidates
    the "one list call per 1000 items" arithmetic behind a flat-listing
    rewrite.
    """
    masks = {
        "minimal": "files(id,name)",
        "engine": ("files(id,name,mimeType,parents,modifiedTime,size,"
                   "md5Checksum,owners,shared,capabilities(canDownload),"
                   "shortcutDetails,description,starred)"),
        "with-inline-perms": ("files(id,name,mimeType,shared,"
                              "permissions(id,type,role,emailAddress))"),
    }
    got, capped = {}, {}
    for label, mask in masks.items():
        r = drive.files().list(
            q="trashed=false and 'me' in owners", spaces="drive", pageSize=1000,
            fields=f"nextPageToken,{mask}").execute()
        got[label] = len(r.get("files", []))
        # Only a nextPageToken proves a cap was hit; otherwise the corpus
        # simply ran out and the number says nothing about the ceiling.
        capped[label] = bool(r.get("nextPageToken"))

    # The minimal arm returning everything with no nextPageToken proves
    # nothing about the ceiling -- it never reached one. Saying so is the
    # difference between a measurement and a shrug.
    minimal_capped = capped.get("minimal", False)
    detail = (f"minimal {got['minimal']}"
              + ("" if minimal_capped else " (whole corpus, cap never reached)")
              + f", engine mask {got['engine']}, "
              f"inline perms {got['with-inline-perms']} per page")
    if not capped.get("engine") and not capped.get("with-inline-perms"):
        status = SKIP      # corpus fits in one page for every mask
        detail += " -- corpus too small to observe a cap"
    elif got["engine"] > got["with-inline-perms"]:
        status = PASS
    else:
        status = UNEXP     # Drive started honouring pageSize uniformly
    p.record("pageSize is a ceiling, not a promise",
             "any 'items/1000' arithmetic about flat listing", status, detail)

    if got["engine"] and got["with-inline-perms"]:
        ratio = got["engine"] / got["with-inline-perms"]
        p.record("inline perms shrink the page", "not relied upon",
                 PASS if ratio > 1.5 else SKIP,
                 f"{ratio:.1f}x more list calls to read the same corpus")


def probe_gmail_index_latency(p: Probe, gmail, scratch_domain: str) -> None:
    """
    How long after an insert can `rfc822msgid:` find the message?

    This is the assumption the dedup guard actually rests on, and proving the
    search *works* does not prove it works in the window the guard uses. The
    guard queries roughly one backoff delay after an insert that may have
    landed a second earlier. Gmail's search index is not synchronous with
    messages.insert -- so if indexing is slower than the delay, the lookup
    finds nothing, the retry inserts, and the duplicate happens anyway with
    every test green and the capability probe passing.

    Verifying a capability and verifying its timing are different claims. This
    measures the second.
    """
    msgid = f"<probe-{uuid.uuid4().hex}@{scratch_domain}>"
    raw = (f"Message-ID: {msgid}\r\n"
           f"From: probe@{scratch_domain}\r\n"
           f"To: probe@{scratch_domain}\r\n"
           f"Subject: contract probe -- safe to delete\r\n"
           f"Date: Mon, 3 Aug 2026 00:00:00 +0000\r\n\r\n"
           f"Written by contract_probe.py to measure search index latency.\r\n")
    body = {"raw": base64.urlsafe_b64encode(raw.encode()).decode(),
            "labelIds": ["INBOX"]}
    created = gmail.users().messages().insert(
        userId="me", body=body, internalDateSource="dateHeader").execute()
    mid = created["id"]

    try:
        started = time.monotonic()
        found_after = None
        # 30s ceiling: past that the guard is not viable as designed anyway.
        while time.monotonic() - started < 30.0:
            hits = gmail.users().messages().list(
                userId="me", q=f"rfc822msgid:{msgid}", maxResults=1,
                includeSpamTrash=True).execute().get("messages") or []
            if hits:
                found_after = time.monotonic() - started
                break
            time.sleep(0.5)

        # Does insert spam-filter at all? The engine uses insert rather than
        # import precisely to bypass the delivery pipeline, so it should not
        # -- but includeSpamTrash was being carried on that belief without
        # anyone checking it.
        stored = gmail.users().messages().get(
            userId="me", id=mid, format="minimal").execute()
        labels = stored.get("labelIds") or []
        p.record("insert does not spam-filter",
                 "gmail_engine._find_by_message_id includeSpamTrash",
                 PASS if "SPAM" not in labels else UNEXP,
                 f"asked for INBOX, stored as {','.join(labels) or '(none)'}")

        if found_after is None:
            p.record("insert is searchable within 30s",
                     "gmail_engine._insert_once before_retry", FAIL,
                     "never appeared -- the dedup guard cannot see its own "
                     "insert and will duplicate on every transport retry")
            return

        # The first retry sleeps random.uniform(0, base_delay), so the
        # earliest possible check is ~0s after the failure.
        verdict = PASS if found_after <= 1.0 else UNEXP
        p.record("insert is searchable within one backoff",
                 "gmail_engine._insert_once before_retry", verdict,
                 f"visible after {found_after:.1f}s"
                 + ("" if verdict == PASS else
                    " -- the guard's first check must be delayed past this"))
    finally:
        # trash, not delete: permanent deletion needs the full mail.google.com
        # scope, which this tool deliberately does not hold. Trashing works
        # with gmail.modify, which it does. A probe that cannot clean up after
        # itself leaves litter in a real mailbox every time it runs.
        try:
            gmail.users().messages().trash(userId="me", id=mid).execute()
        except Exception as exc:      # noqa: BLE001
            print(f"  ! could not trash probe message {mid}: {exc}\n"
                  f"    remove it by hand; search rfc822msgid:{msgid}")


def probe_gmail_read(p: Probe, gmail) -> None:
    listed = gmail.users().messages().list(
        userId="me", maxResults=1, includeSpamTrash=True).execute()
    msgs = listed.get("messages") or []
    if not msgs:
        p.record("format=raw omits payload.headers", "gmail_engine", SKIP,
                 "mailbox is empty")
        return

    full = gmail.users().messages().get(
        userId="me", id=msgs[0]["id"], format="raw").execute()

    # The bug: the dedup guard originally read Message-ID from payload.headers.
    has_payload = bool((full.get("payload") or {}).get("headers"))
    p.record("format=raw omits payload.headers",
             "gmail_engine._message_id_header",
             PASS if not has_payload else FAIL,
             "no parsed headers, so Message-ID must come from `raw`"
             if not has_payload else "payload.headers WAS present")

    raw = full.get("raw") or ""
    p.record("format=raw returns base64url `raw`", "fakes.FakeGmail.add_message",
             PASS if raw else FAIL, f"{len(raw)} chars")

    # The engine passes `raw` through untouched rather than re-encoding it.
    try:
        base64.urlsafe_b64decode(raw)
        decodes = True
    except Exception:      # noqa: BLE001
        decodes = False
    p.record("`raw` is urlsafe-base64", "gmail_engine passes it through",
             PASS if decodes else FAIL,
             "decodes with urlsafe alphabet" if decodes else "does NOT decode")

    # rfc822msgid: is what the dedup guard queries on.
    text = base64.urlsafe_b64decode(raw[: (8192 // 3) * 4 // 4 * 4]).decode(
        "utf-8", "replace")
    msgid = None
    for line in text.splitlines():
        if line.lower().startswith("message-id:"):
            msgid = line.split(":", 1)[1].strip()
            break
    if not msgid:
        p.record("rfc822msgid: search works", "gmail_engine._find_by_message_id",
                 SKIP, "sampled message carries no Message-ID header")
        return
    found = gmail.users().messages().list(
        userId="me", q=f"rfc822msgid:{msgid}", maxResults=1,
        includeSpamTrash=True).execute().get("messages") or []
    p.record("rfc822msgid: search works", "gmail_engine._find_by_message_id",
             PASS if found else FAIL,
             f"{msgid[:40]} -> {len(found)} hit(s)")


def probe_drive_writes(p: Probe, drive, grantee: str) -> None:
    """Create one scratch file to observe write-side behaviour, then remove it."""
    created = drive.files().create(
        body={"name": f"CONTRACT-PROBE-{uuid.uuid4().hex[:8]}",
              "mimeType": "text/plain",
              "modifiedTime": "2019-01-01T00:00:00.000Z"},
        fields="id,modifiedTime").execute()
    fid = created["id"]
    try:
        before = created.get("modifiedTime", "")
        p.record("files.create honours modifiedTime", "drive_engine._sync_binary",
                 PASS if before.startswith("2019") else FAIL,
                 f"asked 2019, got {before[:10]}")

        # A user grant, not type:anyone. Link sharing is restricted in plenty
        # of tenants, and there the anyone grant fails on policy -- reporting
        # FAIL against a modifiedTime assumption that was never reached.
        drive.permissions().create(
            fileId=fid, sendNotificationEmail=False,
            body={"type": "user", "role": "reader",
                  "emailAddress": grantee}).execute()
        after_grant = drive.files().get(
            fileId=fid, fields="modifiedTime").execute().get("modifiedTime", "")
        bumped = not after_grant.startswith("2019")
        p.record("a grant bumps modifiedTime", "fakes.PERMISSION_BUMP_TIME",
                 PASS if bumped else FAIL,
                 f"{before[:10]} -> {after_grant[:10]}"
                 + ("" if bumped else "  (restore step may be unnecessary)"))
    finally:
        try:
            drive.files().delete(fileId=fid).execute()
        except Exception as exc:      # noqa: BLE001
            print(f"  ! could not delete scratch file {fid}: {exc}")


def probe_staging_acl_order(p: Probe, auth, settings, source_user: str,
                            target_user: str, grantee: str) -> None:
    """
    Can grants be applied while the copy is still in the staging shared
    drive, so the staging->My Drive move can carry the final modifiedTime?

    Why it matters. The server-side path currently spends three TARGET-account
    writes per file -- move, grant batch, modifiedTime restore -- against a
    3/sec per-account ceiling, and that is the binding constraint on the whole
    migration. Granting before the move would fold the restore into the move
    and drop it to two. That is ~1.5x, which is worth more than everything
    else left on the table.

    Answered, 2026-08-10, against the live tenant: **no, and this probe is
    what keeps it from being retried.** The grant does survive the move, but
    a parent-changing update cannot reassert modifiedTime once a grant has
    bumped it -- the file came out stamped 2026-08-10 instead of 2019. In the
    reordered design the move is last, so nothing would correct it, and every
    shared file would silently carry the migration date. The separate restore
    is load-bearing.

    Two controls, because they fail differently: the same move with **no**
    preceding grant does hold modifiedTime, which is why unshared files are
    correct today even though `_restore_modified_time` never runs for them.

    B4 recorded a clean run while 20,714 of 20,714 grants were 404ing, so
    "it did not throw" is not evidence; this checks the resulting state.

    Creates one scratch file and one staging drive, and removes both.
    """
    src = auth.source_drive(source_user)
    tgt = auth.target_drive(target_user)
    stamp = "2019-01-01T00:00:00.000Z"
    drive_id = fid = copy_id = None

    try:
        drive_id = tgt.drives().create(
            requestId=uuid.uuid4().hex,
            body={"name": f"CONTRACT-PROBE-STAGING-{uuid.uuid4().hex[:8]}"},
            fields="id").execute()["id"]
        tgt.permissions().create(
            fileId=drive_id, body={"type": "user", "role": "organizer",
                                   "emailAddress": source_user},
            supportsAllDrives=True, sendNotificationEmail=False,
            fields="id").execute()

        fid = src.files().create(
            body={"name": f"CONTRACT-PROBE-{uuid.uuid4().hex[:8]}",
                  "mimeType": "text/plain", "modifiedTime": stamp},
            fields="id").execute()["id"]

        copy_id = src.files().copy(
            fileId=fid, body={"name": "probe-copy", "parents": [drive_id],
                              "modifiedTime": stamp},
            supportsAllDrives=True, fields="id").execute()["id"]

        # 1. Does a grant on a file inside the staging shared drive stick?
        granted = False
        try:
            tgt.permissions().create(
                fileId=copy_id, body={"type": "user", "role": "reader",
                                      "emailAddress": grantee},
                supportsAllDrives=True, sendNotificationEmail=False,
                fields="id").execute()
            granted = True
        except Exception as exc:      # noqa: BLE001
            p.record("grant applies inside the staging drive",
                     "drive_engine._sync_server_side (proposed reorder)",
                     FAIL, str(exc)[:80])

        if granted:
            # 2. Does it survive the move out to My Drive, and does the move's
            #    own modifiedTime hold now that a grant preceded it?
            tgt.files().update(
                fileId=copy_id, addParents="root", removeParents=drive_id,
                body={"modifiedTime": stamp}, supportsAllDrives=True,
                fields="id").execute()

            after = tgt.files().get(
                fileId=copy_id, fields="modifiedTime",
                supportsAllDrives=True).execute().get("modifiedTime", "")
            perms = tgt.permissions().list(
                fileId=copy_id, fields="permissions(role,emailAddress)",
                supportsAllDrives=True).execute().get("permissions", [])
            kept = any(x.get("emailAddress") == grantee for x in perms)

            p.record("grant survives the move to My Drive",
                     "drive_engine._sync_server_side",
                     PASS if kept else FAIL,
                     f"grantee present after move: {kept}")
            # PASS here means the move could NOT carry modifiedTime once a
            # grant preceded it -- i.e. the standalone restore step is
            # load-bearing and the reorder must not be done. Recorded this
            # way round on purpose: an optimisation that is unsafe is a
            # settled question, not a standing failure to look at every run.
            stale = not after.startswith("2019")
            p.record("post-grant move cannot carry modifiedTime",
                     "drive_engine._restore_modified_time (why it exists)",
                     PASS if stale else UNEXP,
                     "restore step required"
                     if stale else
                     f"move DID hold 2019 -- reorder may now be viable "
                     f"({after[:10]})")
    except Exception as exc:          # noqa: BLE001
        p.record("staging-drive ACL ordering", "-", SKIP, str(exc)[:90])
    finally:
        for svc, ident in ((tgt, copy_id), (src, fid)):
            if ident:
                try:
                    svc.files().delete(fileId=ident,
                                       supportsAllDrives=True).execute()
                except Exception:     # noqa: BLE001
                    pass
        if drive_id:
            try:
                tgt.drives().delete(driveId=drive_id).execute()
            except Exception as exc:  # noqa: BLE001
                print(f"  ! could not delete probe staging drive {drive_id}: {exc}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Check tests/fakes.py assumptions against the real APIs.")
    ap.add_argument("--include-writes", action="store_true",
                    help="also create and delete one scratch file on the target")
    ap.add_argument("--user", help="which mapped user to probe as")
    ap.add_argument("--min-held", type=int, default=0,
                    help="fail if fewer than N assumptions could be checked")
    args = ap.parse_args(argv)

    settings = Settings()
    auth = AuthManager(settings)
    user = args.user or f"alice@{settings.source_domain}"
    print(f"probing as {user}")

    p = Probe()
    for label, fn, svc in (
        ("drive", probe_drive_read, lambda: auth.source_drive(user)),
        ("pagesize", probe_page_sizes, lambda: auth.source_drive(user)),
        ("gmail", probe_gmail_read, lambda: auth.source_gmail(user)),
    ):
        try:
            fn(p, svc())
        except Exception as exc:      # noqa: BLE001 - one API must not stop the rest
            p.record(f"{label} probe", "-", SKIP, str(exc)[:90])

    if args.include_writes:
        if os.getenv("SANDBOX_MODE", "").lower() != "true":
            p.record("drive write probes", "-", SKIP,
                     "needs SANDBOX_MODE=true; it creates a file on the target")
        else:
            target = user.replace(settings.source_domain, settings.target_domain)
            # A grant needs somebody other than the file's owner, so take a
            # second mapped account rather than inventing an address.
            from db import MigrationDB

            rows = [r["target_email"] for r
                    in MigrationDB(settings.db_path).all_identities()
                    if r["entity_type"] == "user" and r["target_email"] != target]
            if not rows:
                p.record("drive write probes", "-", SKIP,
                         "need a second mapped account to grant to")
            else:
                try:
                    probe_drive_writes(p, auth.target_drive(target), rows[0])
                except Exception as exc:  # noqa: BLE001
                    p.record("drive write probes", "-", SKIP, str(exc)[:90])
            try:
                probe_gmail_index_latency(p, auth.target_gmail(target),
                                          settings.target_domain)
            except Exception as exc:      # noqa: BLE001
                p.record("insert is searchable within one backoff",
                         "gmail_engine._insert_once before_retry", SKIP,
                         str(exc)[:90])
            if rows:
                probe_staging_acl_order(p, auth, settings, user, target, rows[0])

    return p.report(args.min_held)


if __name__ == "__main__":
    raise SystemExit(main())
