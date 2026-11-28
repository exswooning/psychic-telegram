import json
import pytest
import benchmark_local_diagnostics as B

CASES = [{'id': 'c1', 'message': 'KeyError: detail', 'category': 'code', 'expected': 'Missing optional key'}]


def test_busy_laptop_never_loads_model():
    out = B.evaluate(CASES, 'm', 8, hold=lambda: 'verification active', ask=lambda *a: pytest.fail('model loaded'))
    assert out['status'] == 'HELD' and not out['results']


def test_valid_schema_cannot_qualify_a_model_and_thread_override_is_restored(monkeypatch):
    monkeypatch.setenv('AUTOPILOT_MODEL_THREADS', '2')
    response = json.dumps({**{k: 'unknown' for k in B.D.FIELDS - {'evidence_ids'}}, 'evidence_ids': ['E1']})
    def ask(evidence, model):
        assert B.os.environ['AUTOPILOT_MODEL_THREADS'] == '4'
        return {'text': response, 'usage': {'eval_count': 12}}
    out = B.evaluate(CASES, 'm', 4, hold=lambda: '', ask=ask)
    assert out['status'] == 'MEASURED_NOT_QUALIFIED' and out['schemaValid'] == 1
    assert out['results'][0]['review'] == 'REQUIRED'
    assert not out['modelSwitchAuthorizedByThisReport']
    assert B.os.environ['AUTOPILOT_MODEL_THREADS'] == '2'


def test_dataset_requires_distinct_labelled_cases(tmp_path):
    p = tmp_path / 'cases.json'
    p.write_text(json.dumps(CASES * 2))
    with pytest.raises(ValueError, match='unique'):
        B.load_cases(p)
