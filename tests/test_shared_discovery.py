"""One parsed discovery document per API, shared by every client (auth.py).

build() parsed a fresh copy per client: 0.55 MB per Drive client vs 0.01 MB
shared, ~300 MiB of a 2.4 GB live run. Sharing is safe only because the
document is warmed -- every nested resource built once, under a lock -- before
any client sees it: googleapiclient inserts keys into it lazily on first touch,
and two threads doing that first touch together would race. These tests pin
the property that makes it safe: after warm-up a build changes no key at all.
"""
import json
import threading

import httplib2
import pytest
from googleapiclient.discovery import build_from_document

import auth


def _snapshot(doc) -> str:
    return json.dumps(doc, sort_keys=True, default=str)


def _full_build(doc):
    auth._warm(build_from_document(doc, http=httplib2.Http()), doc)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(auth, "_DISCOVERY", {})


def test_the_library_really_does_write_into_a_cold_document():
    """The hazard is real -- so the next test cannot pass vacuously."""
    from googleapiclient import discovery_cache
    cold = json.loads(discovery_cache.get_static_doc("drive", "v3"))
    before = _snapshot(cold)
    _full_build(cold)
    assert _snapshot(cold) != before


@pytest.mark.parametrize("api", sorted(auth._API_VERSIONS))
def test_after_warm_up_a_build_changes_no_key(api):
    doc = auth.shared_discovery_doc(api, auth._API_VERSIONS[api])
    before = _snapshot(doc)
    _full_build(doc)
    assert _snapshot(doc) == before


def test_concurrent_builds_from_the_shared_document_race_on_nothing():
    doc = auth.shared_discovery_doc("drive", "v3")
    before, errors = _snapshot(doc), []

    def worker():
        try:
            for _ in range(5):
                _full_build(doc)
        except BaseException as exc:      # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert not errors, repr(errors[0])
    assert _snapshot(doc) == before


def test_every_client_shares_the_one_document():
    doc = auth.shared_discovery_doc("drive", "v3")
    a = build_from_document(doc, http=httplib2.Http())
    b = build_from_document(doc, http=httplib2.Http())
    assert a._rootDesc is doc and b._rootDesc is doc
    assert auth.shared_discovery_doc("drive", "v3") is doc


def test_no_bundled_document_falls_back_to_build(monkeypatch):
    from googleapiclient import discovery_cache
    monkeypatch.setattr(discovery_cache, "get_static_doc", lambda *a: None)
    assert auth.shared_discovery_doc("drive", "v3") is None
