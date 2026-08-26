"""Failures that no later pass would ever revisit.

Drive's delta enumerates files and notices a missing mapping, so a failed
file gets picked up again on its own. Gmail and Calendar list by the ITEM's
own date -- newer_than:Nd and updatedMin -- so an item that failed to import
is never offered again unless somebody edits it, and its FAILED row sits in
the ledger forever. Live, four failures survived two runs and a repair that
way.
"""
import db as dbmod
import retry_failed


class FakeGmail:
    made = []

    def __init__(self, auth, db, settings, src, tgt):
        self.src_user, self.tgt_user = src, tgt
        FakeGmail.made.append((src, tgt))
        self.seen = []

    def sync_labels(self):
        pass

    def _migrate_one_message(self, ref):
        self.seen.append(ref["id"])
        FakeGmail.last = self


class FakeCalendar:
    def __init__(self, auth, db, settings, src, tgt):
        self.src_user = src
        self.fetched, self.migrated = [], []
        FakeCalendar.last = self

    def fetch_event(self, eid, src_cal_id="primary"):
        self.fetched.append(eid)
        return None if eid == "gone" else {"id": eid}

    def migrate_event(self, item, tgt="primary", src="primary"):
        self.migrated.append(item["id"])


def _db(tmp_path, pairs=(("u@src", "u@tgt"),)):
    d = dbmod.MigrationDB(str(tmp_path / "m.db"))
    for s, t in pairs:
        d.conn.execute("INSERT INTO identity_map(source_email,target_email) "
                       "VALUES(?,?)", (s, t))
    d.conn.commit()
    return d


def _run(d, **kw):
    return retry_failed.retry(None, d, None, _gmail_cls=FakeGmail,
                              _calendar_cls=FakeCalendar, **kw)


class TestItFindsWhatIsStranded:
    def test_it_collects_failed_messages_and_events(self, tmp_path):
        d = _db(tmp_path)
        d.log_audit("u@src", "m1", "message", "FAILED", "400")
        d.log_audit("u@src", "e1", "event", "FAILED", "400")
        found = retry_failed.failed_items(d)
        assert found["u@src"]["message"] == ["m1"]
        assert found["u@src"]["event"] == ["e1"]
        d.close()

    def test_a_succeeded_item_is_not_retried(self, tmp_path):
        d = _db(tmp_path)
        d.log_audit("u@src", "m1", "message", "SUCCESS")
        assert retry_failed.failed_items(d) == {}
        d.close()

    def test_a_failed_user_row_is_not_this_passs_job(self, tmp_path):
        # A failed user means the whole user errored -- a migration's
        # problem, not something to re-import item by item.
        d = _db(tmp_path)
        d.log_audit("u@src", "u@src", "user", "FAILED", "401")
        assert retry_failed.failed_items(d) == {}
        d.close()

    def test_files_are_left_to_drive(self, tmp_path):
        # Drive's own delta already revisits these; retrying them here would
        # duplicate work that is not stranded.
        d = _db(tmp_path)
        d.log_audit("u@src", "f1", "file", "FAILED", "403")
        assert retry_failed.failed_items(d) == {}
        d.close()


class TestItActuallyRetries:
    def test_each_failed_message_is_re_attempted_by_id(self, tmp_path):
        d = _db(tmp_path)
        d.log_audit("u@src", "m1", "message", "FAILED", "400")
        d.log_audit("u@src", "m2", "message", "FAILED", "400")
        out = _run(d, apply=True)
        assert sorted(FakeGmail.last.seen) == ["m1", "m2"]
        assert out["retried"] == 2
        d.close()

    def test_an_event_is_re_read_before_being_re_imported(self, tmp_path):
        # The body has to come from the source; the ledger only kept an id.
        d = _db(tmp_path)
        d.log_audit("u@src", "e1", "event", "FAILED", "400")
        _run(d, apply=True)
        assert FakeCalendar.last.fetched == ["e1"]
        assert FakeCalendar.last.migrated == ["e1"]
        d.close()

    def test_an_event_that_no_longer_exists_is_skipped_quietly(self, tmp_path):
        d = _db(tmp_path)
        d.log_audit("u@src", "gone", "event", "FAILED", "400")
        out = _run(d, apply=True)
        assert FakeCalendar.last.migrated == []
        assert out["retried"] == 0
        d.close()

    def test_a_dry_run_touches_neither_tenant(self, tmp_path):
        d = _db(tmp_path)
        d.log_audit("u@src", "m1", "message", "FAILED", "400")
        FakeGmail.made.clear()
        out = _run(d, apply=False)
        assert out["messages"] == 1 and out["retried"] == 0
        assert FakeGmail.made == [], "reporting must not construct a migrator"
        d.close()

    def test_a_user_with_no_target_is_reported_not_guessed(self, tmp_path):
        d = _db(tmp_path)          # no identity_map row for this user
        d.conn.execute("DELETE FROM identity_map")
        d.conn.commit()
        d.log_audit("u@src", "m1", "message", "FAILED", "400")
        out = _run(d, apply=True)
        assert out["unmapped_users"] == 1 and out["retried"] == 0
        d.close()

    def test_one_users_failure_does_not_abort_the_rest(self, tmp_path):
        class Boom(FakeGmail):
            def sync_labels(self):
                raise RuntimeError("mailbox unavailable")

        d = _db(tmp_path, pairs=(("a@src", "a@tgt"), ("b@src", "b@tgt")))
        d.log_audit("a@src", "m1", "message", "FAILED", "400")
        d.log_audit("b@src", "e1", "event", "FAILED", "400")
        out = retry_failed.retry(None, d, None, apply=True, _gmail_cls=Boom,
                                 _calendar_cls=FakeCalendar)
        assert any("mailbox unavailable" in e for e in out["errors"])
        assert FakeCalendar.last.migrated == ["e1"]
        d.close()
