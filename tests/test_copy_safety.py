"""copy_safety.py: the server-side copy safety test. Fakes only; nothing here reaches Google."""
import types

import pytest

import copy_safety as C
from auth import ReadOnlyDrive

G = "application/vnd.google-apps."
USER = "u@src.test"


class _Req:
    def __init__(self, fn):
        self.fn = fn

    def execute(self, **_):
        return self.fn()


class Src:
    """A source Drive. Records every call; `drops_copy_of` mimes Google's Form behaviour."""

    def __init__(self, files, drops_copy_of=()):
        self.files_ = {f["id"]: dict(f, version="1", modifiedTime="t0", trashed=False) for f in files}
        self.calls, self.drops, self.n = [], set(drops_copy_of), 0

    def files(self):
        return self

    def list(self, **kw):
        self.calls.append("files.list")
        return _Req(lambda: {"files": [dict(f) for f in self.files_.values()]})

    def copy(self, fileId, body=None, **kw):
        self.calls.append("files.copy")

        def go():
            f = self.files_[fileId]
            if f["mimeType"] in self.drops:
                self.n += 1
                self.files_[f"x{self.n}"] = dict(f, id=f"x{self.n}", name="Copy of " + f["name"],
                                                 version="1", modifiedTime="t1")
            return {"id": "c-" + fileId}
        return _Req(go)

    def update(self, **kw):
        self.calls.append("files.update")
        return _Req(lambda: {})

    def __getattr__(self, name):       # any other write would land here and be refused by ReadOnlyDrive
        raise AssertionError(f"unexpected source call {name}")


class Tgt:
    def __init__(self):
        self.calls, self.drives_ = [], {}

    def drives(self):
        return self

    def permissions(self):
        return self

    def create(self, **kw):
        self.calls.append(("create", kw))
        if "requestId" in kw:
            self.drives_["d1"] = kw["body"]["name"]
            return _Req(lambda: {"id": "d1"})
        return _Req(lambda: {"id": "p1"})

    def list(self, **kw):
        return _Req(lambda: {"permissions": [{"id": "p1", "emailAddress": USER},
                                             {"id": "p0", "emailAddress": "admin@tgt.test"}]})

    def delete(self, **kw):
        self.calls.append(("delete", kw))
        if "driveId" in kw:
            self.drives_.pop(kw["driveId"])
        return _Req(lambda: {})


class FakeAuthCS:
    def __init__(self, src, tgt, ok=True):
        self.src, self.tgt, self.ok = src, tgt, ok

    def verify_delegation(self, tenant, user):
        return (True, "ok") if self.ok else (False, "unauthorized_client")

    def source_drive(self, user):
        return ReadOnlyDrive(self.src)

    def target_drive(self, user):
        return self.tgt

    def api(self, *a):       # no forms API: _form_sheets is stubbed below
        raise AssertionError


def settings(domain="src.test", tmp=None):
    return types.SimpleNamespace(source_domain=domain, target_admin="admin@tgt.test", account_id=99)


FILES = [{"id": "d", "name": "Doc", "mimeType": G + "document"},
         {"id": "f", "name": "Form", "mimeType": FORM_MIME if (FORM_MIME := G + "form") else ""},
         {"id": "p", "name": "Pdf", "mimeType": "application/pdf"}]


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setattr(C, "_form_sheets", lambda *a: set())
    monkeypatch.setattr(C, "audit_events", lambda *a, **k: [])
    monkeypatch.setattr(C.os.path, "dirname", lambda p: str(tmp_path))      # report lands in tmp


def _run(src, tgt=None, **kw):
    tgt = tgt or Tgt()
    out = C.run(settings(), FakeAuthCS(src, tgt), USER, 2, 0, kw.pop("apply", True), sleep=lambda s: None,
                say=lambda m: None, **kw)
    return out, tgt


def test_government_source_is_refused():
    src = Src(FILES)
    for dom in ("dharan.gov.np", "x.gov"):
        with pytest.raises(SystemExit):
            C.run(settings(dom), FakeAuthCS(src, Tgt()), USER, apply=True, say=lambda m: None)
    assert src.calls == []                                        # nothing was even listed


def test_unminted_drive_scope_is_refused():
    src = Src(FILES)
    with pytest.raises(SystemExit):
        C.run(settings(), FakeAuthCS(src, Tgt(), ok=False), USER, apply=True, say=lambda m: None)
    assert src.calls == []


