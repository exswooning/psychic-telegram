"""A migrated Sheet kept reading from the source tenant.

Cell values survive migration -- a Sheet is exported to .xlsx and uploaded
with the native mimeType, so Drive converts it back. What did not survive
is any reference BY ID: IMPORTRANGE, a cell HYPERLINK, a Doc's link. The
target file has a new id, so those still name the source file.

That is worse than breaking. It looks correct, keeps working, and silently
reads from the tenant you are about to tear down.

Gmail and Calendar have rewritten Drive links since they were written
(rewrite_raw, rewrite_text). Drive files themselves did not, because the
content is inside an OOXML package rather than a body string.
"""
from __future__ import annotations

import inspect
import io
import re
import zipfile

import link_rewrite as lr

SRC = "1AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
TGT = "9ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ"
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4


def _lookup(i: str) -> str | None:
    return TGT if i == SRC else None


def _xlsx(*formulas: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/worksheets/sheet1.xml",
                   "".join(f"<f>{f}</f>" for f in formulas).encode())
        z.writestr("xl/media/image1.png", PNG)
    return buf.getvalue()


class TestEveryFormOfReference:
    def test_a_bare_id_in_importrange(self):
        """Sheets accepts IMPORTRANGE("<id>") with no URL, and the editor
        offers that form -- but DRIVE_ID requires the host on purpose,
        because matching a bare id made base64 a minefield. Anchoring on
        the function name is what makes this one safe."""
        out, hits = lr.rewrite_zip(_xlsx(f'IMPORTRANGE("{SRC}","A1")'), _lookup)
        assert hits == 1
        # Read through the zip: the parts are deflated, so a substring
        # check on the package finds neither id.
        with zipfile.ZipFile(io.BytesIO(out)) as z:
            sheet = z.read("xl/worksheets/sheet1.xml")
        assert TGT.encode() in sheet and SRC.encode() not in sheet

    def test_a_full_url_in_importrange(self):
        out, hits = lr.rewrite_zip(_xlsx(
            f'IMPORTRANGE("https://docs.google.com/spreadsheets/d/{SRC}/edit","A1")'),
            _lookup)
        assert hits == 1
        with zipfile.ZipFile(io.BytesIO(out)) as z:
            assert SRC.encode() not in z.read("xl/worksheets/sheet1.xml")

    def test_a_cell_hyperlink(self):
        out, hits = lr.rewrite_zip(_xlsx(
            f'HYPERLINK("https://drive.google.com/file/d/{SRC}/view","x")'), _lookup)
        assert hits == 1
        with zipfile.ZipFile(io.BytesIO(out)) as z:
            assert SRC.encode() not in z.read("xl/worksheets/sheet1.xml")

    def test_all_three_at_once(self):
        data = _xlsx(f'IMPORTRANGE("{SRC}","A1")',
                     f'IMPORTRANGE("https://docs.google.com/spreadsheets/d/{SRC}/edit","B1")',
                     f'HYPERLINK("https://drive.google.com/file/d/{SRC}/view","x")')
        out, hits = lr.rewrite_zip(data, _lookup)
        assert hits == 3
        with zipfile.ZipFile(io.BytesIO(out)) as z:
            assert z.read("xl/worksheets/sheet1.xml").count(TGT.encode()) == 3


