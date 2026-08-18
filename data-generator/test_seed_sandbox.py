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
from tests.fakes import (FakeAuth, FakeCalendar, FakeChat, FakeDrive,
                        FakeGmail, FakePeople, FakeTasks)
from corpus import ORG, SCALES, CorpusBuilder

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
    import seed_sandbox as s

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


def test_domain_wide_departments_also_get_the_extra_external_reader(settings):
    """Domain-wide already covers every org member; this operator-requested
    address is additive on top of it, not a replacement."""
    import corpus as corpus_mod

    drive = FakeDrive("alice@tenanta.com", "source")
    peers = ["bob@tenanta.com", "carol@tenanta.com", "dave@tenanta.com",
             "erin@tenanta.com"]
    b = _builder(drive, settings, "alice@tenanta.com", peers)
    m = b.build("Engineering", "PRJ-001-Apollo", edge_cases=False)
    perms = drive.perms[m["items"]["dept_root"]]
    assert any(p["type"] == "user"
              and p["emailAddress"] == corpus_mod.EXTRA_EXTERNAL_SHARE
              for p in perms)


def test_restricted_departments_do_not_get_the_extra_external_reader(settings):
    """RESTRICTED_DEPTS exists specifically to test that restricted access
    survives migration -- widening it, even by one address, would defeat
    that."""
    import corpus as corpus_mod

    drive = FakeDrive("bob@tenanta.com", "source")
    peers = ["alice@tenanta.com", "carol@tenanta.com", "dave@tenanta.com",
             "erin@tenanta.com"]
    b = _builder(drive, settings, "bob@tenanta.com", peers)
    m = b.build("Finance", "PRJ-002-Borealis", edge_cases=False)
    perms = drive.perms[m["items"]["dept_root"]]
    assert not any(p.get("emailAddress") == corpus_mod.EXTRA_EXTERNAL_SHARE
                  for p in perms)


def test_project_reaches_every_peer_not_just_the_first_three(settings):
    """The writer/commenter subset is unchanged; every remaining peer gets
    read access on top of it, so the whole org ends up with access to the
    project folder, not just the original 4-peer team."""
    drive = FakeDrive("alice@tenanta.com", "source")
    peers = [f"user{i}@tenanta.com" for i in range(10)]
    b = _builder(drive, settings, "alice@tenanta.com", peers)
    m = b.build("Engineering", "PRJ-001-Apollo", edge_cases=False)
    perms = drive.perms[m["items"]["project_root"]]
    emails = {p["emailAddress"] for p in perms if p["type"] == "user"}
    assert set(peers) <= emails


def test_project_also_gets_the_extra_external_reader(settings):
    import corpus as corpus_mod

    drive = FakeDrive("alice@tenanta.com", "source")
    peers = ["bob@tenanta.com", "carol@tenanta.com", "dave@tenanta.com",
             "erin@tenanta.com"]
    b = _builder(drive, settings, "alice@tenanta.com", peers)
    m = b.build("Engineering", "PRJ-001-Apollo", edge_cases=False)
    perms = drive.perms[m["items"]["project_root"]]
    assert any(p["emailAddress"] == corpus_mod.EXTRA_EXTERNAL_SHARE for p in perms)


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

    # "Oversized Doc" no longer exceeds the 10 MB files.export ceiling, and
    # cannot: Google's plain-text-to-Docs *import* hard-fails somewhere
    # between ~1.3 MB and ~2 MB of source text (measured against live
    # tenants), well below the export limit. Seeding the old ~12.9 MB version
    # returned 400 and killed the whole build. It stays as a large-native
    # round-trip case; the export ceiling itself is not reachable this way.
    big = next(f for f in drive.store.values() if f["name"] == "Oversized Doc")
    size = len(drive.exports[big["id"]])
    assert 1_000_000 < size < 2 * 1024 * 1024, (
        f"expected a large-but-importable native doc, got {size} bytes"
    )


