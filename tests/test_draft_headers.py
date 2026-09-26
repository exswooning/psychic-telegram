"""drafts.create reads every raw 8-bit header byte as Latin-1, so a draft with a UTF-8 subject came
back with one more layer of mojibake per copy. Non-ASCII header values now go across as RFC 2047
encoded words, which Gmail keeps intact; everything else goes across exactly as it was."""
import base64
import email
import email.policy

import gmail_engine
from gmail_engine import _ascii_headers
from tests.conftest import SRC_USER, TGT_USER


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode()


def unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def parsed(raw: bytes):
    return email.message_from_bytes(raw, policy=email.policy.default)


ASCII = (b"Message-Id: <a@mail.gmail.com>\r\nFrom: tom@x.com\r\nTo: bob@x.com\r\nSubject: Half-finished\r\n"
         b"MIME-Version: 1.0\r\nContent-Type: text/plain; charset=UTF-8\r\n\r\nStill drafting this.\r\n")


class TestOnlyDamagedHeadersAreTouched:
    def test_an_ascii_draft_is_returned_exactly_as_it_came(self):
        assert _ascii_headers(b64(ASCII)) == b64(ASCII)

    def test_a_draft_with_no_header_separator_is_left_alone(self):
        assert _ascii_headers(b64(b"garbage")) == b64(b"garbage")

    def test_the_body_is_never_touched_even_when_it_is_not_ascii(self):
        raw = ASCII.replace(b"Half-finished", "Über".encode()) .replace(b"Still drafting this.", "Größe — ok".encode())
        out = unb64(_ascii_headers(b64(raw)))
        assert out.split(b"\r\n\r\n", 1)[1] == raw.split(b"\r\n\r\n", 1)[1]


class TestNonAsciiHeadersAreEncoded:
    def test_a_raw_utf8_subject_is_written_as_an_encoded_word_that_reads_the_same(self):
        raw = ASCII.replace(b"Half-finished", "Reply — needs review".encode("utf-8"))
        out = unb64(_ascii_headers(b64(raw)))
        assert out.split(b"\r\n\r\n")[0].isascii(), "a raw 8-bit byte is still in the header block"
        assert str(parsed(out)["subject"]) == "Reply — needs review"

    def test_the_other_headers_are_left_as_they_were(self):
        raw = ASCII.replace(b"Half-finished", "Café".encode("utf-8"))
        out = unb64(_ascii_headers(b64(raw)))
        for h in (b"Message-Id: <a@mail.gmail.com>", b"From: tom@x.com", b"To: bob@x.com", b"MIME-Version: 1.0",
                  b"Content-Type: text/plain; charset=UTF-8"):
            assert h in out

    def test_a_display_name_is_encoded_and_the_address_stays_readable(self):
        raw = ASCII.replace(b"To: bob@x.com", "To: José <jose@x.com>".encode("utf-8"))
        out = unb64(_ascii_headers(b64(raw)))
        to = parsed(out)["to"]
        assert to.addresses[0].display_name == "José" and to.addresses[0].addr_spec == "jose@x.com"

    def test_a_folded_header_is_rewritten_as_one(self):
        raw = ASCII.replace(b"Subject: Half-finished",
                            b"Subject: Long " + "été".encode() + b"\r\n continued here")
        out = unb64(_ascii_headers(b64(raw)))
        assert out.split(b"\r\n\r\n")[0].isascii() and str(parsed(out)["subject"]).startswith("Long été")

    def test_bytes_that_are_not_utf8_are_kept_not_lost(self):
        raw = ASCII.replace(b"Half-finished", b"caf\xe9")
        out = unb64(_ascii_headers(b64(raw)))
        assert str(parsed(out)["subject"]) == "café"

    def test_applying_it_twice_changes_nothing_more(self):
        raw = ASCII.replace(b"Half-finished", "Reply — x".encode("utf-8"))
        once = _ascii_headers(b64(raw))
        assert _ascii_headers(once) == once


class TestTheDraftPass:
    def test_a_migrated_draft_reads_the_same_as_its_source(self, auth, db, settings, identity):
        src = auth.source_gmail(SRC_USER)
        src.add_draft(ASCII.replace(b"Half-finished", "Reply — needs review".encode("utf-8")))
        gmail_engine.GmailMigrator(auth, db, settings, SRC_USER, TGT_USER).run()
        made = next(iter(auth.target_gmail(TGT_USER).drafts.values()))["message"]["raw"]
        raw = unb64(made)
        assert raw.split(b"\r\n\r\n")[0].isascii()
        assert str(parsed(raw)["subject"]) == "Reply — needs review"

    def test_an_ascii_draft_is_created_exactly_as_it_was(self, auth, db, settings, identity):
        src = auth.source_gmail(SRC_USER)
        src.add_draft(ASCII)
        gmail_engine.GmailMigrator(auth, db, settings, SRC_USER, TGT_USER).run()
        assert unb64(next(iter(auth.target_gmail(TGT_USER).drafts.values()))["message"]["raw"]) == ASCII
