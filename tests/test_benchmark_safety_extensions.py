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
