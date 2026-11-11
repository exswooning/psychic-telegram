"""Identify the concrete comparison code and observation interval; no tenant reads."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from uuid import uuid4

FILES = ('verify_sample.py', 'verification_coverage.py', 'verification_cache.py',
         'native_fidelity.py', 'native_repair.py', 'link_rewrite.py',
         'verification_provenance.py')


def utc():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def begin(root, users, services, *, limit=None):
    root = Path(root)
    hashes, unavailable = {}, []
    for name in FILES:
        try:
            hashes[name] = hashlib.sha256((root / name).read_bytes()).hexdigest()
        except OSError:
            unavailable.append(name)
    try:
        claim = (root / 'DEPLOYED_COMMIT').read_text().strip()[:64]
    except OSError:
        claim = 'unknown'
    from verification_cache import MAIL_COMPARISON_VERSION
    return {'schema': 1, 'runId': uuid4().hex, 'startedAt': utc(),
            'claimedRelease': claim, 'fileHashes': hashes, 'unavailableFiles': unavailable,
            'codeDigest': hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(),
            'mailComparisonVersion': MAIL_COMPARISON_VERSION,
            'users': list(users), 'services': list(services), 'checkLimit': limit,
            'codeObservation': 'comparison files at run start; not an attestation of already-loaded modules',
            'observationMode': 'live interval; not an atomic source snapshot'}


def for_service(run, user, target, service, started):
    return {**run, 'sourceUser': user, 'targetUser': target, 'service': service,
            'serviceStartedAt': started, 'serviceFinishedAt': utc()}
