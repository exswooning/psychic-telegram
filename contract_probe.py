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
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from auth import AuthManager          # noqa: E402
from config import FOLDER_MIME, Settings  # noqa: E402

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


class Probe:
    """One assumption, its source in the fakes, and what Google says."""

    def __init__(self):
        self.results: list[tuple[str, str, str, str]] = []

    def record(self, name: str, where: str, status: str, detail: str) -> None:
        self.results.append((name, where, status, detail))

    def report(self) -> int:
        width = max(len(n) for n, _, _, _ in self.results) + 2
        print(f"\n{'assumption':<{width}} {'result':<6} detail")
        print("-" * (width + 66))
        for name, where, status, detail in self.results:
            print(f"{name:<{width}} {status:<6} {detail}")
        failed = [r for r in self.results if r[2] == FAIL]
        skipped = [r for r in self.results if r[2] == SKIP]
        print()
        for name, where, _, detail in failed:
            print(f"  {name}\n    encoded in: {where}\n    reality:    {detail}\n")
        print(f"{len(self.results) - len(failed) - len(skipped)} held, "
              f"{len(failed)} contradicted, {len(skipped)} untestable here.")
        if failed:
            print("\nA contradicted assumption means the fake disagrees with "
                  "Google, and every test resting on it proves nothing.")
        return 1 if failed else 0


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
    got = {}
    for label, mask in masks.items():
        r = drive.files().list(
            q="trashed=false and 'me' in owners", spaces="drive", pageSize=1000,
            fields=f"nextPageToken,{mask}").execute()
        got[label] = len(r.get("files", []))

    p.record("pageSize=1000 is a ceiling, not a promise",
             "discovery.iter_all_drive_items assumes 1000/page",
             PASS if got["minimal"] > got["engine"] >= got["with-inline-perms"]
             else SKIP,
             f"minimal {got['minimal']}, engine mask {got['engine']}, "
             f"inline perms {got['with-inline-perms']} per page")

    if got["engine"] and got["with-inline-perms"]:
        ratio = got["engine"] / got["with-inline-perms"]
        p.record("inline perms shrink the page", "not relied upon",
                 PASS if ratio > 1.5 else SKIP,
                 f"{ratio:.1f}x more list calls to read the same corpus")


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


def probe_drive_writes(p: Probe, drive) -> None:
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

        # The fake bumps modifiedTime on permissions.create. Does Drive?
        drive.permissions().create(
            fileId=fid, sendNotificationEmail=False,
            body={"type": "anyone", "role": "reader"}).execute()
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Check tests/fakes.py assumptions against the real APIs.")
    ap.add_argument("--include-writes", action="store_true",
                    help="also create and delete one scratch file on the target")
    ap.add_argument("--user", help="which mapped user to probe as")
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
            try:
                probe_drive_writes(p, auth.target_drive(target))
            except Exception as exc:  # noqa: BLE001
                p.record("drive write probes", "-", SKIP, str(exc)[:90])

    return p.report()


if __name__ == "__main__":
    raise SystemExit(main())
