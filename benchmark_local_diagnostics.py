"""Read-only local-model evaluation, never a tenant operation or model switch.

Pass a JSON array of redacted labelled cases: id, message, category, expected.
Outputs model answers for human grounding review plus measured latency/tokens.
Synthetic or schema-only results cannot qualify a model for unattended fixes.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import time

import local_diagnostics as D


def load_cases(path):
    with Path(path).open('rb') as stream:
        data = stream.read(256_001)
    if len(data) > 256_000:
        raise ValueError('case file too large')
    cases = json.loads(data)
    if not isinstance(cases, list) or not 1 <= len(cases) <= 100:
        raise ValueError('supply 1 to 100 cases')
    ids = set()
    for case in cases:
        if not isinstance(case, dict) or not all(isinstance(case.get(k), str) and case[k].strip()
                                               for k in ('id', 'message', 'category', 'expected')):
            raise ValueError('each case needs id, message, category and expected explanation')
        if case['id'] in ids:
            raise ValueError('case IDs must be unique')
        ids.add(case['id'])
    return cases, hashlib.sha256(data).hexdigest()


def evaluate(cases, model, threads, *, hold=D.resource_hold, ask=None):
    results = []
    old = os.environ.get('AUTOPILOT_MODEL_THREADS')
    os.environ['AUTOPILOT_MODEL_THREADS'] = str(threads)
    try:
        for case in cases:
            reason = hold()
            if reason:
                return {'model': model, 'threads': threads, 'status': 'HELD',
                        'reason': reason, 'results': results}
            evidence = {'id': 'E1', 'message': D.redact(case['message'], identities=True)}
            started = time.monotonic()
            row = {'case': case['id'], 'category': case['category'], 'review': 'REQUIRED'}
            try:
                response = ask(evidence, model) if ask else D.request_model(evidence, 'http://127.0.0.1:11434', model)
                row.update(diagnosis=D.validate(response['text']), usage=response.get('usage', {}), valid=True)
            except Exception as exc:
                row.update(valid=False, error=type(exc).__name__)
            row['seconds'] = round(time.monotonic() - started, 3)
            results.append(row)
    finally:
        if old is None: os.environ.pop('AUTOPILOT_MODEL_THREADS', None)
        else: os.environ['AUTOPILOT_MODEL_THREADS'] = old
    return {'model': model, 'threads': threads, 'effectiveThreads': min(threads, os.cpu_count() or 1),
            'status': 'MEASURED_NOT_QUALIFIED', 'cases': len(results),
            'schemaValid': sum(r['valid'] for r in results),
            'medianSeconds': statistics.median(r['seconds'] for r in results),
            'results': results, 'modelSwitchAuthorizedByThisReport': False}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('cases')
    p.add_argument('--model', required=True)
    p.add_argument('--threads', type=int, choices=(2, 4, 8), default=2)
    p.add_argument('--run', action='store_true', help='Run inference only if the laptop is idle')
    a = p.parse_args(argv)
    cases, digest = load_cases(a.cases)
    out = evaluate(cases, a.model, a.threads) if a.run else {'status': 'VALIDATED_INPUT_ONLY', 'cases': len(cases)}
    out['caseDigest'] = digest
    out['fullEvaluationSize'] = len(cases) >= 50
    print(json.dumps(out, indent=2))
    return 1 if out['status'] == 'HELD' else 0


if __name__ == '__main__':
    raise SystemExit(main())
