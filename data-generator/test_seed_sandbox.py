"""
tests/test_seed_sandbox.py
==========================
The sandbox seeder writes to live tenants, so it cannot run in CI -- but its
*logic* can. These tests drive the real corpus builder against the same fakes
the engine tests use, which catches the failure that would otherwise cost an
hour of live debugging: a seeder that quietly produces an empty or unshared
corpus.

The final test is the important one. It builds a five-user org with cross-user
sharing, migrates all five with the real engine, and asserts the property that
only becomes checkable once files are shared: the target must contain the OWNED
union exactly once, not once per recipient.
"""

from __future__ import annotations

import base64

import pytest

from config import FOLDER_MIME, Settings
from tests.fakes import FakeAuth, FakeCalendar, FakeDrive, FakeGmail
from tools.corpus import ORG, SCALES, CorpusBuilder

SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
DOC_MIME = "application/vnd.google-apps.document"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"


class _FakeMedia:
    def __init__(self, data: bytes, mimetype: str):
        self._data, self.mimetype = data, mimetype

    def read_all(self) -> bytes:
        return self._data


def _media(data, mimetype):
    return _FakeMedia(data, mimetype)


def _retry(fn):
    return fn


@pytest.fixture
def seed(monkeypatch):
    import tools.seed_sandbox as s

    monkeypatch.setattr(s, "_media", _media)
    monkeypatch.setattr(s, "_retry_factory", lambda settings: _retry)
    return s


def _builder(drive, settings, user, peers, scale="tiny"):
    return CorpusBuilder(drive, settings, user, peers, "ext@example.com",
                         scale, _media, _retry, rng_seed=1234)


# ======================================================================
# Safety guards
# ======================================================================
def test_refuses_without_sandbox_mode(seed, settings, monkeypatch):
    monkeypatch.delenv("SANDBOX_MODE", raising=False)
    with pytest.raises(SystemExit):
        seed.assert_sandbox(settings, settings.source_domain)


def test_refuses_on_domain_mismatch(seed, settings, monkeypatch):
    monkeypatch.setenv("SANDBOX_MODE", "true")
    with pytest.raises(SystemExit):
        seed.assert_sandbox(settings, "some-other-domain.com")


def test_refuses_protected_domain(seed, settings, monkeypatch):
    monkeypatch.setenv("SANDBOX_MODE", "true")
    monkeypatch.setenv("PROTECTED_DOMAINS", f"foo.com,{settings.source_domain}")
    with pytest.raises(SystemExit):
        seed.assert_sandbox(settings, settings.source_domain)


def test_accepts_a_properly_declared_sandbox(seed, settings, monkeypatch):
    monkeypatch.setenv("SANDBOX_MODE", "true")
    monkeypatch.setenv("PROTECTED_DOMAINS", "prod.example")
    seed.assert_sandbox(settings, settings.source_domain)


# ======================================================================
# Org structure
# ======================================================================
def test_org_has_five_distinct_departments_and_projects():
    assert len(ORG) == 5
    assert len({e["dept"] for e in ORG}) == 5
    assert len({e["project"] for e in ORG}) == 5


def test_department_tree_is_realistic(settings):
    drive = FakeDrive("alice@tenanta.com", "source")
    b = _builder(drive, settings, "alice@tenanta.com",
                 ["bob@tenanta.com", "carol@tenanta.com", "dave@tenanta.com",
                  "erin@tenanta.com"])
    m = b.build("Engineering", "PRJ-001-Apollo", edge_cases=False)

    names = {f["name"] for f in drive.store.values()}
    assert "Dept-Engineering" in names
    assert {"Architecture", "Runbooks", "Postmortems"} <= names
    assert "PRJ-001-Apollo" in names
    assert {"Discovery", "Design", "Specs"} <= names
    assert "Personal" in names
    assert "Archive" in names
    assert m["folders"] > 20
    assert m["total_files"] > 20


