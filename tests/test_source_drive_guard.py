"""The source Drive is read-only in code, not only by scope.

A server-side run needs the full `drive` scope on the source (files.copy is a
create call), so Google itself would let it edit or delete any of the user's
files. auth.ReadOnlyDrive refuses every write before Google sees it, except
files.copy into the run's own staging drive on the target.
"""
import pytest

import auth
from auth import ReadOnlyDrive, SourceWriteRefused, allow_copy_into


class _Call:
    def __init__(self, log, what): self.log, self.what = log, what
    def execute(self):
        self.log.append(self.what)
        return {}


class _Files:
    def __init__(self, log): self.log = log
    def __getattr__(self, method):
        return lambda *a, **k: _Call(self.log, ("files", method, k))


class _Drive:
    def __init__(self): self.log, self.flag = [], None
    def files(self): return _Files(self.log)
    def permissions(self): return _Files(self.log)
    def comments(self): return _Files(self.log)
    def helper(self): return "fake helper"


@pytest.mark.parametrize("method", ["get", "list", "export_media", "get_media"])
def test_reads_go_through(method):
    raw = _Drive()
    getattr(ReadOnlyDrive(raw).files(), method)(fileId="f").execute()
    assert raw.log[0][1] == method


@pytest.mark.parametrize("coll,method", [("files", "update"), ("files", "delete"),
                                         ("files", "create"), ("files", "emptyTrash"),
                                         ("files", "watch"), ("permissions", "create"),
                                         ("permissions", "delete"), ("comments", "create")])
def test_every_write_is_refused_before_google_sees_it(coll, method):
    raw = _Drive()
    with pytest.raises(SourceWriteRefused):
        getattr(getattr(ReadOnlyDrive(raw, copy_into="STAGING"), coll)(), method)(fileId="f")
    assert raw.log == []


def test_a_copy_goes_through_only_into_the_staging_drive():
    raw = _Drive()
    src = ReadOnlyDrive(raw, copy_into="STAGING")
    src.files().copy(fileId="f", body={"parents": ["STAGING"]}).execute()
    assert raw.log[0][1] == "copy"
    with pytest.raises(SourceWriteRefused):
        src.files().copy(fileId="f", body={"parents": ["SOMEWHERE-ELSE"]})
    with pytest.raises(SourceWriteRefused):
        src.files().copy(fileId="f", body={})
    assert len(raw.log) == 1


def test_without_a_staging_drive_even_a_copy_is_refused():
    raw = _Drive()
    with pytest.raises(SourceWriteRefused):
        ReadOnlyDrive(raw).files().copy(fileId="f", body={"parents": ["STAGING"]})
    assert raw.log == []


def test_it_is_transparent_for_anything_that_is_not_a_drive_call():
    raw = _Drive()
    guarded = ReadOnlyDrive(raw)
    assert guarded.helper() == "fake helper"
    guarded.flag = "set"
    assert raw.flag == "set"


def test_allowing_a_copy_keeps_everything_else_refused():
    raw = _Drive()
    src = allow_copy_into(ReadOnlyDrive(raw), "STAGING")
    src.files().copy(fileId="f", body={"parents": ["STAGING"]}).execute()
    with pytest.raises(SourceWriteRefused):
        src.files().delete(fileId="f")
    assert allow_copy_into(raw, "STAGING") is raw        # an explicitly writable client


class TestAuthManagerHandsOutTheGuard:
    def _auth(self, raw):
        am = object.__new__(auth.AuthManager)
        am._service = lambda tenant, api, user: raw
        return am

    def test_read_only_by_default(self):
        raw = _Drive()
        assert isinstance(self._auth(raw).source_drive("u"), ReadOnlyDrive)

    def test_writable_only_when_asked(self):
        raw = _Drive()
        assert self._auth(raw).source_drive("u", writable=True) is raw


class TestTheEngine:
    def test_a_server_side_engine_may_copy_into_its_staging_drive_and_nothing_else(
            self, auth, db, settings):
        import drive_engine
        settings.transfer_mode = "server_side"
        m = drive_engine.DriveMigrator(auth, db, settings, "u@tenanta.com", "u@tenantb.com",
                                       type("Q", (), {"reserve": lambda s, n: None,
                                                      "refund": lambda s, n: None})())
        m._staging_drive_id = "STAGING"
        assert isinstance(m.src, ReadOnlyDrive) and m.src._copy_into == "STAGING"
        with pytest.raises(SourceWriteRefused):
            m.src.files().update(fileId="x", body={})

    def test_only_link_flip_gets_a_writable_source(self, auth, db, settings):
        import drive_engine
        settings.transfer_mode = "link_flip"
        m = drive_engine.DriveMigrator(auth, db, settings, "u@tenanta.com", "u@tenantb.com",
                                       type("Q", (), {"reserve": lambda s, n: None,
                                                      "refund": lambda s, n: None})())
        assert not isinstance(m.src, ReadOnlyDrive)
