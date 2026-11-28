"""Measure the Gmail inventory path on the repository's stateful API fake.

Run from the repository root with:
    .venv/bin/python benchmarks/verify_incremental.py

This isolates mailbox inventory work. It assumes all target messages are
unmapped DMS deliveries, so today's stray path fetches metadata for each one.
"""

from __future__ import annotations

import base64
import json
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import verify_sample as V
from db import MigrationDB
from tests.fakes import FakeGmail

COUNT = 40_000
SOURCE_USER = "bench@source.example"
TARGET_USER = "bench@target.example"
RAW = base64.urlsafe_b64encode(
    b"Message-ID: <benchmark@example>\r\nFrom: sender@example\r\n"
    b"Date: Mon, 3 Jun 2019 10:00:00 +0000\r\nSubject: benchmark\r\n\r\nbody\r\n"
).decode("ascii")


def _seed(mailbox: FakeGmail) -> None:
    for index in range(COUNT):
        item_id = f"m{index:05d}"
        mailbox.messages[item_id] = {
            "id": item_id,
            "raw": RAW,
            "labelIds": ["INBOX"],
            "threadId": f"thread-{index}",
            "internalDate": str(1_700_000_000_000 + index),
            "sizeEstimate": 144,
        }
    # The initial fixture represents an existing mailbox snapshot. A history
    # marker is enough here; the benchmark's tested changes start afterward.
    mailbox._hist = 1


def _counts(*mailboxes):
    methods = ("users.getProfile", "history.list", "messages.list", "messages.get")
    return {method: sum(mailbox.call_count(method) for mailbox in mailboxes) for method in methods}


def _measure(name, mailboxes, fn):
    for mailbox in mailboxes:
        mailbox.reset_calls()
    started = time.perf_counter()
    fn()
    elapsed = time.perf_counter() - started
    return {"case": name, "seconds": round(elapsed, 3), "apiCalls": _counts(*mailboxes)}


def _legacy(v, source, target):
    for _ in v._list_all(source, side="source"):
        pass
    for message in v._list_all(target, side="target"):
        target.users().messages().get(
            userId="me", id=message["id"], format="metadata",
            metadataHeaders=["Message-ID", "From"],
        ).execute()


def main():
    source = FakeGmail(SOURCE_USER, "source")
    target = FakeGmail(TARGET_USER, "target")
    _seed(source)
    _seed(target)
    with tempfile.TemporaryDirectory(prefix="verify-incremental-") as temp:
        db = MigrationDB(str(Path(temp) / "migration.db"))
        auth = SimpleNamespace(source_gmail=lambda _user: source, target_gmail=lambda _user: target)
        settings = SimpleNamespace()
        v = V.Verifier(auth, db, settings, SOURCE_USER, TARGET_USER)
        v._gmail_budget = lambda *_args: SimpleNamespace(acquire=lambda *_a, **_k: None,
                                                         penalise=lambda *_a, **_k: None)
        boxes = (source, target)
        results = []

        results.append(_measure("legacy-first", boxes, lambda: _legacy(v, source, target)))
        results.append(_measure("incremental-first", boxes, lambda: (
            v._gmail_inventory(source, "source"), v._gmail_inventory(target, "target"))))

        results.append(_measure("legacy-reverify-0", boxes, lambda: _legacy(v, source, target)))
        results.append(_measure("incremental-reverify-0", boxes, lambda: (
            v._gmail_inventory(source, "source"), v._gmail_inventory(target, "target"))))

        for index in range(10):
            source.relabel(f"m{index:05d}", add=("STARRED",))
            target.relabel(f"m{index:05d}", add=("STARRED",))
        results.append(_measure("legacy-reverify-10-per-side", boxes, lambda: _legacy(v, source, target)))
        results.append(_measure("incremental-reverify-10-per-side", boxes, lambda: (
            v._gmail_inventory(source, "source"), v._gmail_inventory(target, "target"))))

        for index in range(10, 1010):
            source.relabel(f"m{index:05d}", add=("STARRED",))
            target.relabel(f"m{index:05d}", add=("STARRED",))
        results.append(_measure("legacy-reverify-1000-per-side", boxes, lambda: _legacy(v, source, target)))
        results.append(_measure("incremental-reverify-1000-per-side", boxes, lambda: (
            v._gmail_inventory(source, "source"), v._gmail_inventory(target, "target"))))

        db.close()
        print(json.dumps({"messagesPerMailbox": COUNT, "results": results}, indent=2))


if __name__ == "__main__":
    main()
