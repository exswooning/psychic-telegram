"""A Form has no Drive comment threads: the comments API answers 404 "File not found" for one.
Live (2026-10-31) that was a FAILED comment on every copied Form, and a Drive check left
INCOMPLETE. Neither the engine nor the check asks any more; a real 404 elsewhere still counts."""
import pytest

import verification_coverage as VC
from drive_engine import FORM_MIME


def test_the_engine_never_asks_for_a_forms_comments(migrator, monkeypatch):
    migrator.settings.migrate_comments = True
    asked = []
    monkeypatch.setattr(migrator, "_sync_comments", lambda s, t: asked.append(s) or 0)
    migrator._finish_item({"id": "form1", "name": "Quiz", "mimeType": FORM_MIME, "shared": False}, "t-form1")
    migrator._finish_item({"id": "doc1", "name": "Plan", "mimeType": "application/vnd.google-apps.document",
                           "shared": False}, "t-doc1")
    assert asked == ["doc1"]


class _Files:
    def __init__(self, mime):
        self.mime = mime

    def get(self, **kw):
        return type("C", (), {"execute": lambda s: {"mimeType": self.mime}})()


class _Svc:
    def __init__(self, mime):
        self._f = _Files(mime)

    def files(self):
        return self._f


@pytest.mark.parametrize("mime, form", [(FORM_MIME, True), ("application/pdf", False)])
def test_the_check_recognises_a_form_by_its_type_not_by_the_404(mime, form):
    assert VC._is_form(_Svc(mime), "x") is form


def test_an_unreadable_type_is_not_taken_for_a_form():
    class Broken:
        def files(self):
            raise RuntimeError("down")
    assert VC._is_form(Broken(), "x") is False