class TestItDoesNotDamageThePackage:
    def test_media_is_byte_identical(self):
        """Rewriting inside a PNG could corrupt it for no possible gain, and
        the id pattern can match image entropy by chance."""
        out, _ = lr.rewrite_zip(_xlsx(f'IMPORTRANGE("{SRC}","A1")'), _lookup)
        with zipfile.ZipFile(io.BytesIO(out)) as z:
            assert z.read("xl/media/image1.png") == PNG

    def test_the_result_is_still_a_valid_zip(self):
        out, _ = lr.rewrite_zip(_xlsx(f'IMPORTRANGE("{SRC}","A1")'), _lookup)
        assert zipfile.ZipFile(io.BytesIO(out)).testzip() is None

    def test_an_unmapped_id_is_left_exactly_alone(self):
        """An unmapped id is a file that did not move. Inventing a target
        for it would break a link that currently works -- and returning the
        same object lets the caller skip an upload entirely."""
        data = _xlsx(f'IMPORTRANGE("{SRC}","A1")')
        out, hits = lr.rewrite_zip(data, lambda i: None)
        assert hits == 0
        assert out is data

    def test_a_non_package_export_still_works(self):
        """A Drawing exports as PNG, not a zip."""
        raw = f'see https://drive.google.com/file/d/{SRC}/view'.encode()
        out, hits = lr.rewrite_zip(raw, _lookup)
        assert hits == 1 and TGT.encode() in out


class TestTheCheapGate:
    def test_it_finds_both_reference_forms(self):
        assert lr.has_drive_ref(_xlsx(f'IMPORTRANGE("{SRC}","A1")'))
        assert lr.has_drive_ref(_xlsx(
            f'HYPERLINK("https://drive.google.com/file/d/{SRC}/view","x")'))

    def test_it_is_false_for_a_plain_file(self):
        assert not lr.has_drive_ref(_xlsx('SUM(A1:A9)'))

    def test_it_ignores_whether_the_id_is_mapped(self):
        """At the moment this is asked, the referenced file may not have
        migrated yet -- that is the whole reason the rewrite is a post-pass."""
        assert lr.has_drive_ref(_xlsx(f'IMPORTRANGE("{SRC}","A1")'))


class TestTheEngineRunsItAtTheRightTime:
    def _src(self, name):
        import drive_engine
        return re.sub(r"#.*$", "",
                      inspect.getsource(getattr(drive_engine.DriveMigrator, name)),
                      flags=re.M)

    def test_the_rewrite_is_queued_not_done_inline(self):
        """A Sheet may reference a document that has not migrated yet -- a
        formula pointing at next week's folder is ordinary -- so the mapping
        it needs does not exist until the tree is finished."""
        src = self._src("_sync_native")
        assert "_pending_link_rewrites.append" in src
        assert "rewrite_zip" not in src

    def test_it_runs_after_the_pool_and_the_shortcuts(self):
        """Both can still add mappings; a rewrite is only correct when no
        more are coming."""
        src = self._src("run")
        assert src.index("_drain_file_pool") < src.index("_rewrite_pending_links")
        assert src.index("_fixup_shortcuts") < src.index("_rewrite_pending_links")

    def test_it_looks_up_across_owners(self):
        """target_for_source_id, not get_target_id(self.source_user, ...): a
        Sheet importing a colleague's document is the common case, and
        scoping the lookup to this user would call it unmapped."""
        src = self._src("_rewrite_pending_links")
        assert "self.db.target_for_source_id" in src

    def test_a_failed_rewrite_does_not_lose_the_file(self):
        """The file itself migrated. Losing the whole user over a stale link
        would be the worse trade."""
        src = self._src("_rewrite_pending_links")
        assert '"link_rewrite",\n                                  "FAILED"' in src \
            or '"FAILED"' in src
        assert "raise" not in src

    def test_nothing_is_uploaded_when_nothing_changed(self):
        src = self._src("_rewrite_pending_links")
        assert "if not hits:" in src
        assert src.index("if not hits:") < src.index("files().update")


class TestCommentsTravelByDefault:
    def test_the_default_is_on(self):
        """A comment thread is the argument that produced the document.
        Delivering the file and losing the reasoning is not noticed until
        somebody goes looking for a decision months later."""
        from config import Settings
        assert Settings().migrate_comments is True

    def test_it_can_still_be_turned_off(self, monkeypatch):
        monkeypatch.setenv("MIGRATE_COMMENTS", "0")
        from config import Settings
        assert Settings().migrate_comments is False
