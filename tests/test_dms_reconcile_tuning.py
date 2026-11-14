"""The DMS reconciliation reads at a useful fraction of Gmail's quota.

13 Nov 2026 (account 3): it ran two threads per mailbox behind the engine's 4-calls-a-second limiter,
about 2 messages a second -- 14,000 messages in two hours, a projected 20 hours for 141,000. It only
reads (one raw read on each tenant and one search per message), and Gmail allows a user about fifty
such reads a second, so it has its own threads and its own rate."""
import split_mail


def test_defaults_are_several_times_the_old_two_threads(monkeypatch):
    monkeypatch.delenv("DMS_RECONCILE_WORKERS", raising=False)
    monkeypatch.delenv("DMS_RECONCILE_QPS", raising=False)
    workers, qps = split_mail.reconcile_tuning()
    assert workers == 8 and qps == 20.0


def test_the_operator_can_tune_it_within_bounds(monkeypatch):
    monkeypatch.setenv("DMS_RECONCILE_WORKERS", "100")
    monkeypatch.setenv("DMS_RECONCILE_QPS", "500")
    assert split_mail.reconcile_tuning() == (16, 40.0), "never past what one Gmail user allows"
    monkeypatch.setenv("DMS_RECONCILE_WORKERS", "0")
    monkeypatch.setenv("DMS_RECONCILE_QPS", "junk")
    assert split_mail.reconcile_tuning() == (1, 20.0)