def test_corpus_seeds_drive_comments(settings):
    """Comments are the clearest case of a migration that "succeeds" while
    losing something users notice, so the corpus has to contain some."""
    drive = FakeDrive("alice@tenanta.com", "source")
    b = _builder(drive, settings, "alice@tenanta.com",
                 ["bob@tenanta.com"] * 4, scale="medium")
    m = b.build("Engineering", "PRJ-001-Apollo", edge_cases=True)

    assert m["comments"] > 0, "corpus produced no comments to migrate"
    commented = [fid for fid, cs in drive.comment_store.items() if cs]
    assert commented
    assert any(c.get("replies") for cs in drive.comment_store.values()
               for c in cs), "no comment thread has a reply"


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


class TestConcurrentLeafCreation:
    """Leaf files are created on a small thread pool now, because doing them
    one at a time left the account's Drive write budget 6.3x idle (measured
    live: 0.47 writes/sec against Google's 3/sec per-account ceiling).

    The risk that buys is a corpus that changes shape depending on timing --
    `self.rng` is one Random consumed in a fixed order, so drawing from it
    inside worker threads would both race and reassign content. _plan_leaf
    draws everything serially and _exec_leaf only performs I/O, so these
    pin the property that makes the optimisation safe: a seeded run is
    identical whether it ran serial or concurrent."""

    def _build(self, settings, workers):
        settings.drive_file_workers = workers
        drive = FakeDrive("alice@tenanta.com", "source")
        b = CorpusBuilder(drive, settings, "alice@tenanta.com",
                          ["bob@tenanta.com"] * 4, "ext@example.com",
                          "small", _media, _retry, rng_seed=1234)
        m = b.build("Engineering", "PRJ-001-Apollo", edge_cases=False)
        return m, sorted(f["name"] for f in drive.store.values())

    def test_a_seeded_run_is_identical_serial_or_concurrent(self, seed, settings):
        serial_m, serial_names = self._build(settings, 1)
        par_m, par_names = self._build(settings, 4)

        assert serial_names == par_names, "concurrency changed which files exist"
        for k in ("docs", "sheets", "slides", "binaries", "folders",
                  "comments", "total_files"):
            assert serial_m[k] == par_m[k], f"{k} differs under concurrency"
        assert serial_m["grants"] == par_m["grants"]

    def test_concurrency_is_switchable_back_off(self, seed, settings):
        """drive_file_workers=1 must take the plain serial path -- the
        escape hatch if a tenant ever rate-limits badly under parallelism."""
        m, names = self._build(settings, 1)
        assert m["total_files"] > 0 and names

    def test_planning_draws_no_api_calls(self, seed, settings):
        """_plan_leaf must stay pure: if it ever performed I/O, planning a
        window of leaves up front would serialise exactly what this change
        set out to parallelise."""
        settings.drive_file_workers = 4
        drive = FakeDrive("alice@tenanta.com", "source")
        b = CorpusBuilder(drive, settings, "alice@tenanta.com",
                          ["bob@tenanta.com"] * 4, "ext@example.com",
                          "small", _media, _retry, rng_seed=1234)
        before = len(drive.store)
        plan = b._plan_leaf("root", "Engineering", "Runbooks", 0)

        assert len(drive.store) == before, "planning hit the API"
        assert plan["kind"] in {"doc", "sheet", "slides", "binary"}


