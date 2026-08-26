"""An account without Tasks is not a failing migration.

Google Tasks answers 404 on users/@me/lists for an account the service was
never provisioned for. One user's two lists failed that way on every single
run, were retried forever, and sat in the UI as work needing a person.
Eight other accounts on the same tenant listed fine, so it is the account,
not the tenant, and no retry or code change reaches it.
"""
import db as dbmod
from tasks_engine import TasksMigrator


class _Lists:
    def __init__(self, items, error=None):
        self._items, self._error = items, error

    def list(self, **kw):
        outer = self

        class _R:
            def execute(self):
                if outer._error:
                    raise outer._error
                return {"items": outer._items}
        return _R()

    def insert(self, body=None):
        raise AssertionError("must not write to an unprovisioned account")


class _Svc:
    def __init__(self, items, error=None):
        self._l = _Lists(items, error)

    def tasklists(self):
        return self._l

    def tasks(self):
        return self._l


class _Auth:
    def __init__(self, src, tgt):
        self._src, self._tgt = src, tgt

    def source_tasks(self, u):
        return self._src

    def target_tasks(self, u):
        return self._tgt


class _Settings:
    dry_run = False
    per_user_qps = 5
    max_retries = 1
    base_backoff = 0.0
    max_backoff = 0.0


def _mig(tmp_path, target_error):
    d = dbmod.MigrationDB(str(tmp_path / "m.db"))
    src = _Svc([{"id": "L1", "title": "My Tasks"},
                {"id": "L2", "title": "Other"}])
    tgt = _Svc([], error=target_error)
    return d, TasksMigrator(_Auth(src, tgt), d, _Settings(), "u@src", "u@tgt")


def _statuses(d):
    return [r["status"] for r in d.conn.execute(
        "SELECT status FROM audit_log WHERE item_type='task_list'")]


class TestAnUnprovisionedTargetIsSkipped:
    def test_a_404_records_skips_not_failures(self, tmp_path):
        d, m = _mig(tmp_path, Exception("HttpError 404 ... Not Found"))
        out = m.run()
        assert _statuses(d) == ["SKIPPED_SERVICE_UNAVAILABLE"] * 2
        assert out["failed"] == 0 and out["skipped"] == 2
        d.close()

    def test_it_says_why(self, tmp_path):
        d, m = _mig(tmp_path, Exception("HttpError 404 ... Not Found"))
        m.run()
        msg = d.conn.execute("SELECT error_message FROM audit_log "
                             "WHERE item_type='task_list' LIMIT 1").fetchone()[0]
        assert "not enabled on the target account" in msg
        d.close()

    def test_nothing_is_written_to_that_account(self, tmp_path):
        # _Lists.insert asserts if called.
        d, m = _mig(tmp_path, Exception("HttpError 404 ... Not Found"))
        m.run()
        d.close()


class TestOtherRefusalsAreNotTreatedAsProvisioning:
    def test_a_transient_error_does_not_become_a_permanent_skip(self, tmp_path):
        # A quota or network blip says nothing about whether Tasks exists;
        # turning it into a skip would quietly abandon real work.
        d, m = _mig(tmp_path, Exception("HttpError 429 rateLimitExceeded"))
        assert m._target_has_tasks() is True
        d.close()

    def test_a_healthy_target_proceeds_normally(self, tmp_path):
        d, m = _mig(tmp_path, None)
        assert m._target_has_tasks() is True
        d.close()
