import hashlib
import json

import host_ops


def test_backend_manifest_is_checked_without_reading_tenant_data(tmp_path):
    code = tmp_path / 'workspace_configuration.py'
    code.write_text('a release file')
    manifest = {'commit': 'a' * 40, 'files': {code.name: hashlib.sha256(code.read_bytes()).hexdigest()}}
    (tmp_path / 'CONFIGURATION_RELEASE.json').write_text(json.dumps(manifest))
    r = host_ops.release_check(str(tmp_path))['backend']
    assert r['checked'] == r['total'] == 1 and not r['mismatches']
    code.write_text('changed')
    assert host_ops.release_check(str(tmp_path))['backend']['mismatches'] == [code.name]


def test_paths_outside_the_release_are_refused(tmp_path):
    for path in ('../private', '/etc/passwd'):
        (tmp_path / 'CONFIGURATION_RELEASE.json').write_text(json.dumps({'files': {path: 'a' * 64}}))
        assert 'invalid release file path' in host_ops.release_check(str(tmp_path))['backend']['error']


def test_missing_manifest_does_not_claim_integrity(tmp_path):
    assert host_ops.release_check(str(tmp_path)) == {'backend': None, 'workspace': None, 'originalUiSha256': None}