class TestReseedingReusesExistingLabels:
    """Create-only label handling meant every label 409'd on a tenant that
    had been seeded before, leaving label_ids empty -- so `user_labels` was
    empty and NOT ONE message got a nested label. Each user still reported
    "done", so a re-seeded tenant silently produced a corpus with zero
    nested-label assignments: exactly the data the migration's own label
    handling exists to move. Confirmed live at ~1,200 warning lines across
    201 users."""

    def _preseeded(self):
        gmail = FakeGmail("alice@tenanta.com", "source")
        for name in __import__("seed_sandbox").SEED_LABELS:
            gmail.add_user_label(name)
        return gmail

    def test_messages_still_get_nested_labels_on_a_reseed(self, seed, settings):
        gmail = self._preseeded()
        seed.seed_gmail(gmail, settings, "alice@tenanta.com",
                        ["bob@tenanta.com"] * 4, "ext@example.com", count=80)

        nested = {lb["id"] for lb in gmail.labels
                  if "/" in (lb.get("name") or "")}
        labelled = [msg for msg in gmail.messages.values()
                    if nested & set(msg.get("labelIds") or [])]
        assert labelled, "a re-seed produced no nested-label assignments at all"

    def test_the_existing_labels_are_reused_not_duplicated(self, seed, settings):
        gmail = self._preseeded()
        before = len(gmail.labels)
        m = seed.seed_gmail(gmail, settings, "alice@tenanta.com",
                            ["bob@tenanta.com"] * 4, "ext@example.com", count=10)

        assert len(gmail.labels) == before, "re-seeding should create no new labels"
        # Reported as present, not skipped -- they are usable either way.
        assert "Clients/Acme/2024" in m["labels"]

    def test_a_reseed_creates_no_duplicate_label_calls(self, seed, settings):
        gmail = self._preseeded()
        seed.seed_gmail(gmail, settings, "alice@tenanta.com",
                        ["bob@tenanta.com"] * 4, "ext@example.com", count=10)
        assert gmail.calls_to("labels.create") == [], \
            "an already-seeded tenant should not attempt a single label create"

    def test_a_fresh_tenant_still_creates_them(self, seed, settings):
        gmail = FakeGmail("alice@tenanta.com", "source")
        m = seed.seed_gmail(gmail, settings, "alice@tenanta.com",
                            ["bob@tenanta.com"] * 4, "ext@example.com", count=10)
        assert "Clients/Acme/2024" in m["labels"]
        assert gmail.calls_to("labels.create"), "a fresh tenant must still create labels"


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


def test_chat_seed_creates_room_spaces_with_messages(seed, settings):
    FakeChat.reset_shared()
    chat = FakeChat("alice@tenuta.com", "source")
    m = seed.seed_chat(chat, settings, "alice@tenuta.com",
                       ["bob@tenuta.com"], "ext@example.com", local="alice")
    assert m["spaces"] == 2
    assert m["messages"] == 10
    assert chat.call_count("spaces.create") == 2
    assert chat.call_count("chat.messages.create") == 10
    names = [s["displayName"] for s in chat.space_store.values()]
    assert "alice team" in names and "alice standup" in names


def test_chat_seed_failure_is_best_effort_not_fatal(seed):
    class Bogus:
        def spaces(self):
            return self

        def create(self, **kw):
            raise RuntimeError("chat not enabled")

    FakeChat.reset_shared()
    m = seed.seed_chat(Bogus(), None, "alice@tenute.com",
                       ["bob@flight.com"], "x", local="alice")
    assert m["spaces"] == 0 and m["messages"] == 0 and m["note"]


def test_chat_reset_deletes_only_seeded_rooms(seed, settings):
    FakeChat.reset_shared()
    chat = FakeChat("alice@tenuta.com", "source")
    seed.seed_chat(chat, settings, "alice@tenuta.com",
                   ["bob@tenuta.com"], "ext@example.com", local="alice")
    chat.add_space("Real Room")          # must be left alone
    deleted = seed.reset_chat(chat, settings, local="alice")
    assert deleted == 2
    remaining = {s["displayName"] for s in chat.space_store.values()}
    assert remaining == {"Real Room"}


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


class TestSeedExitCode:
    """
    A run that seeded nothing must not report success.

    It returned 0 unconditionally. Five users timing out for thirty minutes
    rendered in the web UI as a green "exit 0" next to a run that wrote
    nothing at all — the single most misleading outcome the seeder can produce,
    because it looks exactly like the good one.
    """

    def test_exit_codes_are_distinct(self):
        import inspect

        import seed_sandbox

        src = inspect.getsource(seed_sandbox.main)
        assert "return 1" in src, "total failure must exit non-zero"
        assert "return 2" in src, "partial failure needs its own code"

    def test_total_failure_is_named_in_the_output(self):
        import inspect

        import seed_sandbox

        src = inspect.getsource(seed_sandbox.main)
        assert "0 of" in src and "Nothing was written" in src

    def test_partial_failure_is_distinguished_from_success(self):
        import inspect

        import seed_sandbox

        src = inspect.getsource(seed_sandbox.main)
        assert "PARTIAL" in src


