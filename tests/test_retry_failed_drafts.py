"""Live, dharan.gov.np pilot: 12 drafts refused before the account's licence stayed FAILED
after every message around them was retried -- nothing re-ran the drafts pass."""
import retry_failed


def test_failed_drafts_rerun_the_drafts_pass(db, settings, identity):
    db.log_audit("alice@tenanta.com", "r123", "draft", "FAILED", "Mail service not enabled")
    ran = []

    class Gmail:
        def __init__(self, *a):
            pass

        def _migrate_drafts(self):
            ran.append(1)
    out = retry_failed.retry(object(), db, settings, apply=True, _gmail_cls=Gmail)
    assert ran == [1] and out["drafts"] == 1 and out["retried"] == 1
