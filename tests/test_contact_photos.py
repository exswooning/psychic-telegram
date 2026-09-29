"""A contact's own photo goes with it; the drawn letter avatar does not."""
from __future__ import annotations

import base64
import io

import pytest

from tests.conftest import SRC_USER, TGT_USER


@pytest.fixture
def contacts(auth, db, settings, identity):
    import contacts_engine
    settings.migrate_contacts = True
    return contacts_engine.ContactsMigrator(auth, db, settings, SRC_USER, TGT_USER)


def _serve(monkeypatch, data=b"\x89PNG-photo"):
    import urllib.request
    fetched = []

    def fake(url, timeout=0):
        fetched.append(url)
        return io.BytesIO(data)
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return fetched


def test_a_real_photo_is_copied_at_full_size(contacts, auth, monkeypatch):
    fetched = _serve(monkeypatch)
    src = auth.source_people(SRC_USER)
    rid = src.add_contact("Ada", "ada@x.com")
    src.contacts[rid]["photos"] = [{"url": "https://lh3.googleusercontent.com/abc=s100"}]
    contacts.run()
    tgt = auth.target_people(TGT_USER)
    [made] = tgt.contacts.values()
    assert base64.b64decode(made["_photoBytes"]) == b"\x89PNG-photo"
    assert fetched == ["https://lh3.googleusercontent.com/abc=s0"]
    assert contacts.stats["photos"] == 1


def test_the_default_avatar_is_not_copied(contacts, auth, monkeypatch):
    fetched = _serve(monkeypatch)
    src = auth.source_people(SRC_USER)
    rid = src.add_contact("Bo")
    src.contacts[rid]["photos"] = [{"url": "https://lh3.googleusercontent.com/x", "default": True}]
    contacts.run()
    assert fetched == [] and auth.target_people(TGT_USER).call_count("people.updateContactPhoto") == 0


def test_a_photo_that_cannot_be_fetched_never_fails_the_contact(contacts, auth, monkeypatch, db):
    import urllib.request

    def boom(url, timeout=0):
        raise OSError("404")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    src = auth.source_people(SRC_USER)
    rid = src.add_contact("Cy")
    src.contacts[rid]["photos"] = [{"url": "https://lh3.googleusercontent.com/y"}]
    contacts.run()
    assert db.get_audit(SRC_USER, rid, "contact")["status"] == "SUCCESS"


def test_photos_are_asked_for():
    import contacts_engine
    assert "photos" in contacts_engine.PERSON_FIELDS
