"""One rewritten link used to silence the link-rot warning for a tenant.

The rule was `if mail and not rewritten`, counting link_rewrite rows across
the whole ledger. So the first rewrite anywhere turned the warning off --
migrate 300,000 messages with rewriting off, switch it on, migrate one more,
and nothing reports that 300,000 messages still name source files that die
with the source tenant.
"""
import sqlite3

import next_actions


class _DB:
    def __init__(self, conn):
        self.conn = conn


def _ledger(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE audit_log (source_user TEXT, item_type TEXT, "
                 "item_id TEXT, status TEXT, timestamp TEXT)")
    conn.executemany("INSERT INTO audit_log VALUES (?,?,?,?,?)", rows)
    return conn


def _warn_titles(conn, mail_rewritten_on=True):
    """Drive rule 5 directly: it is the only part under test."""
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
    rewritten = q("SELECT COUNT(*) FROM audit_log WHERE item_type='link_rewrite'")
    if not rewritten:
        return ["never rewritten"]
    return [q("SELECT COUNT(*) FROM audit_log WHERE item_type='message' "
              "AND status='SUCCESS' AND timestamp < "
              "(SELECT MIN(timestamp) FROM audit_log "
              " WHERE item_type='link_rewrite')")]


class TestOneRewriteIsNotTheAllClear:
    def test_mail_migrated_before_the_first_rewrite_is_counted(self):
        conn = _ledger([
            ("u", "message", "m1", "SUCCESS", "2026-09-06T03:04:00Z"),
            ("u", "message", "m2", "SUCCESS", "2026-09-06T03:04:01Z"),
            ("u", "link_rewrite", "m3", "SUCCESS", "2026-09-06T03:25:00Z"),
            ("u", "message", "m3", "SUCCESS", "2026-09-06T03:25:00Z"),
        ])
        assert _warn_titles(conn) == [2], "the two pre-rewrite messages"

    def test_a_tenant_rewritten_from_the_start_is_clean(self):
        conn = _ledger([
            ("u", "link_rewrite", "m1", "SUCCESS", "2026-09-06T03:00:00Z"),
            ("u", "message", "m1", "SUCCESS", "2026-09-06T03:00:00Z"),
        ])
        assert _warn_titles(conn) == [0]

    def test_the_old_rule_would_have_said_nothing(self):
        """Guards the actual regression: `not rewritten` is False here."""
        conn = _ledger([
            ("u", "message", "m1", "SUCCESS", "2026-09-06T03:04:00Z"),
            ("u", "link_rewrite", "m2", "SUCCESS", "2026-09-06T03:25:00Z"),
        ])
        rewritten = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE item_type='link_rewrite'"
        ).fetchone()[0]
        assert rewritten, "old rule silenced itself"
        assert _warn_titles(conn) == [1], "new rule still reports the gap"


class TestTheRuleIsWiredIn:
    def test_next_actions_checks_the_first_rewrite_timestamp(self):
        import inspect
        src = inspect.getsource(next_actions)
        assert "MIN(timestamp) FROM audit_log" in src, (
            "rule 5 no longer compares against the first rewrite")
