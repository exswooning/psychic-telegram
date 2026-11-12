from pathlib import Path
import subprocess
import pytest

SCRIPT=Path(__file__).resolve().parents[1]/'sync_vps.sh'

@pytest.mark.parametrize('dest',['/opt/bitport','/srv/bitport'])
def test_unit_paths_follow_destination_without_changing_other_policy(dest):
    unit='[Service]\nWorkingDirectory=/root/migration\nExecStart=/root/migration/.venv/bin/python api_server.py\nKillMode=process\nEnvironment=PROTECTED_DOMAINS=example.org\n'
    r=subprocess.run(['bash',str(SCRIPT),'--render-unit',dest],input=unit,text=True,capture_output=True,check=True)
    assert '/root/migration' not in r.stdout
    assert 'WorkingDirectory='+dest in r.stdout
    assert 'KillMode=process' in r.stdout and 'PROTECTED_DOMAINS=example.org' in r.stdout
