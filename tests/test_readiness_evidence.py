import copy
import json
from pathlib import Path

import pytest
import readiness_report as R
import verification_provenance as P
import native_repair as N


def complete(tmp_path):
    for name in P.FILES:
        (tmp_path / name).write_text('comparison code')
    p = P.begin(tmp_path, ['u@s'], R.SERVICES)
    result = dict(checked=1, identical=1, coverageComplete=True, **{k: [] for k in R.ISSUES})
    users = {'u@s': {svc: {**result, 'provenance': P.for_service(p, 'u@s', 'u@t', svc, p['startedAt'])}
                     for svc in R.SERVICES}}
    return {'users': users, 'coverage': {'completion': 'COMPLETE'}}, p


def test_full_evidence_is_not_a_production_or_full_fidelity_certificate(tmp_path):
    report, p = complete(tmp_path)
    result = R.assess(report, ['u@s'])
    assert result['status'] == 'VERIFICATION_EVIDENCE_COMPLETE'
    assert not result['overallReadinessEstablished'] and result['remainingGates']
    assert p['claimedRelease'] == 'unknown'


@pytest.mark.parametrize('mutate', [
    lambda r: r['users']['u@s']['gmail'].update(extras=[{'id': 'extra'}]),
    lambda r: r['users']['u@s']['gmail'].pop('errors'),
    lambda r: r['users']['u@s']['drive'].update(unverified=[{'state': 'timeout'}]),
    lambda r: r['users']['u@s']['drive'].pop('provenance'),
    lambda r: r.update(verifyLimit=25),
    lambda r: r['users']['u@s'].pop('tasks'),
    lambda r: r['users']['u@s']['gmail']['provenance'].update(sourceUser='other@s'),
    lambda r: r['users']['u@s']['gmail']['provenance'].update(codeDigest='different'),
    lambda r: r['users']['u@s']['gmail']['provenance'].update(fileHashes=['not-a-manifest']),
    lambda r: r['users']['u@s']['gmail'].update(sampled={'of': 100, 'checked': 1}),
])
def test_incomplete_or_contradictory_evidence_blocks_even_if_verdict_claims_identical(tmp_path, mutate):
    report, _ = complete(tmp_path)
    report['verdict'] = 'IDENTICAL'
    mutate(report)
    assert R.assess(report, ['u@s'])['status'] == 'BLOCKED'


def test_expected_inventory_cannot_be_inferred_from_a_partial_run(tmp_path):
    report, _ = complete(tmp_path)
    assert R.assess(report, ['u@s', 'missing@s'])['status'] == 'BLOCKED'
    assert R.assess(report, [])['status'] == 'BLOCKED'


def test_code_drift_changes_digest_even_with_same_release_claim(tmp_path):
    _, p = complete(tmp_path)
    (tmp_path / 'native_repair.py').write_text('changed')
    q = P.begin(tmp_path, ['u@s'], R.SERVICES)
    assert p['codeDigest'] != q['codeDigest']
    assert p['runId'] != q['runId']


def test_sheet_diagnostics_locate_cell_and_field_without_leaking_contents():
    src = {'sheets': [{'properties': {'sheetId': 1, 'title': 'Tab'},
                      'data': [{'startRow': 9, 'startColumn': 26, 'rowData': [
                          {'values': [{'userEnteredValue': {'formulaValue': '=PRIVATE_SOURCE'}}]}]}]}]}
    tgt = copy.deepcopy(src)
    tgt['sheets'][0]['data'][0]['rowData'][0]['values'][0]['userEnteredValue']['formulaValue'] = '=PRIVATE_TARGET'
    before = copy.deepcopy(src)
    out = N.compare_sheets(src, tgt, lambda _: None)
    assert any('AA10.userEnteredValue.formulaValue' in line for line in out)
    assert 'PRIVATE' not in json.dumps(out)
    assert src == before
    assert N.compare_sheets(src, src, lambda _: None) == []


def test_sheet_diagnostics_are_bounded():
    src = {'sheets': [], 'properties': {str(i): i for i in range(1000)}}
    tgt = {'sheets': [], 'properties': {str(i): -1 for i in range(1000)}}
    assert len(N.compare_sheets(src, tgt, lambda _: None)) <= 21


def test_simultaneous_reports_do_not_overwrite_history_or_share_temp_files(tmp_path, monkeypatch):
    import verify_sample as V
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setattr(V, 'to_markdown', lambda r: str(r['number']))
    def save(number):
        return V.save({'generatedAt': '2026-11-11T00:00:00Z', 'number': number}, str(tmp_path))[0]
    with ThreadPoolExecutor(max_workers=4) as pool:
        paths = list(pool.map(save, range(4)))
    assert len(set(paths)) == 4
    assert {json.loads(Path(p).read_text())['number'] for p in paths} == set(range(4))
    assert json.loads((tmp_path / 'latest.json').read_text())['number'] in range(4)
    assert not list(tmp_path.glob('*.tmp'))


def test_service_provenance_exists_before_incremental_save(auth, db, settings, identity, monkeypatch):
    import verify_sample as V
    from tests.conftest import SRC_USER
    seen = []
    def empty(self):
        result = self._blank()
        result.update(emptySource=True, coverageComplete=True)
        return result
    monkeypatch.setattr(V.Verifier, 'gmail', empty)
    def save(user, service, result):
        seen.append(result['provenance'])
        V.save_user_results(db, {'users': {user: {service: result}}})
    report = V.run(auth, db, settings, [SRC_USER], ('gmail',), progress=lambda *a: None, on_service=save)
    assert seen and seen[0]['runId'] == report['provenance']['runId']
    payload = json.loads(db.conn.execute('select payload from user_verification').fetchone()[0])
    assert payload['provenance']['codeDigest'] == report['provenance']['codeDigest']
    assert seen[0]['sourceUser'] == SRC_USER and seen[0]['service'] == 'gmail'
