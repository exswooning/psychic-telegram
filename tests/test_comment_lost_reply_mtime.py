"""F-lost, 28 Nov 2026: a comment whose create reply was lost returned 0 writes, so the file's modifiedTime was
neither restored nor re-checked after Drive re-stamped it, and the copy kept the run's time."""
import drive_engine


def test_a_failed_comment_sync_counts_as_a_possible_write(monkeypatch):
    import migration_comments

    def lost(*a, **k):
        raise ConnectionResetError("failpoint drive.comment.created@2: reply lost")
    monkeypatch.setattr(migration_comments, "reconcile", lost)
    m = drive_engine.DriveMigrator.__new__(drive_engine.DriveMigrator)
    m.settings = type("S", (), {"dry_run": False})()
    m.source_user = "u@src.test"
    logged = []
    m.db = type("D", (), {"log_audit": lambda self, *a, **k: logged.append(a)})()
    m._has_no_comments_api = lambda sid: False
    assert m._sync_comments("src-file", "tgt-file") == 1
    assert logged and logged[0][3] == "FAILED"


def test_a_refusal_before_any_write_is_not_counted(monkeypatch):
    import migration_comments
    monkeypatch.setattr(migration_comments, "reconcile",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Comment repair requires an exact mapped target file")))
    m = drive_engine.DriveMigrator.__new__(drive_engine.DriveMigrator)
    m.settings = type("S", (), {"dry_run": False})()
    m.source_user = "u@src.test"
    m.db = type("D", (), {"log_audit": lambda self, *a, **k: None})()
    m._has_no_comments_api = lambda sid: False
    assert m._sync_comments("src-file", "tgt-file") == 0
