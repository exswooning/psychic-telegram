"""
tests/test_seed_one_service.py
==============================
A live seed produced 126 identical warnings -- one per user -- because the
Chat app was not configured, so every chat call 404'd and every user ended
with "0 chat messages in 0 spaces". The corpus is otherwise complete and
took twelve hours.

Reseeding all of it to recover one service would be absurd, so the seeder
can run a single service across the users that already exist. The risk that
carries is the opposite one: a partial pass that looks complete, or one that
quietly rebuilds Drive on top of a corpus that already has it.
"""

from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "data-generator"))

import seed_sandbox as ss           # noqa: E402
import webui                        # noqa: E402


class TestTheServiceListIsOneList:
    def test_webui_reads_the_seeders_own_names(self):
        """Two copies is how an endpoint comes to accept a name the child
        then rejects -- a job that starts and instantly dies."""
        assert webui._seedable_services() == list(ss.SEEDABLE)

    def test_it_covers_what_the_seeder_actually_writes(self):
        for svc in ("drive", "gmail", "calendar", "chat", "contacts", "tasks"):
            assert svc in ss.SEEDABLE


class TestTheEndpointChecksTheName:
    def _argv(self, monkeypatch, **body):
        # seed_argv imports Settings from config inside the function, so the
        # patch has to land on config, not on webui.
        import config

        monkeypatch.setattr(config, "Settings", lambda **k: type(
            "S", (), {"source_domain": "src.example", "target_domain": "tgt.example",
                      "db_path": ":memory:", "source_admin": "", "target_admin": "",
                      "source_sa_key": "", "target_sa_key": ""})())
        return webui.seed_argv(dict({"confirm_domain": "src.example"}, **body))

    def test_a_real_service_is_passed_through(self, monkeypatch):
        argv, _env, err = self._argv(monkeypatch, only="chat")
        assert not err, err
        assert argv[argv.index("--only") + 1] == "chat"

    def test_several_are_allowed(self, monkeypatch):
        argv, _env, err = self._argv(monkeypatch, only="chat,tasks")
        assert not err and argv[argv.index("--only") + 1] == "chat,tasks"

    def test_an_unknown_service_is_refused_by_name(self, monkeypatch):
        _argv, _env, err = self._argv(monkeypatch, only="slack")
        assert "slack" in err and "chat" in err

    def test_case_and_spacing_are_forgiven(self, monkeypatch):
        argv, _env, err = self._argv(monkeypatch, only=" Chat , Tasks ")
        assert not err and argv[argv.index("--only") + 1] == "chat,tasks"

    def test_omitting_it_seeds_everything(self, monkeypatch):
        """The flag must be opt-in: a full seed cannot start narrowing
        itself because a caller left a field out."""
        argv, _env, err = self._argv(monkeypatch)
        assert not err and "--only" not in argv


class TestTheSeederHonoursIt:
    """seed_one_user is the thing that must actually skip. These drive it
    with fakes -- a real one needs a tenant."""

    def _run(self, monkeypatch, only, called, items_fn=None):
        for name in ("seed_gmail", "seed_calendar", "seed_chat",
                     "seed_contacts", "seed_tasks", "seed_drafts",
                     "seed_secondary_calendars"):
            monkeypatch.setattr(ss, name,
                                lambda *a, _n=name, **k: called.append(_n) or {})
        monkeypatch.setattr(ss, "build_services", lambda *a, **k: (1, 2, 3))
        monkeypatch.setattr(ss, "build_chat", lambda *a, **k: 4)
        monkeypatch.setattr(ss, "build_people_tasks", lambda *a, **k: (5, 6))
        monkeypatch.setattr(ss, "_existing_drive_items",
                            items_fn or (lambda *a, **k: {}))

        class FakeBuilder:
            def __init__(self, *a, **k):
                pass

            def build(self, *a, **k):
                called.append("drive")
                return {"items": {}}
        monkeypatch.setattr(ss, "CorpusBuilder", FakeBuilder)

        return ss.seed_one_user(
            settings=type("S", (), {"source_domain": "s.example"})(),
            entry={"email": "u@s.example", "dept": "Eng", "project": "P",
                   "local": "u"},
            all_users=["u@s.example"], external="x@e.example", scale="tiny",
            mail_count=1, event_count=1, edge_cases=False, only=only)

    def test_chat_only_writes_chat(self, monkeypatch):
        called: list[str] = []
        self._run(monkeypatch, frozenset({"chat"}), called)
        assert "seed_chat" in called

    def test_chat_only_does_not_rebuild_drive(self, monkeypatch):
        """The corpus already exists. Rebuilding it would double every file
        and invalidate the counts the run is being judged against."""
        called: list[str] = []
        self._run(monkeypatch, frozenset({"chat"}), called)
        assert "drive" not in called

    def test_chat_only_touches_nothing_else(self, monkeypatch):
        called: list[str] = []
        self._run(monkeypatch, frozenset({"chat"}), called)
        assert called == ["seed_chat"]

    def test_no_only_still_seeds_everything(self, monkeypatch):
        called: list[str] = []
        self._run(monkeypatch, None, called)
        for expected in ("drive", "seed_gmail", "seed_calendar", "seed_chat",
                         "seed_contacts", "seed_tasks"):
            assert expected in called

    def test_a_skipped_service_says_so_rather_than_reporting_zero(self, monkeypatch):
        """A partial pass has to look partial. "0 messages" is what a
        BROKEN gmail seed reports, and the two must not be confused in a
        result anyone reads afterwards."""
        called: list[str] = []
        out = self._run(monkeypatch, frozenset({"chat"}), called)
        assert out["gmail"].get("note") == "not requested"
        assert out["drive"].get("note") == "not requested"

    def test_it_still_looks_for_drive_links_to_embed(self, monkeypatch):
        """Link rewriting is the thing a migration has to get right, so
        chat seeded with no links tests less than it looks like it does."""
        called: list[str] = []
        seen: list[int] = []
        self._run(monkeypatch, frozenset({"chat"}), called,
                  items_fn=lambda *a, **k: seen.append(1) or {})
        assert seen, "seeded chat without going to find anything to link to"


class TestFindingExistingFiles:
    def test_no_seeded_tree_yet_is_not_an_error(self):
        """No corpus just means no links -- better than refusing to seed."""
        class Drive:
            def files(self):
                return self

            def list(self, **k):
                return self

            def execute(self):
                return {"files": []}

        settings = type("S", (), {"retry_max": 1, "retry_base": 0})()
        assert ss._existing_drive_items(Drive(), settings) == {}

    def test_an_api_failure_is_not_fatal(self):
        class Drive:
            def files(self):
                raise RuntimeError("drive is down")

        settings = type("S", (), {"retry_max": 1, "retry_base": 0})()
        assert ss._existing_drive_items(Drive(), settings) == {}


class TestThePartialRunLooksPartial:
    def test_the_transcript_names_what_was_skipped(self, capsys, monkeypatch):
        """Otherwise every user prints "0 files, 0 messages, 0 events",
        which is indistinguishable from a seed whose credentials were
        wrong -- and the transcript is what anyone judges a run by."""
        called: list[str] = []
        TestTheSeederHonoursIt()._run(monkeypatch, frozenset({"chat"}), called)
        assert "[only: chat]" in capsys.readouterr().out

    def test_a_full_run_says_nothing_extra(self, capsys, monkeypatch):
        called: list[str] = []
        TestTheSeederHonoursIt()._run(monkeypatch, None, called)
        assert "[only:" not in capsys.readouterr().out