class TestResetIsActuallyComplete:
    """
    Reset has to leave a clean slate, or a reseed builds on the last one.

    Two things survived it, both observed live: 201 drafts in a mailbox that
    had been reset repeatedly, and every seeded label — which is what produced
    "Label name exists or conflicts" on each label of the following run.
    """

    def test_the_label_set_has_one_definition(self):
        """Creation and reset used separate literal lists; drift between them
        means reset silently stops removing whatever was added."""
        import inspect

        import seed_sandbox

        src = inspect.getsource(seed_sandbox.seed_gmail)
        assert "SEED_LABELS" in src, "creation must use the shared constant"
        reset = inspect.getsource(seed_sandbox.reset_gmail)
        assert "SEED_LABELS" in reset, "reset must use the shared constant"

    def test_reset_deletes_drafts(self):
        """Trashing a draft's underlying message does not remove the draft."""
        import inspect

        import seed_sandbox

        src = inspect.getsource(seed_sandbox.reset_gmail)
        assert "drafts().list" in src and "drafts().delete" in src

    def test_reset_only_removes_labels_the_seeder_made(self):
        """Deleting every user label would take ones the account owner
        created themselves."""
        import inspect

        import seed_sandbox

        src = inspect.getsource(seed_sandbox.reset_gmail)
        assert 'type") == "user"' in src or "'user'" in src
        assert "wanted" in src

    def test_messages_are_still_matched_by_seed_marker(self):
        """The existing protection: only mail this seeder inserted is touched,
        identified by its @seed.test Message-ID."""
        import inspect

        import seed_sandbox

        assert "@seed.test" in inspect.getsource(seed_sandbox.reset_gmail)


# ======================================================================
# Licence-aware user selection (--fit-to-licenses)
# ======================================================================
def _entries(locals_):
    return [{"local": lp, "email": f"{lp}@tenanta.com"} for lp in locals_]


def test_fit_truncates_when_over_capacity():
    """More requested users than seats: keep the first `available`."""
    import seed_sandbox as s

    fitted = s.fit_entries(_entries(["alice", "bob", "carol", "dave", "erin"]),
                           available=3, existing_emails=set(),
                           domain="tenanta.com")
    assert [e["email"] for e in fitted] == [
        "alice@tenanta.com", "bob@tenanta.com", "carol@tenanta.com"]


def test_fit_keeps_existing_users_first():
    """Existing users never consume headroom; new users fill what is left."""
    import seed_sandbox as s

    entries = _entries(["alice", "bob", "carol", "dave", "erin"])
    existing = {"alice@tenanta.com", "bob@tenanta.com"}
    fitted = s.fit_entries(entries, available=2, existing_emails=existing,
                           domain="tenanta.com")
    assert [e["email"] for e in fitted] == [
        "alice@tenanta.com", "bob@tenanta.com", "carol@tenanta.com",
        "dave@tenanta.com"]


def test_fit_pads_unused_seats_with_generated_users():
    """Unused headroom is filled so every licence gets exercised."""
    import seed_sandbox as s

    entries = _entries(["alice", "bob"])
    fitted = s.fit_entries(entries, available=5, existing_emails=set(),
                           domain="tenanta.com")
    emails = [e["email"] for e in fitted]
    assert len(emails) == 5
    assert len(set(emails)) == 5
    assert emails[:2] == ["alice@tenanta.com", "bob@tenanta.com"]
    # Generated users must carry the ORG dept/project template, like any seed.
    for e in fitted[2:]:
        assert e["dept"] and e["project"]


def test_fit_with_no_headroom_keeps_only_existing():
    """Zero free seats and nobody existing -> nothing to seed."""
    import seed_sandbox as s

    entries = _entries(["alice", "bob"])
    fitted = s.fit_entries(entries, available=0, existing_emails=set(),
                           domain="tenanta.com")
    assert fitted == []


def test_fit_generated_names_never_collide_with_requested():
    """A generated localpart must not shadow one already in the list."""
    import seed_sandbox as s

    # "fiona" is the first generated name; force a collision and expect fiona1.
    entries = _entries(["alice", "fiona"])
    fitted = s.fit_entries(entries, available=4, existing_emails=set(),
                           domain="tenanta.com")
    locals_ = [e["local"] for e in fitted]
    assert "fiona" in locals_ and len(set(locals_)) == 4


