"""Move a native file that is too big to export, without writing to the source.

A Google Doc or Sheet has no bytes to download -- files.get_media refuses
one outright -- so files.export is the only way out, and Google caps it at
10 MB. files.copy clears the cap because it never exports, but it is a
CREATE made as the SOURCE user, and config.py is explicit that a source
credential which cannot write is "a structural guarantee, not just a
policy".

So two read-only routes were added, cheapest first: a different export
FORMAT, since the ceiling applies to the representation Google builds and
those differ enormously; then the document's own API, which is a paginated
data read with no export ceiling at all.
"""
from __future__ import annotations

import inspect
import re

import drive_engine
import native_api as na


def _code(fn) -> str:
    src = inspect.getsource(fn)
    src = re.sub(r'""".*?"""', "", src, flags=re.S)
    return re.sub(r"#.*$", "", src, flags=re.M)


class TestAlternativeFormatsCoverEveryNativeType:
    def test_docs_sheets_slides_and_drawings_all_have_fallbacks(self):
        for mime in (na.DOC, na.SHEET, na.SLIDES, na.DRAWING):
            assert na.alt_formats(mime), mime

    def test_they_descend_in_fidelity(self):
        """Ordered best-first, so the first one that fits is the most
        faithful one that fits."""
        docs = [note for _m, _e, note in na.alt_formats(na.DOC)]
        assert "OpenDocument" in docs[0]
        assert "PLAIN TEXT" in docs[-1]

    def test_every_format_states_what_it_costs(self):
        """"Migrated" and "migrated as HTML with layout flattened" are
        different claims, and a verification that cannot tell them apart is
        not a verification."""
        for mime in (na.DOC, na.SHEET, na.SLIDES, na.DRAWING):
            for _m, _ext, note in na.alt_formats(mime):
                assert len(note) > 20, (mime, note)

    def test_the_lossiest_sheet_option_says_it_drops_tabs(self):
        """CSV is first-sheet-only. Silently delivering one tab of a
        twelve-tab workbook is the worst outcome available here."""
        notes = " ".join(n for _m, _e, n in na.alt_formats(na.SHEET))
        assert "FIRST SHEET ONLY" in notes


class TestTheExportCascade:
    def test_it_tries_the_preferred_format_first(self):
        src = _code(drive_engine.DriveMigrator._export_within_ceiling)
        assert "[(export_mime, \"\")]" in src

    def test_it_moves_on_only_for_the_ceiling(self):
        """Any other export error is a real failure and must not be masked
        by trying eleven formats against a broken file."""
        src = _code(drive_engine.DriveMigrator._export_within_ceiling)
        assert 'if "exportSizeLimitExceeded" not in str(exc):' in src
        assert "raise" in src

    def test_it_also_catches_an_oversized_success(self):
        """Google's enforcement is not consistent -- it sometimes builds the
        file anyway. Same condition, noticed by us instead of by them."""
        src = _code(drive_engine.DriveMigrator._export_within_ceiling)
        assert "size <= self.settings.export_size_limit" in src

    def test_it_cleans_up_each_rejected_attempt(self):
        """Otherwise a Doc that tries four formats leaves three scratch
        files behind, per file, per user."""
        src = _code(drive_engine.DriveMigrator._export_within_ceiling)
        assert "self._cleanup(path)" in src

    def test_a_degraded_format_is_counted_separately(self):
        """So "500 files migrated" does not quietly include 12 that are now
        flat text."""
        src = _code(drive_engine.DriveMigrator._sync_native)
        assert '_bump("degraded_format")' in src
        assert "fidelity_note" in src


class TestTheApiRebuild:
    def test_it_is_the_last_strategy_tried(self):
        """It rebuilds a document rather than moving one -- the most
        faithful thing available that neither exports nor writes to the
        source, and still less faithful than either path above it."""
        src = _code(drive_engine.DriveMigrator._file_strategies)
        assert src.index("server_side") < src.index("native_api")
        assert src.index("download_upload") < src.index("native_api")

    def test_it_is_offered_only_for_native_files(self):
        """A binary has nothing to rebuild; it downloads with no ceiling."""
        src = _code(drive_engine.DriveMigrator._file_strategies)
        assert "if is_native:" in src

    def test_the_source_is_only_ever_read(self):
        """The entire point. The rebuild runs against the TARGET's
        credential."""
        src = _code(drive_engine.DriveMigrator._sync_native_api)
        assert 'self.auth.api("source"' in src
        assert 'self.auth.api("target"' in src
        # Nothing is created or updated on the source side.
        assert "self.src.files().create" not in src
        assert "self.src.files().update" not in src

    def test_the_scopes_it_needs_are_readonly(self):
        for scope in na.SOURCE_SCOPES:
            assert scope.endswith(".readonly"), scope

    def test_a_type_it_cannot_rebuild_says_so_rather_than_guessing(self):
        """Slides is declined deliberately: batchUpdate can rebuild shapes
        and text but not faithfully enough to claim, and an .odp export is a
        more honest result."""
        assert not na.can_rebuild(na.SLIDES)
        assert "not implemented" in na.API_FIDELITY[na.SLIDES]
        src = _code(drive_engine.DriveMigrator._sync_native_api)
        assert "SKIPPED_UNEXPORTABLE" in src

    def test_sheets_reads_the_grid_not_an_export(self):
        """includeGridData is what makes this a data read -- the 10 MB
        export ceiling does not apply to it at all."""
        src = _code(na.copy_sheet)
        assert "includeGridData=True" in src

    def test_the_source_ids_are_stripped_before_creating(self):
        """A spec carries ids and URLs that name the source document and
        mean nothing in the target; create rejects some outright."""
        spec = {"spreadsheetId": "SRC", "spreadsheetUrl": "https://x",
                "properties": {"title": "t", "spreadsheetId": "SRC"},
                "sheets": [{"properties": {"title": "Tab1"}}]}
        out = na._strip_ids(spec)
        assert "spreadsheetId" not in out
        assert "spreadsheetUrl" not in out
        assert "spreadsheetId" not in out["properties"]
        assert out["sheets"] == spec["sheets"]

    def test_docs_inserts_back_to_front(self):
        """Every insertion shifts the indices after it; walking forwards
        places each element at an offset the previous one invalidated."""
        src = _code(na.copy_doc)
        assert "reversed(" in src

    def test_the_fidelity_of_each_type_is_stated(self):
        for mime in (na.SHEET, na.DOC, na.SLIDES):
            assert len(na.API_FIDELITY[mime]) > 40, mime

    def test_auth_can_reach_those_apis_on_both_tenants(self):
        import auth
        for name in ("sheets", "docs", "slides"):
            assert name in auth._API_VERSIONS, name
        assert hasattr(auth.AuthManager, "api")