def test_corpus_contains_docs_sheets_slides_and_binaries(settings):
    drive = FakeDrive("alice@tenanta.com", "source")
    b = _builder(drive, settings, "alice@tenanta.com", ["bob@tenanta.com"] * 4)
    m = b.build("Engineering", "PRJ-001-Apollo", edge_cases=False)
    assert m["docs"] > 0 and m["sheets"] > 0 and m["slides"] > 0
    assert m["binaries"] > 0
    mimes = {f.get("mimeType") for f in drive.store.values()}
    assert DOC_MIME in mimes and SHEET_MIME in mimes


def test_scale_profiles_actually_scale(settings):
    counts = {}
    for scale in ("tiny", "small", "medium"):
        drive = FakeDrive("alice@tenanta.com", "source")
        b = _builder(drive, settings, "alice@tenanta.com",
                     ["bob@tenanta.com"] * 4, scale=scale)
        counts[scale] = b.build("Sales", "PRJ-003-Cygnus",
                                edge_cases=False)["total_files"]
    assert counts["tiny"] < counts["small"] < counts["medium"]


# ======================================================================
# Sharing graph
# ======================================================================
def test_department_is_shared_domain_wide(settings):
    drive = FakeDrive("alice@tenanta.com", "source")
    peers = ["bob@tenanta.com", "carol@tenanta.com", "dave@tenanta.com",
             "erin@tenanta.com"]
    b = _builder(drive, settings, "alice@tenanta.com", peers)
    m = b.build("Engineering", "PRJ-001-Apollo", edge_cases=False)
    perms = drive.perms[m["items"]["dept_root"]]
    assert any(p["type"] == "domain" and p["domain"] == settings.source_domain
               for p in perms)


def test_restricted_departments_are_not_domain_shared(settings):
    drive = FakeDrive("bob@tenanta.com", "source")
    peers = ["alice@tenanta.com", "carol@tenanta.com", "dave@tenanta.com",
             "erin@tenanta.com"]
    b = _builder(drive, settings, "bob@tenanta.com", peers)
    m = b.build("Finance", "PRJ-002-Borealis", edge_cases=False)
    perms = drive.perms[m["items"]["dept_root"]]
    assert not any(p["type"] == "domain" for p in perms), \
        "Finance must be restricted to named colleagues, not domain-wide"
    assert all(p["type"] == "user" for p in perms)


def test_project_is_shared_with_a_named_team(settings):
    drive = FakeDrive("alice@tenanta.com", "source")
    peers = ["bob@tenanta.com", "carol@tenanta.com", "dave@tenanta.com",
             "erin@tenanta.com"]
    b = _builder(drive, settings, "alice@tenanta.com", peers)
    m = b.build("Engineering", "PRJ-001-Apollo", edge_cases=False)
    perms = drive.perms[m["items"]["project_root"]]
    writers = {p["emailAddress"] for p in perms if p["role"] == "writer"}
    assert writers == set(m["items"]["project_team"])
    assert any(p["role"] == "commenter" for p in perms), \
        "not every grant should be the same role"


def test_personal_folder_is_never_shared(settings):
    drive = FakeDrive("alice@tenanta.com", "source")
    peers = ["bob@tenanta.com"] * 4
    b = _builder(drive, settings, "alice@tenanta.com", peers)
    m = b.build("Engineering", "PRJ-001-Apollo", edge_cases=False)
    assert drive.perms.get(m["items"]["personal_root"], []) == []


def test_external_and_anyone_grants_exist(settings):
    drive = FakeDrive("alice@tenanta.com", "source")
    peers = ["bob@tenanta.com", "carol@tenanta.com", "dave@tenanta.com",
             "erin@tenanta.com"]
    b = _builder(drive, settings, "alice@tenanta.com", peers, scale="small")
    m = b.build("Engineering", "PRJ-001-Apollo", edge_cases=True)
    assert m["grants"]["external"] > 0
    assert m["grants"]["anyone"] > 0
    assert m["grants"]["user"] > 0
    assert m["grants"]["domain"] > 0


