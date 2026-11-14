"""Exercise the actual remote-process filter with the commands this campaign runs."""
from pathlib import Path
import re
import subprocess
import pytest


@pytest.mark.parametrize('args', [
    '/opt/bitport/.venv/bin/python -u /opt/bitport/sandbox_campaign.py --directory /private/run',
    '/opt/bitport/.venv/bin/python -u main.py --account-id 3 migrate --ordered',
    '/opt/bitport/.venv/bin/python3.14 -B -u verify_sample.py --account-id 3',
    '/opt/bitport/.venv/bin/python dms_migrate.py --apply',
    '/opt/bitport/.venv/bin/python -u tenant_verify.py --account-id 3',
    '/opt/bitport/.venv/bin/python main.py --account-id 3 migrate',
])
def test_normal_deploy_gate_recognizes_every_active_campaign_phase(args):
    source=(Path(__file__).resolve().parents[1]/'sync_vps.sh').read_text()
    pattern=re.search(r"ps -eo pid=,args= \| grep -iE '([^']+)'",source).group(1)
    result=subprocess.run(['grep','-iE',pattern],input='12345 '+args+'\n',text=True,capture_output=True)
    assert result.returncode==0, result.stderr


def test_gate_does_not_mistake_its_own_ssh_command_for_a_python_job():
    source=(Path(__file__).resolve().parents[1]/'sync_vps.sh').read_text()
    pattern=re.search(r"ps -eo pid=,args= \| grep -iE '([^']+)'",source).group(1)
    result=subprocess.run(['grep','-iE',pattern],input='123 ssh server "python -u sandbox_campaign.py"\n',text=True,capture_output=True)
    assert result.returncode==1
