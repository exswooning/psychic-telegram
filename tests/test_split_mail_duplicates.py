"""Two source messages may share one target copy only when they are the same email byte for byte."""
import base64
from types import SimpleNamespace

import split_mail


def _gm(raw: bytes):
    class Call:
        def execute(self):
            return {"raw": base64.urlsafe_b64encode(raw).decode().rstrip("=")}
    msgs = SimpleNamespace(get=lambda **kw: Call())
    src = SimpleNamespace(users=lambda: SimpleNamespace(messages=lambda: msgs))
    return SimpleNamespace(src=src, _retry=lambda f: f())


def test_the_same_email_twice_on_the_source_may_share_one_copy():
    raw = b"Message-ID: <twice@seed.test>\r\nSubject: One Message-ID, twice\r\n\r\nsame id\r\n"
    assert split_mail.same_source_email(_gm(raw), "other", base64.urlsafe_b64encode(raw).decode())


def test_copies_that_differ_only_in_gmails_received_stamp_are_one_email():
    a = (b"Received: by 2002:a05:7000::1; Tue, 17 Nov 2026 11:51:21 -0800\r\nMessage-ID: <t@seed.test>\r\n"
         b"Subject: One Message-ID, twice\r\n\r\nsame id\r\n")
    b = a.replace(b"11:51:21", b"11:51:20")
    assert split_mail.same_source_email(_gm(b), "other", base64.urlsafe_b64encode(a).decode())


def test_a_different_email_never_does():
    a = b"Message-ID: <x@seed.test>\r\n\r\none\r\n"
    b = b"Message-ID: <x@seed.test>\r\n\r\ntwo\r\n"
    assert not split_mail.same_source_email(_gm(b), "other", base64.urlsafe_b64encode(a).decode())
