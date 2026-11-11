"""Audit a saved verification against an explicit tenant inventory, offline.

Does not call Google, enable repairs, close findings or certify overall readiness.
A successful evidence check still needs deployment, snapshot stability, recovery
and end-to-end qualification from the readiness plan.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import re

from verification_provenance import FILES

SERVICES = ('drive', 'gmail', 'calendar', 'contacts', 'tasks', 'directory')
ISSUES = ('differences', 'missing', 'duplicates', 'extras', 'notCopied', 'errors', 'unverified')


def assess(report, expected_users, services=SERVICES):
    reasons, rows = [], []
    users = report.get('users') or {}
    expected = set(expected_users)
    if len(expected) != len(expected_users):
        reasons.append('Expected tenant inventory contains duplicate users')
    if not expected:
        reasons.append('Expected tenant inventory is empty or unknown')
    if set(users) != expected:
        reasons.append('Checked users do not equal the explicit tenant inventory')
    if report.get('verifyLimit') or report.get('sampleLimit'):
        reasons.append('Report is sampled; it cannot establish complete tenant parity')
    if (report.get('coverage') or {}).get('completion') != 'COMPLETE':
        reasons.append('Run coverage is incomplete or unknown')
    code_digests = set()
    for user in sorted(expected):
        for service in services:
            r = users.get(user, {}).get(service)
            blockers = []
            if not isinstance(r, dict):
                blockers.append('No saved check')
            else:
                for category in ISSUES:
                    items = r.get(category)
                    if not isinstance(items, list):
                        blockers.append(f'{category}: evidence missing or invalid')
                    elif items:
                        blockers.append(f'{category}: {len(items)}')
                if r.get('coverageComplete') is not True:
                    blockers.append('Full feature coverage not established')
                if type(r.get('checked')) is not int or r['checked'] < 0:
                    blockers.append('Checked count unavailable')
                elif not r['checked'] and not r.get('emptySource'):
                    blockers.append('No items checked; empty source not established')
                elif r.get('identical') != r['checked']:
                    blockers.append('Not every checked item matched')
                sample = r.get('sampled') or {}
                if sample and sample.get('of') != sample.get('checked', r.get('checked')):
                    blockers.append('Item sample does not cover inventory')
                provenance = r.get('provenance') or {}
                if not all(provenance.get(k) for k in ('runId', 'codeDigest', 'fileHashes',
                                                       'serviceStartedAt', 'serviceFinishedAt', 'mailComparisonVersion')):
                    blockers.append('Comparison provenance missing')
                hashes = provenance.get('fileHashes')
                if not isinstance(hashes, dict) or not hashes:
                    blockers.append('Comparison manifest missing or invalid')
                else:
                    if set(hashes) != set(FILES) or any(not isinstance(v, str) or not re.fullmatch(r'[0-9a-f]{64}', v) for v in hashes.values()):
                        blockers.append('Comparison manifest files or hashes incomplete')
                    digest = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
                    if digest != provenance.get('codeDigest'):
                        blockers.append('Comparison manifest digest mismatch')
                if provenance.get('unavailableFiles'):
                    blockers.append('Comparison code inventory incomplete')
                if provenance.get('sourceUser') != user or provenance.get('service') != service:
                    blockers.append('Provenance does not identify this user/service')
                if provenance.get('checkLimit'):
                    blockers.append('Provenance records a limited check')
                if provenance.get('codeDigest'):
                    code_digests.add(provenance['codeDigest'])
            rows.append({'user': user, 'service': service, 'blockers': blockers})
    if len(code_digests) > 1:
        reasons.append('Results use different comparison code; do not combine them into one proof')
    clean = sum(not row['blockers'] for row in rows)
    complete = bool(rows) and clean == len(rows) and not reasons
    return {'status': 'VERIFICATION_EVIDENCE_COMPLETE' if complete else 'BLOCKED',
            'expectedUsers': len(expected), 'expectedServiceChecks': len(rows),
            'checksWithoutBlockers': clean, 'reasons': reasons, 'checks': rows,
            'overallReadinessEstablished': False,
            'remainingGates': ['Stable source snapshot/cutoff and repeatability',
                               'Exact deployed baseline and rollback qualification',
                               'Zero unresolved DMS delivery failures',
                               'Capability contract and explicit accepted differences',
                               'Recovery, resource and end-to-end qualification']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', help='Saved full verification JSON')
    parser.add_argument('--users', required=True, help='JSON array of expected source users')
    args = parser.parse_args(argv)
    with open(args.report, encoding='utf-8') as f:
        report = json.load(f)
    with open(args.users, encoding='utf-8') as f:
        users = json.load(f)
    if not isinstance(users, list) or not all(isinstance(u, str) and u for u in users):
        parser.error('users must be a nonempty array of user identities')
    result = assess(report, users)
    print(json.dumps(result, indent=2))
    return 0 if result['status'] == 'VERIFICATION_EVIDENCE_COMPLETE' else 1


if __name__ == '__main__':
    raise SystemExit(main())
