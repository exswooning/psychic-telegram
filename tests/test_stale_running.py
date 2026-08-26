"""RUNNING must not outlive the process that set it.

A delta deadlocked and had to be killed. Nineteen users caught the shutdown
and became INTERRUPTED; three did not and stayed RUNNING with nothing behind
them, so the page reported three users migrating indefinitely.
"""
import db as dbmod
import main


def _db(tmp_path, rows):
    d = dbmod.MigrationDB(str(tmp_path / "m.db"))
    for email, status in rows:
        d.conn.execute("INSERT INTO identity_map(source_email,target_email,"
                       "status) VALUES(?,?,?)", (email, email + ".tgt", status))
    d.conn.commit()
    return d


def _status(d, email):
    return d.conn.execute("SELECT status FROM identity_map WHERE "
                          "source_email=?", (email,)).fetchone()["status"]


class TestStaleRunningIsReopened:
    def test_a_user_left_running_becomes_interrupted(self, tmp_path):
        d = _db(tmp_path, [("a@src", "RUNNING")])
        assert main.demote_stale_running(d) == 1
        assert _status(d, "a@src") == "INTERRUPTED"
        d.close()

    def test_finished_and_failed_users_are_untouched(self, tmp_path):
        d = _db(tmp_path, [("a@src", "DONE"), ("b@src", "FAILED"),
                           ("c@src", "PENDING")])
        assert main.demote_stale_running(d) == 0
        assert _status(d, "a@src") == "DONE"
        assert _status(d, "b@src") == "FAILED"
        assert _status(d, "c@src") == "PENDING"
        d.close()

    def test_interrupted_not_pending_so_progress_is_kept(self, tmp_path):
        # PENDING would read as "never started" and discard the fact that
        # part of this user's work already landed.
        d = _db(tmp_path, [("a@src", "RUNNING")])
        main.demote_stale_running(d)
        assert _status(d, "a@src") != "PENDING"
        d.close()

    def test_it_says_why_in_the_notes(self, tmp_path):
        d = _db(tmp_path, [("a@src", "RUNNING")])
        main.demote_stale_running(d)
        note = d.conn.execute("SELECT notes FROM identity_map WHERE "
                              "source_email='a@src'").fetchone()["notes"]
        assert "no longer alive" in note
        d.close()

    def test_nothing_running_is_a_no_op(self, tmp_path):
        d = _db(tmp_path, [("a@src", "DONE")])
        assert main.demote_stale_running(d) == 0
        d.close()
