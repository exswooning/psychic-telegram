"""Leftover server-side staging drives on the target are cleaned up by every repair:
dharan (2026-10-23) had fifteen, each listed in the source AND target user's Shared
drives, five holding a never-moved copy. Target writes only; never another account's."""
import repair
from db import bulk_seed_identities


class _Exec:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class _Tgt:
    """One fake target tenant: shared drives, their files and their members."""

    def __init__(self):
        self.drives_ = {}       # id -> {"name", "files": {id: meta}, "perms": [{id, emailAddress}]}
        self.files_ = {}        # My Drive files by id
        self.log = []

    def drives(self):
        t = self

        class D:
            def list(self, **k):
                return _Exec(lambda: {"drives": [{"id": i, "name": d["name"]} for i, d in t.drives_.items()]})

            def delete(self, driveId, **k):
                def go():
                    assert not t.drives_[driveId]["files"], "deleted a drive with files in it"
                    t.log.append(("drive-deleted", t.drives_.pop(driveId)["name"]))
                return _Exec(go)
        return D()

    def files(self):
        t = self

        class F:
            def list(self, driveId, **k):
                return _Exec(lambda: {"files": list(t.drives_[driveId]["files"].values())})

            def get(self, fileId, **k):
                return _Exec(lambda: t.files_[fileId])

            def delete(self, fileId, **k):
                def go():
                    for d in t.drives_.values():
                        if d["files"].pop(fileId, None):
                            t.log.append(("file-deleted", fileId))
                return _Exec(go)
        return F()

    def permissions(self):
        t = self

        class P:
            def list(self, fileId, **k):
                return _Exec(lambda: {"permissions": list(t.drives_[fileId]["perms"])})

            def delete(self, fileId, permissionId, **k):
                def go():
                    t.drives_[fileId]["perms"] = [p for p in t.drives_[fileId]["perms"] if p["id"] != permissionId]
                    t.log.append(("member-removed", permissionId))
                return _Exec(go)
        return P()


def test_every_case(db, settings):
    settings.source_domain, settings.target_domain = "dharan.example", "mdharan.example"
    settings.target_admin, settings.staging_drive_prefix = "admin@mdharan.example", "MIGRATION-STAGING"
    bulk_seed_identities(db, [(f"{u}@dharan.example", f"{u}@mdharan.example") for u in ("ward4", "ward6", "ward9", "info")])
    t = _Tgt()
    members = lambda u: [{"id": "p-src", "emailAddress": f"{u}@dharan.example"},     # noqa: E731
                         {"id": "p-tgt", "emailAddress": f"{u}@mdharan.example"}]
    t.files_["mapped-twin"] = {"id": "mapped-twin", "mimeType": "image/jpeg", "md5Checksum": "aa", "size": "3"}
    db.record_mapping("ward4@dharan.example", "SRC-JPEG", "mapped-twin", "file", source_name="IMG.jpeg")
    db.record_mapping("info@dharan.example", "SRC-STUCK", "stuck-copy", "file", source_name="stuck.pdf")
    t.drives_ = {
        "d-empty": {"name": "MIGRATION-STAGING-ward6", "files": {}, "perms": members("ward6")},
        "d-orphan": {"name": "MIGRATION-STAGING-ward4", "perms": members("ward4"),
                     "files": {"orphan": {"id": "orphan", "name": "IMG.jpeg", "mimeType": "image/jpeg", "md5Checksum": "aa"}}},
        "d-unknown": {"name": "MIGRATION-STAGING-ward9", "perms": members("ward9"),
                      "files": {"x": {"id": "x", "name": "no twin.pdf", "mimeType": "application/pdf", "md5Checksum": "zz"}}},
        "d-stuck": {"name": "MIGRATION-STAGING-info", "perms": members("info"),
                    "files": {"stuck-copy": {"id": "stuck-copy", "name": "stuck.pdf", "mimeType": "application/pdf"}}},
        "d-other-account": {"name": "MIGRATION-STAGING-someoneelse", "files": {}, "perms": []},
        "d-real": {"name": "लोकसेवा", "files": {}, "perms": []},
    }
    auth = type("A", (), {"target_drive": lambda self, u: t})()
    out = repair.clean_staging_drives(auth, db, settings, apply=True)
    assert out["errors"] == []
    assert set(t.drives_) == {"d-unknown", "d-stuck", "d-other-account", "d-real"}
    assert ("file-deleted", "orphan") in t.log and ("file-deleted", "x") not in t.log
    assert all(p["emailAddress"].endswith("@mdharan.example") for d in ("d-unknown", "d-stuck")
               for p in t.drives_[d]["perms"])                 # gone from dharan's lists even when kept
    assert (out["deleted_drives"], out["deleted_copies"], len(out["kept"])) == (2, 1, 2)
    rows = {r["status"] for r in db.conn.execute("SELECT status FROM audit_log WHERE item_type='staging_cleanup'")}
    assert rows == {"DELETED_ORPHAN_COPY", "MEMBER_REMOVED", "DRIVE_DELETED"}


def test_a_survey_changes_nothing(db, settings):
    settings.source_domain, settings.target_admin, settings.staging_drive_prefix = "dharan.example", "a@m", "MIGRATION-STAGING"
    bulk_seed_identities(db, [("ward6@dharan.example", "ward6@mdharan.example")])
    t = _Tgt()
    t.drives_ = {"d": {"name": "MIGRATION-STAGING-ward6", "files": {},
                       "perms": [{"id": "p", "emailAddress": "ward6@dharan.example"}]}}
    out = repair.clean_staging_drives(type("A", (), {"target_drive": lambda self, u: t})(), db, settings, apply=False)
    assert out["deleted_drives"] == 1 and t.log == [] and "d" in t.drives_