class TestCreateUntilFullValidation:
    """--create-until-full generates its own candidates and creates as it
    goes, so it cannot be combined with the other user-selection modes --
    these are argparse-level guards in main(), checked the same way the
    rest of this file checks main()'s validation logic: by inspecting the
    source for the sys.exit calls rather than driving the full CLI (main()
    talks to live tenants past this point, same reason TestSeedExitCode
    above does not invoke it either)."""

    def test_requires_create_users(self):
        import inspect

        import seed_sandbox

        src = inspect.getsource(seed_sandbox.main)
        assert "create_until_full and not args.create_users" in src

    def test_rejects_reset(self):
        import inspect

        import seed_sandbox

        src = inspect.getsource(seed_sandbox.main)
        assert "create_until_full and args.reset" in src

    def test_rejects_the_other_selection_modes(self):
        """Combining with --users/--all-users/--fit-to-licenses is
        ambiguous -- --create-until-full builds its own entries and doesn't
        consult a fixed candidate list at all."""
        import inspect

        import seed_sandbox

        src = inspect.getsource(seed_sandbox.main)
        assert ("create_until_full and (args.fit_to_licenses or "
               "args.all_users or args.users)") in src


def test_parse_seats_sums_across_editions():
    """The org holds exactly one edition, but the sum must be right either way."""
    import seed_sandbox as s

    rows = [
        {"name": "accounts:apps_total_licenses", "intValue": "10"},
        {"name": "accounts:apps_used_licenses", "intValue": "4"},
        {"name": "accounts:gsuite_enterprise_total_licenses", "intValue": "1"},
        {"name": "accounts:gsuite_enterprise_used_licenses", "intValue": "1"},
    ]
    seat = s._parse_seats(rows)
    assert seat["total"] == 11
    assert seat["used"] == 5
    assert seat["available"] == 6


def test_parse_seats_defaults_missing_editions_to_zero():
    """A tenant that does not hold a given edition simply omits its row."""
    import seed_sandbox as s

    seat = s._parse_seats([])
    assert seat == {"total": 0, "used": 0, "available": 0,
                    "parameters": {}}


def test_entries_from_existing_users_covers_every_real_account():
    """--all-users' real headcount, not a licence-capacity guess like
    fit_entries() -- every existing address becomes an entry, none invented."""
    import seed_sandbox as s

    existing = {"alice@tenanta.com", "bob@tenanta.com", "carol@tenanta.com"}
    entries = s.entries_from_existing_users(existing, "tenanta.com")
    assert {e["email"] for e in entries} == existing
    assert len(entries) == 3


def test_entries_from_existing_users_never_invents_anyone():
    """Unlike fit_entries(), there is no headroom to pad -- an empty
    directory means an empty seeding list, not generated users."""
    import seed_sandbox as s

    assert s.entries_from_existing_users(set(), "tenanta.com") == []


def test_entries_from_existing_users_ignores_other_domains():
    """A Directory API response covers the whole customer, which can span
    more than one domain; only the sandbox source domain should be seeded."""
    import seed_sandbox as s

    existing = {"alice@tenanta.com", "bob@other-domain.com"}
    entries = s.entries_from_existing_users(existing, "tenanta.com")
    assert [e["email"] for e in entries] == ["alice@tenanta.com"]


def test_entries_from_existing_users_is_ordered_for_stable_reruns():
    """Sorted, not dict/set iteration order, so --edge-cases first always
    lands on the same user across repeated runs."""
    import seed_sandbox as s

    existing = {"zack@tenanta.com", "amy@tenanta.com", "mike@tenanta.com"}
    entries = s.entries_from_existing_users(existing, "tenanta.com")
    assert [e["local"] for e in entries] == ["amy", "mike", "zack"]


def test_entries_from_existing_users_assigns_dept_and_project_templates():
    """Real users still need a department/project template to seed a
    realistic corpus against -- cycled from ORG same as the default path."""
    import seed_sandbox as s
    from corpus import ORG

    existing = {f"user{i}@tenanta.com" for i in range(len(ORG) + 2)}
    entries = s.entries_from_existing_users(existing, "tenanta.com")
    for e in entries:
        assert e["dept"] and e["project"]


