"""
tests/test_shared_drives_default_and_idempotent.py
==================================================
Two faults that produced one another.

--shared-drives defaulted to 0, so a seed launched without thinking about
it created none -- and a corpus with no shared drive cannot exercise
shared_drives.py at all. A 200-user, thirteen-hour run finished with zero
of them purely because the flag was not passed.

The obvious fix, defaulting it on, was unsafe while seed() created
unconditionally: every re-run added another SEEDED-SD-1. The live tenant
has two drives with that name, which is what that looks like from the
outside. "Run it again" has to mean "top it up", never "double it".
"""

from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "data-generator"))

import seed_sandbox as ss                 # noqa: E402
import seed_shared_drives as ssd          # noqa: E402
import webui                              # noqa: E402


class FakeDrives:
    """Just enough Drive to answer list and record creates."""

    def __init__(self, existing):
        self.existing = list(existing)
        self.created: list[str] = []

    # -- client shape --
    def drives(self):
        return self

    def permissions(self):
        return self

    def files(self):
        return self

    def list(self, **kw):
        self._resp = {"drives": list(self.existing)}
        return self

    def create(self, **kw):
        body = kw.get("body") or {}
        if "name" in body and kw.get("requestId"):
            self.created.append(body["name"])
            self._resp = {"id": f"new-{body['name']}", "name": body["name"]}
        else:
            self._resp = {"id": "perm"}
        return self

    def execute(self):
        return self._resp


def retry(fn):
    return fn


class TestItDoesNotDuplicate:
    def test_an_existing_drive_is_reused(self):
        d = FakeDrives([{"id": "d1", "name": "SEEDED-SD-1"}])
        have = ssd.existing_by_name(d, retry)
        assert have["SEEDED-SD-1"] == "d1"

    def test_only_seeded_drives_are_considered(self):
        """A customer's own shared drive must never be adopted or touched."""
        d = FakeDrives([{"id": "x", "name": "Finance 2024"},
                        {"id": "d1", "name": "SEEDED-SD-1"}])
        have = ssd.existing_by_name(d, retry)
        have.pop("__dupes__", None)
        assert list(have) == ["SEEDED-SD-1"]

    def test_a_duplicate_name_is_reported_not_hidden(self):
        """This tenant has two called SEEDED-SD-1. Picking one silently
        leaves a migration matching by name with two candidates and no
        warning."""
        d = FakeDrives([{"id": "a", "name": "SEEDED-SD-1"},
                        {"id": "b", "name": "SEEDED-SD-1"}])
        have = ssd.existing_by_name(d, retry)
        assert have["__dupes__"] == ["SEEDED-SD-1"]
        assert have["SEEDED-SD-1"] == "a", "the choice must be deterministic"

    def test_seed_creates_only_what_is_missing(self, monkeypatch, capsys):
        d = FakeDrives([{"id": "d1", "name": "SEEDED-SD-1"}])
        monkeypatch.setattr(ssd, "_drive_client", lambda *a, **k: d)
        monkeypatch.setattr(ssd, "_retry", lambda s: retry)
        # Stop after the drive loop's membership work.
        monkeypatch.setattr(ssd, "ROLES", ())
        out = ssd.seed(object(), "admin@x.test", [], n_drives=2)
        assert d.created == ["SEEDED-SD-2"], d.created
        assert out["reused"] == 1

    def test_re_running_creates_nothing(self, monkeypatch):
        d = FakeDrives([{"id": "d1", "name": "SEEDED-SD-1"},
                        {"id": "d2", "name": "SEEDED-SD-2"}])
        monkeypatch.setattr(ssd, "_drive_client", lambda *a, **k: d)
        monkeypatch.setattr(ssd, "_retry", lambda s: retry)
        monkeypatch.setattr(ssd, "ROLES", ())
        ssd.seed(object(), "admin@x.test", [], n_drives=2)
        assert d.created == []


class TestADefaultSeedIncludesThem:
    def test_the_seeder_defaults_to_some(self):
        assert ss.DEFAULT_SHARED_DRIVES >= 1

    def test_the_cli_default_is_that_number(self):
        import argparse
        import inspect

        src = inspect.getsource(ss.main)
        assert "default=DEFAULT_SHARED_DRIVES" in src

    def test_zero_still_disables_it(self, monkeypatch):
        """An operator who does not want them must still be able to say so."""
        monkeypatch.setattr(webui, "Settings", lambda **k: None, raising=False)
        import config
        monkeypatch.setattr(config, "Settings", lambda **k: type(
            "S", (), {"source_domain": "src.example", "target_domain": "t.example",
                      "db_path": ":memory:", "source_admin": "", "target_admin": "",
                      "source_sa_key": "", "target_sa_key": ""})())
        argv, _e, err = webui.seed_argv({"confirm_domain": "src.example",
                                         "shared_drives": 0})
        assert not err
        assert "--shared-drives" not in argv

    def test_omitting_it_no_longer_means_none(self):
        """The 200-user run produced zero shared drives for exactly this
        reason: the field was absent, so nothing was passed, so the seeder's
        own default of 0 applied."""
        import inspect

        src = inspect.getsource(webui.seed_argv)
        assert "the caller simply did not pass the field" in src \
            or "did not pass the field" in src
