"""
data-generator/test_reset_purges.py
===================================
The reset trashed and never purged, and Gmail keeps trashed mail for 30
days. So a "wiped" tenant still held the whole corpus -- and the migrator
lists with includeSpamTrash=True and preserves the TRASH label
deliberately, so a migration run against it would faithfully copy the mail
the reset was meant to remove.

Measured on the live tenant: users the reset had emptied and the reseed had
not yet reached held INBOX=2, TRASH=1444. getProfile reported 2, because
messagesTotal excludes Trash -- which is exactly how the reset had been
verified as working.

It purges when the scope is there and trashes when it is not, because an
ungranted scope must cost the purge and not the run.
"""

from __future__ import annotations

import pytest

import seed_sandbox as S


class _Msgs:
    def __init__(self, ids, owner):
        self._ids, self._owner = ids, owner

    def list(self, **kw):
        page = {"messages": [{"id": i} for i in self._ids]}
        return type("E", (), {"execute": lambda _s: page})()

    def get(self, userId=None, id=None, **kw):
        # Every id here is seeded; the caller filters on this header.
        body = {"payload": {"headers": [
            {"name": "Message-ID", "value": f"<{id}@seed.test>"}]}}
        return type("E", (), {"execute": lambda _s: body})()

    def trash(self, userId=None, id=None):
        self._owner.trashed.append(id)
        return type("E", (), {"execute": lambda _s: {}})()

    def batchDelete(self, userId=None, body=None):
        self._owner.purged.append(list((body or {}).get("ids") or []))
        return type("E", (), {"execute": lambda _s: {}})()


class _Gmail:
    def __init__(self, ids=()):
        self.trashed, self.purged = [], []
        self._ids = list(ids)

    def users(self):
        return self

    def messages(self):
        return _Msgs(self._ids, self)

    def drafts(self):
        empty = {"drafts": []}
        return type("D", (), {
            "list": lambda _s, **kw: type("E", (), {"execute": lambda _x: empty})(),
        })()

    def labels(self):
        empty = {"labels": []}
        return type("L", (), {
            "list": lambda _s, **kw: type("E", (), {"execute": lambda _x: empty})(),
        })()


@pytest.fixture
def settings():
    from config import Settings
    return Settings()


class TestItPurgesWhenItCan:
    def test_the_seeded_ids_are_batch_deleted(self, settings):
        g = _Gmail(["a", "b", "c"])
        purge = _Gmail()
        n = S.reset_gmail(g, settings, purge)
        assert purge.purged == [["a", "b", "c"]], purge.purged
        assert n == 3

    def test_nothing_is_trashed_when_it_purges(self, settings):
        g = _Gmail(["a", "b"])
        S.reset_gmail(g, settings, _Gmail())
        assert g.trashed == [], "trashed as well as purged"

    def test_batches_are_capped_at_a_thousand(self, settings):
        """batchDelete's own limit. A full corpus is ~1,450 a user."""
        ids = [str(i) for i in range(2500)]
        purge = _Gmail()
        S.reset_gmail(_Gmail(ids), settings, purge)
        assert [len(b) for b in purge.purged] == [1000, 1000, 500]

    def test_it_only_ever_deletes_ids_it_identified(self, settings):
        """batchDelete is irreversible and takes exactly what it is handed,
        so what it is handed must be the identified set -- never a query."""
        purge = _Gmail()
        S.reset_gmail(_Gmail(["x", "y"]), settings, purge)
        assert set(sum(purge.purged, [])) == {"x", "y"}


class TestItFallsBackRatherThanFailing:
    def test_no_scope_means_trash_exactly_as_before(self, settings):
        g = _Gmail(["a", "b"])
        n = S.reset_gmail(g, settings, None)
        assert g.trashed == ["a", "b"]
        assert n == 2

    def test_a_refused_batch_falls_back_to_trashing_that_batch(self, settings):
        class _Refuses(_Gmail):
            def messages(self):
                m = _Msgs([], self)
                m.batchDelete = lambda **kw: (_ for _ in ()).throw(
                    RuntimeError("403 insufficient scope"))
                return m

        g = _Gmail(["a", "b"])
        S.reset_gmail(g, settings, _Refuses())
        assert g.trashed == ["a", "b"], "a refused purge lost the messages"


class TestTheScopeIsReachable:
    def test_it_rides_the_console_grant_line(self):
        """Otherwise nobody can switch it on without a second hand-pasted
        grant -- how group migration came to be unrunnable by construction."""
        import verify_scopes

        assert S.GMAIL_PURGE_SCOPE in verify_scopes.OPTIONAL_SCOPES

    def test_it_is_not_in_the_seeders_main_scope_list(self):
        """A token request fails WHOLE if any scope in it is ungranted, so
        folding this in would take the entire seed down on every tenant that
        has not granted it."""
        assert S.GMAIL_PURGE_SCOPE not in S.SEED_SCOPES