class TestDiscoverTenantEntries:
    """
    discover_tenant_entries() is the seeder's default source of users --
    the real tenant headcount instead of the fixed 5. It must never hard-fail
    an ordinary run: every way discovery can be unavailable falls back to the
    5-user default with an explanatory warning, not a crash.
    """

    def test_without_source_admin_falls_back_with_a_warning(
            self, settings, monkeypatch):
        import seed_sandbox as s
        from corpus import ORG

        monkeypatch.delenv("SOURCE_ADMIN", raising=False)
        entries, warning = s.discover_tenant_entries(settings)
        assert len(entries) == len(ORG)
        assert "SOURCE_ADMIN" in warning

    def test_a_failed_directory_read_falls_back_with_a_warning(
            self, settings, monkeypatch):
        import seed_sandbox as s
        from corpus import ORG

        monkeypatch.setenv("SOURCE_ADMIN", "admin@tenanta.com")
        monkeypatch.setattr(s, "build_directory_readonly",
                            lambda *a, **k: object())
        monkeypatch.setattr(
            s, "_list_users",
            lambda directory: (_ for _ in ()).throw(RuntimeError("403")))
        entries, warning = s.discover_tenant_entries(settings)
        assert len(entries) == len(ORG)
        assert "could not read" in warning
        assert s.DIRECTORY_READONLY_SCOPE in warning

    def test_an_empty_directory_falls_back_with_a_warning(
            self, settings, monkeypatch):
        import seed_sandbox as s
        from corpus import ORG

        monkeypatch.setenv("SOURCE_ADMIN", "admin@tenanta.com")
        monkeypatch.setattr(s, "build_directory_readonly",
                            lambda *a, **k: object())
        monkeypatch.setattr(s, "_list_users", lambda directory: set())
        entries, warning = s.discover_tenant_entries(settings)
        assert len(entries) == len(ORG)
        assert "no users" in warning

    def test_a_successful_discovery_returns_the_real_users_with_no_warning(
            self, settings, monkeypatch):
        import seed_sandbox as s

        monkeypatch.setenv("SOURCE_ADMIN", "admin@tenanta.com")
        monkeypatch.setattr(s, "build_directory_readonly",
                            lambda *a, **k: object())
        real_users = {"alice@tenanta.com", "bob@tenanta.com",
                     "carol@tenanta.com", "dave@tenanta.com"}
        monkeypatch.setattr(s, "_list_users", lambda directory: real_users)
        entries, warning = s.discover_tenant_entries(settings)
        assert {e["email"] for e in entries} == real_users
        assert warning == ""


# ======================================================================
# Storage top-up, Contacts and Tasks seeding
#
# Added alongside contacts_engine.py/tasks_engine.py, which had nothing to
# migrate: the seeder built drive/gmail/calendar/chat corpora but no contacts
# or tasks at all. And "seed to a target GB per user" has no realistic path
# through the office-document corpus (measured this session at tens of KB per
# file on average -- reaching tens of GB that way means hundreds of thousands
# of files per user), so it is a separate, deliberately unrealistic filler
# pass instead.
# ======================================================================
class _FakeMediaFn:
    """Mirrors test_seed_sandbox.py's own _media() above, injected the same
    way CorpusBuilder already takes one -- top_up_storage's production code
    calls the real _media(), which wraps a real MediaIoBaseUpload that this
    fake Drive cannot read (it expects .read_all(), not the real client's
    interface). Without injection there is no way to test this at all."""

    def __call__(self, data: bytes, mimetype: str):
        return _FakeMedia(data, mimetype)


