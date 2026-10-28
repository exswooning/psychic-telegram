#!/usr/bin/env python3
"""Install one hash-checked static UI at /next, retaining the running /app UI.

Run on l3novo as root after local qualification and GitHub push. Does not
execute staged source code, restart the migration services or write tenant data.
"""
import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

ROOT = Path('/opt/bitport')
CONFIG = Path('/etc/caddy/Caddyfile')
EXPECTED_CONFIG = ':81 {\n\treverse_proxy /api/v2/* 127.0.0.1:8090\n\treverse_proxy /ws 127.0.0.1:8090\n\treverse_proxy /* 127.0.0.1:8081\n}\n'
EXPECTED_ENGINE = '2c45459350861e0849f032bce9711788d23cdcfa'
EXPECTED_UI = '35b89191ca3bed7952043602378f967cd3d34a0e470421e6b3fbb279098378c2'

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def idle():
    busy = []
    commands = subprocess.check_output(['ps', '-eo', 'args='], text=True)
    jobs = r'(?:main|seed_sandbox|reset_target|wipe_target|wipe_tenant|remove_tenant_setup|tally|verify_sample|dms_migrate|full_setup|provision_users|sync_user_profiles|benchmark_run|repair)\.py'
    busy.extend(line[:160] for line in commands.splitlines() if re.search(r'python[\d.]*\s+(?:\S*/)?' + jobs + r'(?:\s|$)', line))
    for path in [ROOT / 'migration.db', *ROOT.glob('data/accounts/*/migration.db')]:
        with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as c:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table, query in [
                ('active_jobs', 'SELECT COUNT(*) FROM active_jobs'),
                ('repair_runs', 'SELECT COUNT(*) FROM repair_runs WHERE finished_at IS NULL'),
                ('job_queue', "SELECT COUNT(*) FROM job_queue WHERE status IN ('queued','running')"),
            ]:
                if table in tables and c.execute(query).fetchone()[0]:
                    busy.append(f'{path.relative_to(ROOT)}: {table} is not idle')
    if busy:
        raise SystemExit('DEPLOY REFUSED: active work: ' + '; '.join(busy))

def tree_hashes(path):
    return {str(p.relative_to(path)): digest(p) for p in path.rglob('*') if p.is_file()}

def services():
    result = {}
    for name in ['bitport-api', 'bitport-webui', 'bitport-cloudflared']:
        if subprocess.check_output(['systemctl', 'is-active', name], text=True).strip() != 'active':
            raise SystemExit(f'Service not healthy: {name}')
        result[name] = subprocess.check_output(['systemctl', 'show', name, '--property=MainPID', '--value'], text=True).strip()
    return result

def fetch(path):
    with urllib.request.urlopen('http://127.0.0.1:81' + path, timeout=10) as response:
        assert response.status == 200
        return response.read()

