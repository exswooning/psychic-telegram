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


def test_the_installed_nondefault_ui_port_is_preserved_when_units_are_refreshed():
    unit = (SCRIPT.parent/'systemd/bitport-webui.service').read_text()
    r = subprocess.run(['bash',str(SCRIPT),'--render-unit','/opt/bitport','8081'],
                       input=unit,text=True,capture_output=True,check=True)
    assert 'webui.py --port 8081' in r.stdout
    assert 'KillMode=process' in r.stdout and 'DISPLAY=:99' in r.stdout


@pytest.mark.parametrize('port',['0','65536','8081; echo unsafe','unknown'])
def test_unit_renderer_rejects_invalid_ports_before_rendering(port):
    r = subprocess.run(['bash',str(SCRIPT),'--render-unit','/opt/bitport',port],
                       input='ExecStart=webui.py --port 8080\n',text=True,capture_output=True)
    assert r.returncode != 0 and not r.stdout


@pytest.mark.parametrize('port',['8080','8081','18181'])
def test_port_is_read_from_the_actual_installed_command(port):
    r = subprocess.run(['bash',str(SCRIPT),'--webui-port'],input=(
        '{ path=/opt/bitport/.venv/bin/python ; argv[]=/opt/bitport/.venv/bin/python '
        f'webui.py --port {port} ; ignore_errors=no ; }}'),text=True,capture_output=True,check=True)
    assert r.stdout.strip()==port


@pytest.mark.parametrize('command',['webui.py','webui.py --port 0','other.py --port 8081',
    'webui.py --port 8081x','webui.py --port 8081 webui.py --port 8082'])
def test_ambiguous_or_invalid_installed_ports_fail_closed(command):
    r = subprocess.run(['bash',str(SCRIPT),'--webui-port'],input=command,text=True,capture_output=True)
    assert r.returncode != 0 and not r.stdout
