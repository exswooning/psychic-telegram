"""An event edited after it was migrated never reached the target.

calendar_engine skipped any event that already had a mapping, so a change
made at the source after the copy was silently dropped and the ledger went
on calling the event DONE. A migration runs for days and people keep using
their calendars throughout, so this is the common case rather than an edge
one -- and it is also how a Drive link added to a description after the
first pass stayed pointed at the source forever.

Drive has always recorded the source stamp for exactly this comparison
(audit_log.modified_time, "delta pass reads modified_time from here").
Calendar logged SUCCESS with no stamp, so there was nothing to compare.
"""
import calendar_engine


class _DB:
    def __init__(self, seen=None):
        self.seen, self.audits = seen, []
    def last_synced_modified_time(self, u, i, t): return self.seen
    def log_audit(self, *a, **k): self.audits.append((a, k))
    def target_for_source_id(self, sid): return "TGTFILE"
    def resolve_identity(self, e): return e


def _mig(settings, seen=None):
    m = object.__new__(calendar_engine.CalendarMigrator)
    m.settings = settings
    m.db = _DB(seen)
    m.source_user = "u@src.test"
    m.stats = {"events": 0, "exceptions": 0, "failed": 0, "skipped": 0, "updated": 0}
    m._retry = lambda fn, label=None: fn()
    return m


class TestStaleness:
    def test_a_newer_source_event_is_stale(self, settings):
        m = _mig(settings, seen="2026-09-01T10:00:00Z")
        assert m._is_stale("e1", {"updated": "2026-09-06T10:00:00Z"}) is True

    def test_an_unchanged_event_is_not(self, settings):
        m = _mig(settings, seen="2026-09-06T10:00:00Z")
        assert m._is_stale("e1", {"updated": "2026-09-06T10:00:00Z"}) is False

    def test_an_older_source_event_is_not(self, settings):
        m = _mig(settings, seen="2026-09-06T10:00:00Z")
        assert m._is_stale("e1", {"updated": "2026-09-01T10:00:00Z"}) is False

    def test_no_recorded_stamp_means_leave_it_alone(self, settings):
        """Events copied before this bookkeeping have no stamp. Treating
        those as stale would re-patch every event in the calendar on every
        pass."""
        m = _mig(settings, seen=None)
        assert m._is_stale("e1", {"updated": "2026-09-06T10:00:00Z"}) is False


class _Cal:
    def __init__(self): self.patched = []
    def events(self): return self
    def patch(self, **kw):
        self.patched.append(kw)
        return type("R", (), {"execute": lambda s=None: {}})()


class TestPatching:
    def _run(self, settings, item):
        m = _mig(settings, seen="2026-09-01T10:00:00Z")
        m.tgt = _Cal()
        m._patch_existing("e1", "TGT1", item, "primary")
        return m

    def test_the_edit_is_carried_over(self, settings):
        m = self._run(settings, {"summary": "moved", "updated": "2026-09-06T10:00:00Z"})
        assert m.tgt.patched[0]["eventId"] == "TGT1"
        assert m.tgt.patched[0]["body"]["summary"] == "moved"
        assert m.stats["updated"] == 1

    def test_a_link_added_after_migration_is_rewritten(self, settings):
        """The reason this matters for link rot: a description edited to add
        a Drive link after the first pass would otherwise never be seen."""
        settings.rewrite_drive_links = True
        m = self._run(settings, {
            "description": "doc https://docs.google.com/document/d/1SOURCEsourceSOURCEsource/edit",
            "updated": "2026-09-06T10:00:00Z"})
        assert "TGTFILE" in m.tgt.patched[0]["body"]["description"]

    def test_the_new_stamp_is_recorded(self, settings):
        """Without this it patches the same event on every later pass."""
        m = self._run(settings, {"summary": "x", "updated": "2026-09-06T10:00:00Z"})
        assert any(k.get("modified_time") == "2026-09-06T10:00:00Z"
                   for _, k in m.db.audits)


class TestItIsWiredIn:
    def test_the_skip_consults_staleness(self):
        import inspect
        src = inspect.getsource(calendar_engine.CalendarMigrator)
        assert "_is_stale" in src and "_patch_existing" in src
        # the skip must be conditional on staleness, not unconditional
        migrate = inspect.getsource(
            calendar_engine.CalendarMigrator.migrate_event)
        assert "_is_stale" in migrate and "_patch_existing" in migrate, (
            "an already-migrated event is skipped without asking whether "
            "the source has changed")

    def test_success_records_the_source_stamp(self):
        import inspect
        src = inspect.getsource(calendar_engine.CalendarMigrator)
        assert 'modified_time=item.get("updated")' in src, (
            "nothing to compare against on the next pass")
