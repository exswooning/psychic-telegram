"""A failpoint spec scoped to one user (`point@n#user`), several specs at once, and a spec that fires is struck out
(what lets a campaign batch arm one fault per twin and restart the run without meeting the same fault again)."""
import pytest

import domain_guard
import failpoints


@pytest.fixture
def fault_file(monkeypatch, tmp_path):
    monkeypatch.setattr(domain_guard, "revocations", lambda: {"b.example.test": {}})
    monkeypatch.setenv("TARGET_DOMAIN", "b.example.test")
    monkeypatch.delenv("BITPORT_FAULT", raising=False)
    monkeypatch.setattr(failpoints, "FAULT_FILE", str(tmp_path / "failpoint"))
    failpoints._counts.clear()
    yield tmp_path / "failpoint"
    failpoints._counts.clear()


def test_a_scoped_spec_counts_and_fires_only_for_its_user(fault_file):
    fault_file.write_text("gmail.inserted@2:lost#t05@a.test\n")
    assert failpoints.hit("gmail.inserted", 1, user="t06@a.test") == 1      # another user's calls are not counted
    assert failpoints.hit("gmail.inserted", 2, user="t06@a.test") == 2
    assert failpoints.hit("gmail.inserted", 3) == 3                          # a call site naming no user is never scoped
    assert failpoints.hit("gmail.inserted", 4, user="t05@a.test") == 4       # its 1st
    with pytest.raises(ConnectionResetError):
        failpoints.hit("gmail.inserted", 5, user="t05")                      # its 2nd: the local part names it too


def test_several_specs_arm_at_once_and_each_is_struck_from_the_file_when_it_fires(fault_file, monkeypatch):
    fault_file.write_text("tasks.created@1:lost#t02\ndrive.copy.landed@1#t03\n")
    codes = []
    monkeypatch.setattr(failpoints.os, "_exit", codes.append)
    with pytest.raises(ConnectionResetError):
        failpoints.hit("tasks.created", 1, user="t02@a.test")
    assert fault_file.read_text() == "drive.copy.landed@1#t03\n"            # a restarted run does not meet it again
    failpoints.hit("drive.copy.landed", 1, user="t02@a.test")
    assert codes == []
    failpoints.hit("drive.copy.landed", 1, user="t03@a.test")
    assert codes == [137] and fault_file.read_text() == ""


def test_a_spec_without_a_scope_still_fires_for_any_user(fault_file):
    fault_file.write_text("contacts.created@1:lost\n")
    with pytest.raises(ConnectionResetError):
        failpoints.hit("contacts.created", 1, user="t09@a.test")


def test_a_run_tells_failpoints_its_target(monkeypatch, tmp_path):
    """fp2-live, 24 Nov 2026: API-launched runs left TARGET_DOMAIN unset; every failpoint read the target as ''."""
    import os
    import main
    monkeypatch.delenv("TARGET_DOMAIN", raising=False)
    monkeypatch.setenv("MIGRATION_DB", str(tmp_path / "l.db"))
    class S:
        target_domain, db_path, dry_run, user_workers = "b.example", str(tmp_path / "l.db"), False, 1
    monkeypatch.setattr(main, "Settings", lambda account_id=None: S())
    monkeypatch.setattr(main, "setup_logging", lambda s: (_ for _ in ()).throw(SystemExit(0)))
    try:
        main.main(["status"])
    except SystemExit:
        pass
    assert os.environ["TARGET_DOMAIN"] == "b.example"
