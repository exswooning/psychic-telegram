"""
tests/test_link_rewrite_check.py
================================
A Drive link that was never repointed still WORKS -- right up until the
source tenant is switched off, at which point every one of them dies at
once and there is nothing left to compare against. So the only useful time
to tell a rewritten link from a stale one is before cutover, and the only
honest way is to read the migrated document's own text and see which file
id it actually names.

These pin the two judgements that check_link_rewrite makes: what a given
body text means, and which users are eligible to prove anything at all.
"""

from __future__ import annotations

import check_link_rewrite as clr

SRC = "1s8SpQgcQ2fDAIDo-hjknefp8tHLj_Q6GxRX9R3TBB28"
TGT = "1AAAbbbCCCdddEEEfffGGGhhhIIIjjjKKKlllMMMnnn"


class TestWhatTheTextMeans:
    def test_naming_the_target_file_is_rewritten(self):
        body = f"The numbers are in https://docs.google.com/spreadsheets/d/{TGT}/edit"
        assert clr.verdict(body, SRC, TGT) == clr.REWRITTEN

    def test_still_naming_the_source_file_is_stale(self):
        body = f"The numbers are in https://docs.google.com/spreadsheets/d/{SRC}/edit"
        assert clr.verdict(body, SRC, TGT) == clr.STALE

    def test_no_link_at_all_is_neither(self):
        """Absent is not a pass. A document that lost its link entirely
        would otherwise be counted as rewritten and hide a real loss."""
        assert clr.verdict("Summary\n\nnothing here", SRC, TGT) == clr.ABSENT

    def test_the_target_wins_when_both_ids_appear(self):
        """The fixture links to a Sheet AND a folder, so the source id can
        legitimately survive elsewhere in a correctly rewritten document.
        Testing for the source first would call that document stale."""
        body = (f"See https://docs.google.com/spreadsheets/d/{TGT}/edit\n"
                f"(was {SRC})")
        assert clr.verdict(body, SRC, TGT) == clr.REWRITTEN


class TestWhoCanProveAnything:
    def _manifest(self, **items):
        return {"per_user": [{"user": "a@src.test",
                              "drive": {"items": items}}]}

    def test_a_migrated_user_with_the_fixture_counts(self):
        m = self._manifest(xref_doc="D1", xref_target="S1")
        out = clr.fixtures(m, {"a@src.test": "a@tgt.test"})
        assert len(out) == 1
        assert out[0]["target_user"] == "a@tgt.test"

    def test_a_user_without_the_fixture_is_skipped(self):
        """No linked document means nothing to check -- counting it would
        report a pass for work that never happened."""
        m = self._manifest(root="R1")
        assert clr.fixtures(m, {"a@src.test": "a@tgt.test"}) == []

    def test_a_user_that_has_not_migrated_is_skipped(self):
        m = self._manifest(xref_doc="D1", xref_target="S1")
        assert clr.fixtures(m, {}) == []

    def test_a_manifest_with_no_users_is_not_a_crash(self):
        assert clr.fixtures({}, {"a@src.test": "a@tgt.test"}) == []
