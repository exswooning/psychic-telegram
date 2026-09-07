"""Edits made while a migration runs must reach the target.

A migration runs for days and people keep working. Drive has always compared
the source modification stamp; calendar was fixed earlier; contacts and tasks
skipped anything already mapped, so a number corrected or a task COMPLETED
mid-migration never arrived, and the ledger went on calling it DONE.
"""
import contacts_engine
import tasks_engine


class _DB:
    def __init__(self, seen=None):
        self.seen, self.audits = seen, []
    def last_synced_modified_time(self, u, i, t): return self.seen
    def get_target_id(self, u, i, t): return "TGT1"
    def log_audit(self, *a, **k): self.audits.append((a, k))
    def record_mapping(self, *a): pass
    def stamps(self): return [k.get("modified_time") for _, k in self.audits]


class _Tasks:
    def __init__(self): self.patched = []
    def tasks(self): return self
    def patch(self, tasklist=None, task=None, body=None):
        self.patched.append((tasklist, task, body))
        return type("R", (), {"execute": lambda s=None: {}})()


def _tasks_mig(settings, seen):
    m = object.__new__(tasks_engine.TasksMigrator)
    m.settings, m.db = settings, _DB(seen)
    m.source_user = "u@src.test"
    m.stats = {"lists": 0, "tasks": 0, "skipped": 0, "updated": 0, "failed": 0}
    m.tgt = _Tasks()
    m.limiter = type("L", (), {"acquire": lambda s=None: None})()
    m._retry = lambda fn, label=None: fn()
    return m


class TestATaskCompletedMidMigrationArrives:
    def test_a_changed_task_is_patched(self, settings):
        m = _tasks_mig(settings, seen="2026-09-01T10:00:00Z")
        m._create_task({"id": "t1", "title": "ship it", "status": "completed",
                        "updated": "2026-09-06T10:00:00Z"}, "L1", None)
        assert m.tgt.patched, "the completion never reached the target"
        assert m.tgt.patched[0][2]["status"] == "completed"
        assert m.stats["updated"] == 1

    def test_an_unchanged_task_is_left_alone(self, settings):
        m = _tasks_mig(settings, seen="2026-09-06T10:00:00Z")
        m._create_task({"id": "t1", "title": "x",
                        "updated": "2026-09-06T10:00:00Z"}, "L1", None)
        assert m.tgt.patched == [] and m.stats["skipped"] == 1

    def test_a_task_with_no_stamp_is_left_alone(self, settings):
        """Tasks copied before this bookkeeping. Treating a missing stamp as
        stale would rewrite every task on every pass."""
        m = _tasks_mig(settings, seen=None)
        m._create_task({"id": "t1", "title": "x",
                        "updated": "2026-09-06T10:00:00Z"}, "L1", None)
        assert m.tgt.patched == []

    def test_the_new_stamp_is_recorded(self, settings):
        """Or it patches the same task on every later pass."""
        m = _tasks_mig(settings, seen="2026-09-01T10:00:00Z")
        m._create_task({"id": "t1", "title": "x",
                        "updated": "2026-09-06T10:00:00Z"}, "L1", None)
        assert "2026-09-06T10:00:00Z" in m.db.stamps()


class _People:
    def __init__(self): self.updated = []
    def people(self): return self
    def get(self, resourceName=None, personFields=None):
        return type("R", (), {"execute": lambda s=None: {"etag": "TGTETAG"}})()
    def updateContact(self, resourceName=None, body=None, updatePersonFields=None):
        self.updated.append((resourceName, body))
        return type("R", (), {"execute": lambda s=None: {}})()


def _contacts_mig(settings, seen):
    m = object.__new__(contacts_engine.ContactsMigrator)
    m.settings, m.db = settings, _DB(seen)
    m.source_user = "u@src.test"
    m.stats = {"contacts": 0, "groups": 0, "skipped": 0, "updated": 0, "failed": 0}
    m.tgt = _People()
    m.limiter = type("L", (), {"acquire": lambda s=None: None})()
    m._retry = lambda fn, label=None: fn()
    return m


def _person(updated, phone="+1"):
    return {"resourceName": "people/c1",
            "names": [{"givenName": "Alice"}],
            "phoneNumbers": [{"value": phone}],
            "metadata": {"sources": [{"updateTime": updated}]}}


class TestANumberChangedMidMigrationArrives:
    def test_a_changed_contact_is_updated(self, settings):
        m = _contacts_mig(settings, seen="2026-09-01T10:00:00Z")
        m._migrate_contact(_person("2026-09-06T10:00:00Z", phone="+999"), {})
        assert m.tgt.updated, "the new number never reached the target"
        assert m.tgt.updated[0][1]["phoneNumbers"][0]["value"] == "+999"

    def test_it_uses_the_target_etag_not_the_source(self, settings):
        """People uses the etag for optimistic concurrency, and the source
        etag describes a different object entirely."""
        m = _contacts_mig(settings, seen="2026-09-01T10:00:00Z")
        m._migrate_contact(_person("2026-09-06T10:00:00Z"), {})
        assert m.tgt.updated[0][1]["etag"] == "TGTETAG"

    def test_an_unchanged_contact_is_left_alone(self, settings):
        m = _contacts_mig(settings, seen="2026-09-06T10:00:00Z")
        m._migrate_contact(_person("2026-09-06T10:00:00Z"), {})
        assert m.tgt.updated == [] and m.stats["skipped"] == 1

    def test_the_update_time_is_the_newest_source(self, settings):
        p = {"metadata": {"sources": [{"updateTime": "2026-09-01T00:00:00Z"},
                                      {"updateTime": "2026-09-06T00:00:00Z"}]}}
        assert contacts_engine._update_time(p) == "2026-09-06T00:00:00Z"

    def test_no_metadata_is_not_a_crash(self, settings):
        assert contacts_engine._update_time({}) is None


class TestTheStampIsActuallyFetched:
    def test_person_fields_asks_for_metadata(self):
        """Without it every already-migrated contact looks identical
        forever, and the comparison silently never fires."""
        assert "metadata" in contacts_engine.PERSON_FIELDS
