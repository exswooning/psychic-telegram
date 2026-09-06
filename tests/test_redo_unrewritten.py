"""Repairing mail migrated before rewriting was switched on.

A migrated message cannot be edited -- Gmail has no API for it -- so the
only repair is to trash the target copy, forget the mapping, and insert a
corrected one. Destructive, so it is opt-in and narrow: only messages a
rewrite would actually change are touched.
"""
import base64

import pytest

import gmail_engine


RAW = base64.urlsafe_b64encode(
    b"From: a@x.test\r\nSubject: s\r\n\r\n"
    b"see https://drive.google.com/file/d/1SOURCEsourceSOURCEsource/view\r\n"
).decode()
PLAIN = base64.urlsafe_b64encode(
    b"From: a@x.test\r\nSubject: s\r\n\r\nno links here\r\n").decode()


class _Msgs:
    def __init__(self, raw): self.raw, self.trashed = raw, []
    def get(self, userId=None, id=None, format=None):
        return _Exec({"raw": self.raw, "labelIds": ["INBOX"]})
    def trash(self, userId=None, id=None):
        self.trashed.append(id); return _Exec({})


class _Exec:
    def __init__(self, v): self.v = v
    def execute(self): return self.v


class _Users:
    def __init__(self, m): self._m = m
    def messages(self): return self._m


class _Svc:
    def __init__(self, m): self._m = m
    def users(self): return _Users(self._m)


class _DB:
    def __init__(self): self.forgot, self.audit = [], []
    def get_target_id(self, u, i, t): return "TGT1"
    def forget_mapping(self, u, i, t): self.forgot.append((u, i, t))
    def log_audit(self, *a, **k): self.audit.append(a)
    def target_for_source_id(self, sid):
        return "1TARGETtargetTARGETtarget" if "SOURCE" in sid else None


def _m(settings, raw):
    g = object.__new__(gmail_engine.GmailMigrator)
    g.settings = settings
    g.db = _DB()
    g.src = _Svc(_Msgs(raw))
    g.tgt = _Svc(_Msgs(raw))
    g.source_user = "u@src.test"
    g._link_map = {}
    g._counts = {}
    g._retry = lambda fn, label=None: fn()
    g._bump = lambda k, n=1: g._counts.__setitem__(k, g._counts.get(k, 0) + n)
    return g


class TestRedo:
    def test_off_by_default_a_migrated_message_is_skipped(self, settings):
        settings.rewrite_drive_links = True
        settings.redo_unrewritten_links = False
        g = _m(settings, RAW)
        g._migrate_one_message({"id": "m1"})
        assert g._counts.get("skipped") == 1
        assert g.db.forgot == []

    def test_it_repairs_a_message_whose_link_would_change(self, settings):
        settings.rewrite_drive_links = True
        settings.redo_unrewritten_links = True
        g = _m(settings, RAW)
        with pytest.raises(Exception):        # falls through to the insert path
            g._migrate_one_message({"id": "m1"})
        assert g.tgt.users().messages().trashed == ["TGT1"], "old copy trashed"
        assert g.db.forgot == [("u@src.test", "m1", "message")]

    def test_it_leaves_a_message_with_nothing_to_fix_alone(self, settings):
        """A redo pass over a healthy mailbox must move nothing."""
        settings.rewrite_drive_links = True
        settings.redo_unrewritten_links = True
        g = _m(settings, PLAIN)
        g._migrate_one_message({"id": "m1"})
        assert g._counts.get("skipped") == 1
        assert g.tgt.users().messages().trashed == []
        assert g.db.forgot == []

    def test_it_does_nothing_with_rewriting_off(self, settings):
        """Otherwise it trashes the target copy and inserts an identical one:
        destruction with no repair attached."""
        settings.rewrite_drive_links = False
        settings.redo_unrewritten_links = True
        g = _m(settings, RAW)
        g._migrate_one_message({"id": "m1"})
        assert g._counts.get("skipped") == 1
        assert g.tgt.users().messages().trashed == []


class TestTheToggleRefusesTheUselessCombination:
    def test_webui_will_not_turn_redo_on_without_rewriting(self):
        import webui
        before = dict(webui._RUN_STATE)
        try:
            webui.set_toggles({"rewrite_drive_links": False})
            t = webui.set_toggles({"redo_unrewritten_links": True})["toggles"]
            assert t["redo_unrewritten_links"] is False
            assert "rewriting" in webui._RUN_STATE.get("last_note", "").lower()
        finally:
            webui._RUN_STATE.clear(); webui._RUN_STATE.update(before)