def main():
    assert os.geteuid() == 0, 'root is required to update the Caddy route'
    assert len(sys.argv) == 3, 'usage: deploy_workspace_preview.py ARCHIVE SHA256'
    archive = Path(sys.argv[1])
    assert archive == Path('/tmp/bitport-workspace-release.tar.gz')
    assert re.fullmatch('[a-f0-9]{64}', sys.argv[2]) and digest(archive) == sys.argv[2], 'archive mismatch'
    assert json.loads((ROOT / 'RELEASE.json').read_text())['commit'] == EXPECTED_ENGINE, 'engine baseline changed'
    assert digest(ROOT / 'migration-webui/dist/index.html') == EXPECTED_UI, 'original UI baseline changed'
    assert CONFIG.read_text() == EXPECTED_CONFIG, 'Caddy config changed; inspect before publishing'
    idle()
    before_services = services()
    original_ui = tree_hashes(ROOT / 'migration-webui/dist')
    stage = Path(tempfile.mkdtemp(prefix='bitport-workspace-stage-'))
    try:
        with tarfile.open(archive) as tar:
            members = tar.getmembers()
            assert sum(m.size for m in members) < 60_000_000
            for m in members:
                p = PurePosixPath(m.name)
                assert m.isfile() and not p.is_absolute() and '..' not in p.parts
                assert m.name == 'MANIFEST.json' or p.parts[0] in {'dist', 'source'}
            tar.extractall(stage, filter='data')
        manifest = json.loads((stage / 'MANIFEST.json').read_text())
        commit = manifest['commit']
        assert re.fullmatch('[a-f0-9]{40}', commit)
        assert manifest['releaseAuthorized'] is True and manifest['tests']['failed'] == 0
        assert manifest['tests']['passed'] >= 1148
        staged = {str(p.relative_to(stage)): digest(p) for p in stage.rglob('*') if p.is_file() and p.name != 'MANIFEST.json'}
        assert staged == manifest['files'], 'staged files differ from manifest'
        index = (stage / 'dist/index.html').read_text()
        assert '/next/assets/' in index and '/app/assets/' not in index
        assert not any('8791' in p.read_text() for p in (stage / 'dist').rglob('*.js')), 'isolated fixture URL in bundle'
        destination = Path('/var/www/bitport-workspace') / commit
        source = ROOT / 'ui-previews' / commit
        assert not destination.exists() and not source.exists(), 'release directory exists; inspect rather than overwrite'
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        backup = ROOT / 'backups' / ('workspace-ui-' + stamp)
        backup.mkdir(parents=True, mode=0o700)
        shutil.copy2(CONFIG, backup / 'Caddyfile')
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        shutil.copytree(stage / 'dist', destination)
        for p in [destination.parent, destination, *destination.rglob('*')]:
            p.chmod(0o755 if p.is_dir() else 0o644)
        source.parent.mkdir(mode=0o700, exist_ok=True)
        shutil.copytree(stage / 'source', source)
        source.chmod(0o700)
        shutil.copy2(stage / 'MANIFEST.json', source / 'UI_MANIFEST.json')
        config = f''':81 {{
    handle /next {{
        redir /next/ 308
    }}
    handle_path /next/* {{
        root * {destination}
        header Cache-Control "no-store"
        @assets path /assets/*
        handle @assets {{
            file_server
        }}
        handle {{
            try_files {{path}} /index.html
            file_server
        }}
    }}
    handle /api/v2/* {{
        reverse_proxy 127.0.0.1:8090
    }}
    handle /ws {{
        reverse_proxy 127.0.0.1:8090
    }}
    handle {{
        reverse_proxy 127.0.0.1:8081
    }}
}}
'''
        candidate = backup / 'Caddyfile.new'
        candidate.write_text(config)
        subprocess.run(['caddy', 'validate', '--config', str(candidate), '--adapter', 'caddyfile'], check=True)
        idle()
        assert services() == before_services
        # Only the web route is reloaded. The API and migration worker remain up.
        try:
            CONFIG.write_text(config)
            subprocess.run(['systemctl', 'reload', 'caddy'], check=True)
            for route in ['/next/login', '/next/overview', '/next/jobs', '/next/tools']:
                assert fetch(route) == (stage / 'dist/index.html').read_bytes(), route
            assets = re.findall(r'(?:src|href)="(/next/assets/[^"#?]+)"', index)
            assert assets
            for url in assets:
                assert fetch(url) == (stage / 'dist' / url.removeprefix('/next/')).read_bytes(), url
            assert tree_hashes(ROOT / 'migration-webui/dist') == original_ui
            assert services() == before_services, 'a protected service restarted'
            assert tree_hashes(destination) == {n.removeprefix('dist/'): h for n, h in staged.items() if n.startswith('dist/')}
            idle()
        except BaseException:
            shutil.copy2(backup / 'Caddyfile', CONFIG)
            subprocess.run(['systemctl', 'reload', 'caddy'], check=True)
            raise
        receipt = {'commit': commit, 'previewPath': '/next/', 'destination': str(destination), 'source': str(source), 'backup': str(backup), 'tests': manifest['tests'], 'originalUIUnchanged': True, 'migrationServicesNotRestarted': True, 'tenantDataWritten': False, 'at': datetime.datetime.now(datetime.timezone.utc).isoformat()}
        (ROOT / 'UI_PREVIEW_RELEASE.json').write_text(json.dumps(receipt, indent=2) + '\n')
        Path('/tmp/bitport-workspace-rollout.json').write_text(json.dumps(receipt, indent=2) + '\n')
        print(json.dumps(receipt, indent=2))
    finally:
        shutil.rmtree(stage)

if __name__ == '__main__':
    main()
