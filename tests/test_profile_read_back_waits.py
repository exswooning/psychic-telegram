"""Google's Directory shows a profile update late. Live (dharan): revenue@ and ward14@
read their old names straight after the write ("read-back still differs") and matched
on the next pass. The read-back waits for it now; a real refusal still says so."""
import copy

import user_profiles as U
from db import bulk_seed_identities


class _Dir:
    def __init__(self, users, stale_reads):
        self.users_by_email, self.stale_reads, self.pending = users, stale_reads, None

    def users(self):
        return self

    def list(self, domain, **k):
        return _R({"users": [copy.deepcopy(u) for e, u in self.users_by_email.items() if e.endswith("@" + domain)]})

    def get(self, userKey, **k):
        u = next(u for u in self.users_by_email.values() if u["id"] == userKey or u["primaryEmail"] == userKey)
        if self.pending and self.stale_reads > 0:
            self.stale_reads -= 1
            return _R(copy.deepcopy(u))                          # the old name, still
        if self.pending:
            u.update(self.pending)
            self.pending = None
        return _R(copy.deepcopy(u))

    def update(self, userKey, body):
        self.pending = {k: v for k, v in body.items() if v is not None}
        return _R({}, headers=True)


class _R:
    def __init__(self, value, headers=False):
        self.value, self.headers = value, {}

    def execute(self, num_retries=0):
        return self.value


class _Auth:
    def __init__(self, d):
        self.d = d

    def directory(self, side, writable=False):
        return self.d


def _run(db, settings, stale_reads):
    settings.source_domain, settings.target_domain = "dharan.example", "mdharan.example"
    bulk_seed_identities(db, [("revenue@dharan.example", "revenue@mdharan.example")])
    users = {"revenue@dharan.example": {"id": "S1", "primaryEmail": "revenue@dharan.example",
                                        "name": {"givenName": "Revenue", "familyName": "Department"}},
             "revenue@mdharan.example": {"id": "T1", "primaryEmail": "revenue@mdharan.example", "etag": "e1",
                                         "name": {"givenName": "Revenue", "familyName": "User"}}}
    out = U.sync_profiles(_Auth(_Dir(users, stale_reads)), db, settings,
                          [("revenue@dharan.example", "revenue@mdharan.example")], apply=True, progress=lambda m: None)
    return out["profiles"][0]


def test_a_write_google_shows_late_is_updated_not_an_error(db, settings, monkeypatch):
    slept = []
    monkeypatch.setattr(U, "_sleep", slept.append)
    row = _run(db, settings, stale_reads=2)
    assert row["status"] == "UPDATED", row
    assert slept[:3] == [0, 2, 4]                               # it waited, then read it


def test_a_write_that_never_shows_is_still_reported(db, settings):
    row = _run(db, settings, stale_reads=99)
    assert row["status"] == "ERROR" and "still differs after" in row["error"]
