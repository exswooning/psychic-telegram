"""
tests/test_seeded_links_are_rewritable.py
=========================================
The seeder writes Drive links into mail, calendar and chat; the migrator
rewrites them so a migrated message still reaches its file. Both halves are
tested on their own, and neither test knows about the other -- so a link
shape the seeder emits and the rewriter does not match would pass both
suites and fail only in production, silently, as mail that arrives with
links to a tenant that is being deleted.

This runs the shapes the seeder ACTUALLY produces through the real
rewriter. It is the join nobody was checking.

A Drive URL names a file by id and nothing else -- there is no domain in
docs.google.com/document/d/<id>/edit -- and files.copy mints a new id. So
every link inside migrated mail still names the SOURCE file, and the day
the source tenant is deleted they all 404 while a good copy sits on the
target under an id nothing points at.
"""

from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "data-generator"))

import link_rewrite                      # noqa: E402
import seed_sandbox as ss                # noqa: E402

SRC = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"
DST = "9ZyXwVuTsRqPoNmLkJiHgFeDcBa987654"


def lookup(source_id: str) -> str | None:
    return DST if source_id == SRC else None


class TestEveryShapeTheSeederWritesIsRewritten:
    @pytest.mark.parametrize("shape", range(4))
    def test_the_four_drive_link_shapes(self, shape):
        body = ss._drive_link(SRC, shape)
        out, hits = link_rewrite.rewrite_text(body, lookup)
        assert hits == 1, f"shape {shape} was not recognised: {body}"
        assert DST in out and SRC not in out

    def test_the_oversized_attachment_link(self):
        """The >25 MB case: Gmail uploads to Drive and sends a link with
        ?usp=drive_web. That is the one shape a real tenant produces in
        bulk, and the one whose rewrite decides whether migrated mail still
        reaches its attachments."""
        body = ss._big_attachment_body(SRC)
        out, hits = link_rewrite.rewrite_text(body, lookup)
        assert hits == 1, f"the converted-attachment shape was missed: {body}"
        assert DST in out

    def test_the_html_bodies(self):
        """In an HTML part the query separator arrives entity-encoded as
        &amp;, and a pattern matching only a bare & misses every HTML
        mail."""
        html = ss._linked_html(SRC)
        out, hits = link_rewrite.rewrite_text(html, lookup)
        assert hits >= 1, f"no link found in {html}"
        assert DST in out

    def test_the_plain_linked_body(self):
        for shape in range(4):
            body = ss._linked_body(SRC, shape)
            _out, hits = link_rewrite.rewrite_text(body, lookup)
            assert hits == 1, f"shape {shape}: {body}"


class TestItLeavesAloneWhatItShould:
    def test_an_unknown_id_is_untouched(self):
        """Links to files outside the migration -- another tenant, a
        deleted file, a drive we skipped -- must not be mangled into
        something worse than a dead link."""
        other = "0OtherIdOtherIdOtherIdOtherId00"
        body = ss._drive_link(other, 0)
        out, hits = link_rewrite.rewrite_text(body, lookup)
        assert hits == 0 and out == body

    def test_a_body_with_no_link_comes_back_identical(self):
        body = "Morning -- where did that land?"
        out, hits = link_rewrite.rewrite_text(body, lookup)
        assert hits == 0 and out == body

    def test_base64_is_not_mistaken_for_a_link(self):
        """The base64 alphabet contains "/", so an attachment blob
        reliably produces "/d/" followed by twenty-plus base64 characters.
        Matching a bare "/d/" read those as Drive ids -- measured on a real
        mailbox, 13 of 21 "link-bearing" messages had no Drive link at
        all."""
        blob = "SGVsbG8vZC9BQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE"
        out, hits = link_rewrite.rewrite_text(blob, lookup)
        assert hits == 0 and out == blob


class TestTheSeededChatBodiesToo:
    def test_chat_lines_carry_rewritable_links(self):
        """Chat is seeded with Drive links on purpose -- "here's the doc"
        is most of what Chat is for -- and nothing rewrites Chat yet. This
        records that the links it writes ARE the rewritable shape, so the
        gap is the engine's, not the corpus's."""
        body = ss._drive_link(SRC, 2)
        _out, hits = link_rewrite.rewrite_text(body, lookup)
        assert hits == 1
