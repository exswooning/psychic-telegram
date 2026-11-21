"""A stray file whose folder chain never resolves must not hold the stray check: its path walk is
bounded per file, and running out is an error for that file (info@, 3-4 Nov 2026: one file held a
verify job for 40+ minutes at 1,400 of 1,408)."""
from types import SimpleNamespace

import pytest

import verify_sample as V


def _verifier(db, settings):
    auth = SimpleNamespace(source_drive=lambda u: object(), target_drive=lambda u: object())
    return V.Verifier(auth, db, settings, "u@src", "u@tgt")


def test_a_path_that_runs_out_of_budget_raises_instead_of_hanging(db, settings, monkeypatch):
    v = _verifier(db, settings)
    monkeypatch.setattr(v, "_drive_meta", lambda svc, fid: {"name": fid, "parents": ["p-" + fid]})  # never ends
    with pytest.raises(TimeoutError):
        v._drive_path(object(), "f1", {}, budget_sec=1e-9)


def test_a_path_within_budget_is_unchanged(db, settings, monkeypatch):
    v = _verifier(db, settings)
    tree = {"f": {"name": "file.pdf", "parents": ["d"]}, "d": {"name": "Docs", "parents": []}}
    monkeypatch.setattr(v, "_drive_meta", lambda svc, fid: tree[fid])
    assert v._drive_path(object(), "f", {}, budget_sec=60) == "/file.pdf"      # My Drive root is not part of the path