class TestStorageTopUp:
    def test_already_at_target_adds_nothing(self, settings):
        import seed_sandbox as s

        drive = FakeDrive("alice@tenanta.com", "source")
        drive.storage_usage = 30 * 1024**3   # already at 30 GB
        drive.storage_limit = 1024**4

        m = s.top_up_storage(drive, settings, "alice@tenanta.com", 30.0,
                             media_fn=_FakeMediaFn())

        assert m["filler_files"] == 0
        assert m["filler_bytes"] == 0

    def test_fills_the_gap_between_usage_and_target(self, settings, monkeypatch):
        import seed_sandbox as s

        # A small chunk so the test uploads a handful of files, not gigabytes.
        monkeypatch.setattr(s, "_filler_blob", lambda: b"x" * (10 * 1024**2))

        drive = FakeDrive("alice@tenanta.com", "source")
        drive.storage_usage = 0
        drive.storage_limit = 1024**4

        # 25 MiB target over a 10 MiB chunk: 2 full chunks + a 5 MiB
        # remainder. top_up_storage works in decimal GB (target_gb * 1e9),
        # matching storageQuota's own byte units -- so the target is derived
        # from the exact byte count wanted, not assumed from a round GB value.
        target_bytes = 25 * 1024**2
        m = s.top_up_storage(drive, settings, "alice@tenanta.com",
                             target_gb=target_bytes / 1e9, media_fn=_FakeMediaFn())

        assert m["filler_files"] == 3
        assert m["filler_bytes"] == target_bytes

    def test_a_tiny_remainder_is_not_worth_a_whole_extra_file(
            self, settings, monkeypatch):
        monkeypatch.setattr(
            __import__("seed_sandbox"), "_filler_blob",
            lambda: b"x" * (10 * 1024**2))
        import seed_sandbox as s

        drive = FakeDrive("alice@tenanta.com", "source")
        drive.storage_usage = 0
        drive.storage_limit = 1024**4

        # 10 MB + 500 KB: the remainder is under the 1 MB floor and is skipped
        # rather than uploaded as a near-empty file.
        target_bytes = 10 * 1024**2 + 500 * 1024
        m = s.top_up_storage(drive, settings, "alice@tenanta.com",
                             target_gb=target_bytes / 1e9,
                             media_fn=_FakeMediaFn())

        assert m["filler_files"] == 1
        assert m["filler_bytes"] == 10 * 1024**2

    def test_the_licence_ceiling_caps_the_target(self, settings, monkeypatch):
        """Asking for more than the account's own licence limit would just
        fail partway through with storageQuotaExceeded -- capped here instead
        of discovered as an upload failure."""
        monkeypatch.setattr(
            __import__("seed_sandbox"), "_filler_blob",
            lambda: b"x" * (10 * 1024**2))
        import seed_sandbox as s

        drive = FakeDrive("alice@tenanta.com", "source")
        drive.storage_usage = 0
        drive.storage_limit = 20 * 1024**2   # a tiny 20 MB "licence"

        m = s.top_up_storage(drive, settings, "alice@tenanta.com",
                             target_gb=1.0, media_fn=_FakeMediaFn())

        assert m["filler_bytes"] <= 20 * 1024**2
        assert "licence" in m["note"]

    def test_filler_lives_under_the_reset_root(self, settings, monkeypatch):
        """Named exactly what reset_drive() already matches on, so top-up
        adds no separate reset path to write or remember."""
        monkeypatch.setattr(
            __import__("seed_sandbox"), "_filler_blob",
            lambda: b"x" * (10 * 1024**2))
        import seed_sandbox as s

        drive = FakeDrive("alice@tenanta.com", "source")
        drive.storage_usage = 0
        drive.storage_limit = 1024**4
        s.top_up_storage(drive, settings, "alice@tenanta.com",
                         target_gb=10 / 1024, media_fn=_FakeMediaFn())

        roots = [f for f in drive.store.values() if f["name"] == "MIGRATION-TEST"]
        assert len(roots) == 1
        deleted = s.reset_drive(drive, settings)
        assert deleted >= 1
        assert not any(f["name"] == "MIGRATION-TEST" for f in drive.store.values())