def test_dry_run_copies_nothing_and_makes_no_drive():
    src, tgt = Src(FILES), Tgt()
    out, _ = _run(src, tgt, apply=False)
    assert out["dryRun"] and [s["name"] for s in out["samples"]["doc"]] == ["Doc"]
    assert "files.copy" not in src.calls and tgt.calls == []


def test_a_form_copy_that_drops_copy_of_is_unsafe_and_a_doc_is_safe_and_source_is_untouched():
    src = Src(FILES, drops_copy_of={G + "form"})
    out, tgt = _run(src)
    assert out["verdicts"]["form"] == "UNSAFE" and out["verdicts"]["doc"] == "SAFE"
    assert out["verdicts"]["script"] == "no sample" and out["overall"] == "UNSAFE"
    assert [a["name"] for a in out["attributed"]["f"]] == ["Copy of Form"]
    assert "x1" in src.files_                                     # reported, and left alone
    assert set(src.calls) == {"files.list", "files.copy"}         # no other source call, ever
    assert src.calls.count("files.copy") == 3


def test_clean_run_is_safe_and_cleanup_touches_only_the_target_staging_drive():
    src, tgt = Src(FILES), Tgt()
    out, _ = _run(src, tgt)
    assert out["overall"] == "SAFE"
    deletes = [kw for n, kw in tgt.calls if n == "delete"]
    assert [d.get("permissionId") for d in deletes[:-1]] == ["p1"]     # only the source user's membership
    assert deletes[-1] == {"driveId": "d1", "useDomainAdminAccess": True, "allowItemDeletion": True}
    assert tgt.drives_ == {} and out["cleanup"]["ok"]
    assert all(c in ("files.list", "files.copy") for c in src.calls)


def test_a_failed_copy_is_unknown_and_unattributed_change_blocks_safe(monkeypatch):
    src = Src(FILES)
    monkeypatch.setattr(src, "copy", lambda **k: (_ for _ in ()).throw(RuntimeError("no")), raising=False)
    out, _ = _run(src)
    assert out["verdicts"]["doc"] == "UNKNOWN" and out["overall"] == "UNKNOWN"
    src2 = Src(FILES)
    orig = src2.copy
    src2.copy = lambda fileId, body=None, **k: (src2.files_.__setitem__("z", {"id": "z", "name": "Other",
                  "mimeType": "text/plain", "version": "1", "modifiedTime": "t", "trashed": False}), orig(fileId, body))[1]
    out2, _ = _run(src2)
    assert out2["unattributed"] and out2["overall"] == "UNSAFE"


def test_the_source_guard_refuses_any_other_write():
    from auth import SourceWriteRefused
    s = ReadOnlyDrive(Src(FILES), "d1")
    with pytest.raises(SourceWriteRefused):
        s.files().copy(fileId="d", body={"parents": ["elsewhere"]})
    with pytest.raises(SourceWriteRefused):
        s.files().update(fileId="d", body={"name": "x"})


@pytest.fixture
def signed_in(monkeypatch, tmp_path):
    import os
    import tempfile

    from fastapi.testclient import TestClient

    import api_server
    import control_plane_db as cpdb
    from db import MigrationDB
    monkeypatch.setenv("MIGRATION_DB", tempfile.mktemp(suffix=".db"))
    monkeypatch.setenv("BITPORT_SIGNUP_OPEN", "1")
    MigrationDB(os.environ["MIGRATION_DB"])
    cpdb.apply_migrations()
    monkeypatch.setattr(api_server, "HERE", str(tmp_path))
    with TestClient(api_server.app) as c:
        assert c.post("/api/v2/auth/signup", json={"email": "cs@example.com", "password": "hunter22222",
                                                   "name": "Tester"}).status_code == 200
        yield c, c.get("/api/v2/auth/me").json()["id"], tmp_path


def test_latest_is_null_then_the_newest_report(signed_in):
    c, aid, root = signed_in
    assert c.get("/api/v2/copy-safety/latest").json() is None
    d = root / "logs" / str(aid)
    d.mkdir(parents=True)
    (d / "copy-safety-20261001T000000Z.json").write_text('{"overall": "SAFE"}')
    (d / "copy-safety-20261002T000000Z.json").write_text('{"overall": "UNSAFE"}')
    assert c.get("/api/v2/copy-safety/latest").json()["overall"] == "UNSAFE"
