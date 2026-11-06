"""Quarantine policy (operator, 6 Nov 2026): a duplicate on the target is renamed, never deleted, and the
person using the file keeps access to it at all times. Live (account 3): large binaries and files with no
ETag were left as duplicates ("needs bounded streaming comparison", "ETag unavailable")."""
import pytest

import finding_repairs as F
import verify_sample as V
from tests.conftest import SRC_USER, TGT_USER


def _setup(auth, db, settings, identity, size=None):
    settings.source_read_only = True
    td = auth.target_drive(TGT_USER)
    keep = td.add_binary("plan.pdf", data=b"same bytes")
    dup = td.add_binary("plan.pdf", data=b"same bytes")
    for fid in (keep, dup):
        td.store[fid]["ownedByMe"] = True
        if size:
            td.store[fid]["size"] = str(size)
    db.record_mapping(SRC_USER, "src-plan", keep, "file")
    td.store[dup]["appProperties"] = {"bitportSourceId": "src-plan", "bitportSourceUser": SRC_USER}
    td.add_permission(dup, "user", "reader", email="colleague@t.test") if hasattr(td, "add_permission") else None
    return td, keep, dup


def _run(auth, db, settings, keep, dup):
    return F.quarantine_duplicates(auth, db, settings, SRC_USER,
                                   {"duplicates": [{"target": dup, "duplicateOf": keep}]}, apply=True)


def test_a_duplicate_is_renamed_in_place_and_the_original_is_untouched(auth, db, settings, identity):
    td, keep, dup = _setup(auth, db, settings, identity)
    perms_before = _perm_ids(td, dup)
    out = _run(auth, db, settings, keep, dup)
    assert out["changed"] == 1 and not out["errors"], out
    assert td.store[dup]["name"].startswith(".Quarantined duplicate ") and td.store[dup]["name"].endswith("plan.pdf")
    assert td.store[dup]["parents"] == td.store[keep]["parents"]          # same folder
    assert not td.store[dup].get("trashed") and td.store[keep]["name"] == "plan.pdf"
    assert _perm_ids(td, dup) == perms_before                              # nobody lost access


def test_a_large_binary_is_quarantined_on_checksum_and_size_without_a_download(auth, db, settings, identity, monkeypatch):
    td, keep, dup = _setup(auth, db, settings, identity, size=V.MAX_COMPARE_BYTES + 1)
    monkeypatch.setattr(type(td.files()), "get_media",
                        lambda *a, **k: pytest.fail("a large file must not be downloaded"), raising=False)
    out = _run(auth, db, settings, keep, dup)
    assert out["changed"] == 1 and not out["errors"], out


def test_a_rename_that_changed_who_can_open_the_file_is_undone(auth, db, settings, identity, monkeypatch):
    td, keep, dup = _setup(auth, db, settings, identity)
    seen = iter([[("p1", "user", "reader", "a@t.test")], []])               # access before, then after
    monkeypatch.setattr(F, "_access", lambda tgt, fid: next(seen))
    out = _run(auth, db, settings, keep, dup)
    assert out["changed"] == 0 and any("undone" in e for e in out["errors"])
    assert td.store[dup]["name"] == "plan.pdf"                              # put back exactly


def test_the_policy_is_named_in_code():
    assert {"rename-only", "same-parent", "sharing-unchanged", "original-untouched", "reversible"} == set(F.QUARANTINE_POLICY)


def _perm_ids(td, fid):
    return sorted(p.get("id") for p in td.perms.get(fid, []))


def test_a_duplicate_in_a_same_name_copy_of_the_folder_is_quarantined_where_it_is(auth, db, settings, identity):
    settings.source_read_only = True
    td = auth.target_drive(TGT_USER)
    f1, f2 = td.add_folder("for website"), td.add_folder("for website")      # the target had this folder twice
    keep = td.add_binary("plan.pdf", parent=f1, data=b"same bytes")
    dup = td.add_binary("plan.pdf", parent=f2, data=b"same bytes")
    for fid in (keep, dup):
        td.store[fid]["ownedByMe"] = True
    db.record_mapping(SRC_USER, "src-plan", keep, "file")
    td.store[dup]["appProperties"] = {"bitportSourceId": "src-plan", "bitportSourceUser": SRC_USER}
    out = _run(auth, db, settings, keep, dup)
    assert out["changed"] == 1 and not out["errors"], out
    assert td.store[dup]["parents"] == [f2]                                   # not moved


def test_a_duplicate_in_a_folder_with_another_name_is_left(auth, db, settings, identity):
    settings.source_read_only = True
    td = auth.target_drive(TGT_USER)
    f1, f2 = td.add_folder("for website"), td.add_folder("somewhere else")
    keep = td.add_binary("plan.pdf", parent=f1, data=b"same bytes")
    dup = td.add_binary("plan.pdf", parent=f2, data=b"same bytes")
    for fid in (keep, dup):
        td.store[fid]["ownedByMe"] = True
    db.record_mapping(SRC_USER, "src-plan", keep, "file")
    td.store[dup]["appProperties"] = {"bitportSourceId": "src-plan", "bitportSourceUser": SRC_USER}
    out = _run(auth, db, settings, keep, dup)
    assert out["changed"] == 0 and any("not a same-name folder" in e for e in out["errors"])
    assert td.store[dup]["name"] == "plan.pdf"


def test_users_with_findings_are_retried_after_the_quarantine_change():
    import verification_repairs as R
    assert R.REPAIR_CAPABILITY_VERSION >= 10
