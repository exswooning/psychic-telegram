"""A conversation lands as one conversation.

Gmail regrouped copied mail from its headers -- close, but not guaranteed to
match. Each message after a thread's first now names the target thread that
first copy started, so the grouping is the source's exactly.
"""
from __future__ import annotations

from tests.conftest import SRC_USER, TGT_USER

A = (b"Message-ID: <t1@tenanta.com>\r\nSubject: plan\r\nDate: Mon, 1 Jan 2024 10:00:00 +0000"
     b"\r\n\r\nfirst\r\n")
B = (b"Message-ID: <t2@tenanta.com>\r\nIn-Reply-To: <t1@tenanta.com>\r\nSubject: Re: plan\r\n"
     b"Date: Mon, 1 Jan 2024 11:00:00 +0000\r\n\r\nsecond\r\n")
C = (b"Message-ID: <u1@tenanta.com>\r\nSubject: other\r\nDate: Mon, 1 Jan 2024 12:00:00 +0000"
     b"\r\n\r\nalone\r\n")


def test_messages_of_one_thread_share_one_target_thread(gmail_migrator, auth, db):
    src = auth.source_gmail(SRC_USER)
    src.add_message(A, thread_id="thr-src")
    src.add_message(B, thread_id="thr-src")
    src.add_message(C, thread_id="thr-other")
    gmail_migrator.run()
    tgt = auth.target_gmail(TGT_USER)
    by_thread = {}
    for m in tgt.messages.values():
        by_thread.setdefault(m["threadId"], []).append(m["id"])
    assert sorted(len(v) for v in by_thread.values()) == [1, 2]
    assert db.get_target_id(SRC_USER, "thr-src", "thread")


def test_a_thread_gone_from_the_target_still_gets_the_message(gmail_migrator, auth, db):
    src = auth.source_gmail(SRC_USER)
    src.add_message(B, thread_id="thr-src")
    db.record_mapping(SRC_USER, "thr-src", "deleted-thread", "thread")
    gmail_migrator.run()
    tgt = auth.target_gmail(TGT_USER)
    assert tgt.call_count("messages.insert") == 2       # tried the thread, then alone
    assert len(tgt.messages) == 1
