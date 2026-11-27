from types import SimpleNamespace
import pytest
import ab_transfer as AB
from benchmark_run import judge


def test_reset_failure_cannot_clear_ledger_or_launch_migration(monkeypatch):
    calls=[]
    monkeypatch.setattr(AB,'run_tail',lambda argv,env:calls.append(argv) or (2,'refused'))
    monkeypatch.setattr(AB,'MigrationDB',lambda *a:pytest.fail('ledger must not be opened after refused reset'))
    with pytest.raises(RuntimeError,match='ledger is unchanged'):
        AB.one_mode('server_side',SimpleNamespace(target_domain='t.test'),None,['u@s.test'],['u@t.test'],{})
    assert len(calls)==1 and 'reset_target.py' in calls[0]


def test_even_one_missing_grant_cannot_pass_a_benchmark():
    result={'migrateReturnCode':0,'totalFiles':1000,'checksumFailures':0,'rateLimitHits':0,'users':{},
            'acl':{'extraGrants':0,'fidelityPct':99.99,'missingGrants':1,'missingFiles':0}}
    passed,reasons=judge(result,set())
    assert not passed and any('FIDELITY' in reason for reason in reasons)


def test_a_benchmark_refuses_an_account_without_both_tenants_set_up(monkeypatch, tmp_path):
    """A benchmark wipes the target: an account missing a side, or sharing the control plane's database as its
    ledger, must never reach the wipe."""
    import accounts_auth, config, control_plane_db
    from benchmark_run import benchmark_scope

    monkeypatch.delenv("BITPORT_LEDGER", raising=False)
    ledger = str(tmp_path / "migration.db")
    monkeypatch.setattr(config, "Settings", lambda account_id=None: SimpleNamespace(
        source_domain="a.test", target_domain="b.test", db_path=ledger))
    monkeypatch.setattr(control_plane_db, "_db_path", lambda: str(tmp_path / "control.db"))
    monkeypatch.setattr(accounts_auth, "get_account", lambda a: {"plan": "pro"})
    monkeypatch.setattr(accounts_auth, "get_tenant_config", lambda a, side: None if side == "source" else {})
    with pytest.raises(ValueError, match="no complete source configuration"):
        benchmark_scope(5, "b.test")
    monkeypatch.setattr(control_plane_db, "_db_path", lambda: ledger)
    with pytest.raises(ValueError, match="separate migration ledger"):
        benchmark_scope(5, "b.test")