def test_grant_rejection_is_recorded_not_swallowed(settings):
    drive = FakeDrive("alice@tenanta.com", "source")
    drive.fail_next("permissions.create", status=403, reason="domainPolicy",
                    times=9999)
    b = _builder(drive, settings, "alice@tenanta.com", ["bob@tenanta.com"] * 4)
    m = b.build("Engineering", "PRJ-001-Apollo", edge_cases=False)
    assert sum(m["grants"].values()) == 0
    assert m["grants_rejected"], "a tenant blocking sharing is a finding"


# ======================================================================
# Edge cases
# ======================================================================
def test_full_edge_cases_cover_the_known_hazards(settings):
    drive = FakeDrive("alice@tenanta.com", "source")
    b = _builder(drive, settings, "alice@tenanta.com",
                 ["bob@tenanta.com"] * 4, scale="small")
    m = b.build("Engineering", "PRJ-001-Apollo", edge_cases=True)

    names = {f["name"] for f in drive.store.values()}
    assert 're/port "final" (v2).pdf' in names
    assert "zero-byte.dat" in names
    assert any(len(n) > 100 for n in names)
    assert m["shortcuts"] == 1
    assert m["oversized_native"] == 1
    assert len(m["items"]["delta_files"]) == 3

    big = next(f for f in drive.store.values() if f["name"] == "Oversized Doc")
    assert len(drive.exports[big["id"]]) > 10 * 1024 * 1024


def test_light_edge_cases_still_give_every_user_delta_targets(settings):
    drive = FakeDrive("dave@tenanta.com", "source")
    b = _builder(drive, settings, "dave@tenanta.com", ["bob@tenanta.com"] * 4)
    m = b.build("Marketing", "PRJ-004-Draco", edge_cases=False)
    assert len(m["items"]["delta_files"]) == 3
    assert m["items"].get("acl_file")


def test_shortcut_points_at_a_real_file(settings):
    drive = FakeDrive("alice@tenanta.com", "source")
    b = _builder(drive, settings, "alice@tenanta.com",
                 ["bob@tenanta.com"] * 4, scale="small")
    m = b.build("Engineering", "PRJ-001-Apollo", edge_cases=True)
    sc = drive.store[m["items"]["shortcut"]]
    assert sc["mimeType"] == SHORTCUT_MIME
    assert sc["shortcutDetails"]["targetId"] in drive.store


# ======================================================================
# Gmail / Calendar
# ======================================================================
def test_mailbox_has_cross_user_mail_in_every_state(seed, settings):
    gmail = FakeGmail("alice@tenanta.com", "source")
    peers = ["bob@tenanta.com", "carol@tenanta.com", "dave@tenanta.com",
             "erin@tenanta.com"]
    m = seed.seed_gmail(gmail, settings, "alice@tenanta.com", peers,
                        "ext@example.com", count=60)
    assert m["messages"] >= 60
    assert m["unread"] > 0 and m["starred"] > 0
    assert "Clients/Acme/2024" in m["labels"]

    senders = set()
    for msg in gmail.messages.values():
        raw = base64.urlsafe_b64decode(msg["raw"].encode()).decode("utf-8", "replace")
        for line in raw.splitlines():
            if line.startswith("From: "):
                senders.add(line[6:].strip())
    assert len(senders & set(peers)) >= 3, "mail should come from several colleagues"


def test_seeded_mail_is_backdated(seed, settings):
    gmail = FakeGmail("alice@tenanta.com", "source")
    seed.seed_gmail(gmail, settings, "alice@tenanta.com",
                    ["bob@tenanta.com"] * 4, "ext@example.com", count=5)
    inserts = gmail.calls_to("messages.insert")
    assert inserts and all(c["internalDateSource"] == "dateHeader"
                           for c in inserts)


def test_calendar_meetings_span_the_org_and_use_import(seed, settings):
    cal = FakeCalendar("alice@tenanta.com", "source")
    peers = ["bob@tenanta.com", "carol@tenanta.com", "dave@tenanta.com",
             "erin@tenanta.com"]
    m = seed.seed_calendar(cal, settings, "alice@tenanta.com", peers,
                           "ext@example.com", count=40)
    assert m["events"] >= 40
    assert m["recurring"] >= 1 and m["all_day"] == 1
    assert cal.call_count("events.import") == m["events"]

    attendee_emails = set()
    for c in cal.calls_to("events.import"):
        for a in c["body"].get("attendees", []):
            attendee_emails.add(a["email"])
    assert len(attendee_emails & set(peers)) >= 3