class TestSeedContacts:
    def test_creates_contacts_and_two_groups(self, settings):
        import seed_sandbox as s

        people = FakePeople("alice@tenanta.com", "source")
        m = s.seed_contacts(people, settings, "alice@tenanta.com",
                            ["bob@tenanta.com"], "external.tester@example.com",
                            count=6)

        assert m["contacts"] == 6
        assert m["groups"] == 2
        assert m["note"] == ""
        assert len(people.contacts) == 6

    def test_every_contact_is_marked_for_reset(self, settings):
        """reset_contacts() can only find what this seeder created if every
        contact carries the marker -- otherwise a reset silently deletes
        nothing, or (worse) it becomes tempting to widen the match and delete
        contacts the seeder never touched."""
        import seed_sandbox as s

        people = FakePeople("alice@tenanta.com", "source")
        s.seed_contacts(people, settings, "alice@tenanta.com", [],
                        "external.tester@example.com", count=3)

        for rec in people.contacts.values():
            assert s._SEED_MARKER in rec.get("userDefined", [])

    def test_contacts_are_split_across_the_groups(self, settings):
        import seed_sandbox as s

        people = FakePeople("alice@tenanta.com", "source")
        s.seed_contacts(people, settings, "alice@tenanta.com", [],
                        "external.tester@example.com", count=10)

        total_membership = sum(len(v) for v in people.group_members.values())
        assert total_membership == 10

    def test_reseeding_reuses_an_existing_group_instead_of_failing(self, settings):
        """Observed live at huge scale against an already-seeded tenant: the
        group create 409s (it exists from the prior run) and, without a
        lookup fallback, that single conflict aborted contacts for every one
        of 201 users. A re-seed must reuse the existing group, not lose the
        whole step to a name collision that isn't actually a problem."""
        import seed_sandbox as s

        people = FakePeople("alice@tenanta.com", "source")
        people.add_group("Clients")
        people.add_group("Vendors")

        m = s.seed_contacts(people, settings, "alice@tenanta.com", [],
                            "external.tester@example.com", count=6)

        assert m["note"] == ""
        assert m["contacts"] == 6
        assert m["groups"] == 2
        assert len(people.groups) == 2  # reused, not duplicated

    def test_a_missing_scope_is_recorded_not_raised(self, settings):
        """The whole reason this is isolated from build_services(): contacts
        write access is commonly granted on a different schedule than
        drive/gmail/calendar/chat, and one user's missing grant must not
        abort seeding for everyone else."""
        import seed_sandbox as s

        class _Denying:
            def contactGroups(self):
                raise RuntimeError("unauthorized_client")

        m = s.seed_contacts(_Denying(), settings, "alice@tenanta.com", [],
                            "external.tester@example.com")
        assert "contacts failed" in m["note"]
        assert m["contacts"] == 0

    def test_reset_removes_only_marked_contacts(self, settings):
        import seed_sandbox as s

        people = FakePeople("alice@tenanta.com", "source")
        people.add_contact("Real Person", "real@tenanta.com")   # not seeded
        s.seed_contacts(people, settings, "alice@tenanta.com", [],
                        "external.tester@example.com", count=4)
        assert len(people.contacts) == 5

        deleted = s.reset_contacts(people, settings)

        assert deleted == 4
        assert len(people.contacts) == 1
        assert next(iter(people.contacts.values()))["names"][0]["givenName"] \
            == "Real Person"


class TestSeedTasks:
    def test_creates_one_list_with_the_requested_tasks(self, settings):
        import seed_sandbox as s

        tasks = FakeTasks("alice@tenanta.com", "source")
        m = s.seed_tasks(tasks, settings, count=8)

        assert m["lists"] == 1
        assert m["tasks"] == 8
        assert m["note"] == ""

    def test_some_tasks_are_marked_completed(self, settings):
        """All-pending test data would not exercise the completed/pending
        split contacts_engine.py's ServiceProgress model actually reports on."""
        import seed_sandbox as s

        tasks = FakeTasks("alice@tenanta.com", "source")
        s.seed_tasks(tasks, settings, count=12)

        all_tasks = [t for lst in tasks.task_store.values() for t in lst]
        completed = [t for t in all_tasks if t.get("status") == "completed"]
        assert completed and len(completed) < len(all_tasks)

    def test_a_missing_scope_is_recorded_not_raised(self, settings):
        import seed_sandbox as s

        class _Denying:
            def tasklists(self):
                raise RuntimeError("unauthorized_client")

        m = s.seed_tasks(_Denying(), settings)
        assert "tasks failed" in m["note"]
        assert m["tasks"] == 0

    def test_reset_deletes_the_list_and_its_tasks(self, settings):
        import seed_sandbox as s

        tasks = FakeTasks("alice@tenanta.com", "source")
        other = tasks.add_list("A real list")
        tasks.add_task(other, "Something real")
        s.seed_tasks(tasks, settings, count=5)
        assert len(tasks.lists) == 2

        deleted = s.reset_tasks(tasks, settings)

        assert deleted == 1
        assert len(tasks.lists) == 1
        assert tasks.lists[other]["title"] == "A real list"
        assert len(tasks.task_store[other]) == 1   # untouched
