"""Say what will not migrate before the run, not in an audit log after it.

Every limitation in this tool was already discoverable -- SKIPPED_EXPORT_
TOO_LARGE, SKIPPED_UNEXPORTABLE, SKIPPED_NO_DOWNLOAD -- all true, all
recorded, none of them known at the point where somebody could have acted.
"3 files need files.copy" and "3,000 files need it" are the same message
today and completely different decisions.
"""
from __future__ import annotations

import preflight as pf
from config import EXPORT_MIME_MAP

G = "application/vnd.google-apps."
CEIL = 10 * 1024 * 1024


def _c(item):
    return pf.classify(item, EXPORT_MIME_MAP, CEIL)[0]


class TestClassification:
    def test_a_binary_file_is_fine(self):
        assert _c({"mimeType": "image/jpeg", "size": "900"}) == pf.FINE

    def test_a_small_native_is_fine(self):
        assert _c({"mimeType": G + "document",
                   "quotaBytesUsed": "50000"}) == pf.FINE

    def test_a_form_needs_copy(self):
        """No export representation at all -- files.copy is the only route."""
        assert _c({"mimeType": G + "form"}) == pf.NEEDS_COPY

    def test_a_site_needs_copy(self):
        assert _c({"mimeType": G + "site"}) == pf.NEEDS_COPY

    def test_an_oversized_native_needs_copy(self):
        assert _c({"mimeType": G + "spreadsheet",
                   "quotaBytesUsed": str(45 * 10**6)}) == pf.NEEDS_COPY

    def test_an_undownloadable_file_cannot_migrate_by_any_route(self):
        """canDownload=false is the source saying this user may not have the
        bytes. Another route would override that decision rather than
        recover from an error -- drive_engine treats it the same way."""
        assert _c({"mimeType": "application/pdf",
                   "capabilities": {"canDownload": False}}) == pf.CANNOT

    def test_folders_and_shortcuts_are_structure_not_content(self):
        assert _c({"mimeType": pf.FOLDER}) == pf.FINE
        assert _c({"mimeType": pf.SHORTCUT}) == pf.FINE

    def test_an_unknown_native_type_is_assumed_unexportable(self):
        """Safer direction to be wrong in: a type with no mapping cannot be
        exported, whether or not this list has heard of it."""
        assert _c({"mimeType": G + "somethingnew"}) == pf.NEEDS_COPY


class TestTheReportDrivesADecision:
    def _summary(self):
        items = [
            {"name": "ok", "mimeType": G + "document", "quotaBytesUsed": "5"},
            {"name": "huge", "mimeType": G + "spreadsheet",
             "quotaBytesUsed": str(45 * 10**6)},
            {"name": "Survey", "mimeType": G + "form"},
            {"name": "locked", "mimeType": "application/pdf",
             "capabilities": {"canDownload": False}},
        ]
        return pf.summarise(items, EXPORT_MIME_MAP, CEIL)

    def test_read_only_says_they_will_be_skipped(self):
        text = pf.report(self._summary(), can_copy=False)
        assert "will be SKIPPED" in text
        assert "auth/drive" in text          # and what to change

    def test_with_the_scope_it_says_they_are_covered(self):
        text = pf.report(self._summary(), can_copy=True)
        assert "FALL BACK to files.copy" in text
        assert "will be SKIPPED" not in text

    def test_it_separates_skippable_from_impossible(self):
        """Granting a scope fixes one group and not the other. Reporting a
        single number would hide that."""
        s = self._summary()
        assert s["counts"][pf.NEEDS_COPY] == 2
        assert s["counts"][pf.CANNOT] == 1

    def test_it_names_examples_so_the_count_can_be_checked(self):
        text = pf.report(self._summary(), can_copy=False)
        assert "huge" in text and "Survey" in text

    def test_a_clean_tenant_says_so_plainly(self):
        s = pf.summarise([{"name": "a", "mimeType": "image/png"}],
                         EXPORT_MIME_MAP, CEIL)
        assert "nothing would be skipped" in pf.report(s, can_copy=False)

    def test_the_ceiling_estimate_is_labelled_as_one(self):
        """A native file reports no size, so this cannot be a fact -- the
        only way to know for certain is to export, which is the run."""
        _, reason = pf.classify({"mimeType": G + "spreadsheet",
                                 "quotaBytesUsed": str(45 * 10**6)},
                                EXPORT_MIME_MAP, CEIL)
        assert "estimated" in reason


class TestItStatesTheUnavoidableLosses:
    def test_the_list_is_there(self):
        """A migration that cannot enumerate its own known losses cannot
        honestly be signed off."""
        for phrase in ("Revision history", "Apps Script", "original creator",
                       "URL", "Group settings"):
            assert phrase in pf.ALWAYS_LOST, phrase

    def test_it_distinguishes_losses_from_skips(self):
        assert "not skips" in pf.ALWAYS_LOST

    def test_it_says_bound_scripts_leave_no_trace(self):
        """The worst kind of loss: nothing detects one, so there is no skip
        row to find afterwards."""
        assert "no skip row" in pf.ALWAYS_LOST

    def test_it_is_printed_by_the_run(self):
        import inspect
        assert "ALWAYS_LOST" in inspect.getsource(pf.main)


class TestCreatedTimeIsPreserved:
    def test_it_is_requested_in_the_listing(self):
        from config import DRIVE_FILE_FIELDS
        assert "createdTime" in DRIVE_FILE_FIELDS

    def test_both_upload_paths_set_it(self):
        """Without it every migrated file claims to have been created on
        migration day, which breaks sort-by-created and any retention
        reasoning that starts from a file's age."""
        import inspect

        import drive_engine
        for name in ("_sync_native", "_sync_binary"):
            fn = getattr(drive_engine.DriveMigrator, name, None)
            if fn is None:
                continue
            src = inspect.getsource(fn)
            assert 'body["createdTime"]' in src, name
