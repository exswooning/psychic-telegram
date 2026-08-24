"""A label mapping that outlived the label it names.

label_map records a TARGET label id, and nothing checked the label was still
there. When a target account is deleted and recreated -- all 200 of them on
2026-08-21 -- the recreated mailbox has entirely different label ids, so
every mapping points into the deleted account. _map_label_ids hands those
dead ids to messages.insert and Gmail rejects the whole message with
"Invalid label": 32,967 messages lost in one run, every one retryable, none
reported as a label problem.
"""
import db as dbmod


def _db(tmp_path):
    d = dbmod.MigrationDB(str(tmp_path / "m.db"))
    d.conn.execute("INSERT INTO identity_map(source_email,target_email) "
                   "VALUES('u@src','u@tgt')")
    d.conn.commit()
    return d


class TestForgettingAUserClearsBothMaps:
    def test_label_map_is_cleared_with_id_mapping(self, tmp_path):
        """Both record a target id and both die for the same reason."""
        d = _db(tmp_path)
        d.record_mapping("u@src", "s1", "t1", "file")
        d.record_label("u@src", "Label_1", "Label_9", "Clients")
        d.forget_mappings("u@src")
        assert d.get_label_map("u@src") == {}
        d.close()

    def test_another_users_labels_survive(self, tmp_path):
        d = _db(tmp_path)
        d.record_label("u@src", "Label_1", "Label_9", "Clients")
        d.record_label("other@src", "Label_1", "Label_4", "Clients")
        d.forget_mappings("u@src")
        assert d.get_label_map("other@src") == {"Label_1": "Label_4"}
        d.close()

    def test_the_audit_history_is_untouched(self, tmp_path):
        """It is the evidence of what happened; a migration that erases its
        own history cannot explain itself afterwards."""
        d = _db(tmp_path)
        d.record_mapping("u@src", "s1", "t1", "file")
        d.log_audit("u@src", "s1", "file", "SUCCESS")
        d.forget_mappings("u@src")
        n = d.conn.execute(
            "SELECT COUNT(*) c FROM audit_log WHERE source_user='u@src'"
        ).fetchone()["c"]
        assert n == 1
        d.close()


class TestForgetOneLabel:
    def test_it_removes_only_that_mapping(self, tmp_path):
        d = _db(tmp_path)
        d.record_label("u@src", "Label_1", "Label_9", "Clients")
        d.record_label("u@src", "Label_2", "Label_8", "Projects")
        d.forget_label("u@src", "Label_1")
        assert d.get_label_map("u@src") == {"Label_2": "Label_8"}
        d.close()

    def test_forgetting_an_unknown_label_is_harmless(self, tmp_path):
        d = _db(tmp_path)
        d.record_label("u@src", "Label_1", "Label_9", "Clients")
        d.forget_label("u@src", "Label_404")
        assert d.get_label_map("u@src") == {"Label_1": "Label_9"}
        d.close()