# ======================================================================
# The one that matters: five users, shared files, real migration
# ======================================================================
def test_five_user_org_migrates_without_duplicating_shared_files(
    settings, auth, db, monkeypatch
):
    """
    Build a five-user org where everyone shares with everyone, migrate all five
    with the real engine, and assert the property that only becomes checkable
    once sharing exists: the target holds the OWNED union exactly once.

    A fan-out bug -- copying each shared file once per recipient -- would make
    the target several times larger while every individual file still looked
    correct.
    """
    import drive_engine
    from db import bulk_seed_identities
    from resilience import DailyQuotaGuard

    locals_ = ["alice", "bob", "carol", "dave", "erin"]
    src_users = [f"{u}@{settings.source_domain}" for u in locals_]
    tgt_users = [f"{u}@{settings.target_domain}" for u in locals_]
    bulk_seed_identities(db, list(zip(src_users, tgt_users)))

    # --- Seed all five, each sharing outward to the other four ---------
    owned_files = 0
    for i, (src, entry) in enumerate(zip(src_users, ORG)):
        drive = auth.source_drive(src)
        peers = [u for u in src_users if u != src]
        b = CorpusBuilder(drive, settings, src, peers, "ext@example.com",
                          "tiny", _media, _retry, rng_seed=100 + i)
        m = b.build(entry["dept"], entry["project"], edge_cases=(i == 0))
        owned_files += m["total_files"]

    # Sanity: the sharing graph must actually be dense, or this proves nothing.
    total_grants = sum(
        len(p) for src in src_users
        for p in auth.source_drive(src).perms.values()
    )
    assert total_grants > 20, "corpus is not shared enough to test duplication"

    # --- Migrate all five ------------------------------------------------
    for src, tgt in zip(src_users, tgt_users):
        quota = DailyQuotaGuard(db, tgt, settings.effective_upload_cap())
        drive_engine.DriveMigrator(auth, db, settings, src, tgt, quota).run()

    target_files = sum(
        sum(1 for f in auth.target_drive(t).store.values()
            if f["mimeType"] != FOLDER_MIME and f["id"] != auth.target_drive(t).root_id)
        for t in tgt_users
    )
    skipped = db.conn.execute(
        """SELECT COUNT(*) c FROM audit_log
           WHERE item_type='file' AND status LIKE 'SKIPPED%'"""
    ).fetchone()["c"]

    expected = owned_files - skipped
    assert target_files == expected, (
        f"expected {expected} files in the target (owned {owned_files} "
        f"- skipped {skipped}), found {target_files}. A ratio near the number "
        f"of users means shared files are duplicated per recipient."
    )

    # --- ACLs translated across users ------------------------------------
    tgt_alice = auth.target_drive(tgt_users[0])
    shared = tgt_alice.by_name("shared-every-way.pdf")[0]
    emails = {p.get("emailAddress") for p in tgt_alice.perms[shared["id"]]}
    assert any(e and e.endswith("@" + settings.target_domain) for e in emails)
    assert not any(e and e.endswith("@" + settings.source_domain)
                   for e in emails), "source-domain address leaked into target ACL"
    assert "ext@example.com" in emails, "external collaborator dropped"

    # --- Private stayed private -------------------------------------------
    personal = tgt_alice.by_name("Personal")
    assert personal and tgt_alice.perms.get(personal[0]["id"], []) == []

    # --- And a second full run is still a no-op ---------------------------
    before = {t: auth.target_drive(t).count() for t in tgt_users}
    for src, tgt in zip(src_users, tgt_users):
        quota = DailyQuotaGuard(db, tgt, settings.effective_upload_cap())
        drive_engine.DriveMigrator(auth, db, settings, src, tgt, quota).run()
    after = {t: auth.target_drive(t).count() for t in tgt_users}
    assert before == after, "second run duplicated content"
