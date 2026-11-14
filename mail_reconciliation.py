"""Durable read-only-Google reconciliation evidence. Checkpoints are never parity verdicts."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path

from db import utc_now


class Checkpoint:
    def __init__(self, gm):
        self.gm = gm
        self.context = None
        self.reused = 0
        with gm.db.write() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS mail_reconciliation_items (
                source_user TEXT, source_id TEXT, target_id TEXT, context TEXT,
                outcome TEXT NOT NULL, detail TEXT NOT NULL, checked_at TEXT NOT NULL,
                PRIMARY KEY(source_user,source_id))''')
        try:
            anchors = []
            for api in (gm.src, gm.tgt):
                value = gm._retry(lambda: api.users().getProfile(userId='me').execute()).get('historyId')
                if not str(value or '').isdigit():
                    raise ValueError('No mailbox history')
                anchors.append(str(value))
            root = Path(__file__).parent
            hashes = {n: hashlib.sha256((root/n).read_bytes()).hexdigest() for n in
                      ('mail_reconciliation.py','split_mail.py','migration_checks.py','verify_sample.py')}
            mappings = [tuple(r) for r in gm.db.conn.execute(
                'SELECT source_user,source_id,target_id,type FROM id_mapping WHERE type!=? ORDER BY 1,2,4', ('message',))]
            self.context = hashlib.sha256(json.dumps({
                'history': anchors, 'target': gm.target_user, 'code': hashes,
                'rewrite': gm.settings.rewrite_drive_links, 'labels': gm.db.get_label_map(gm.source_user),
                'driveMappings': mappings}, sort_keys=True, default=str).encode()).hexdigest()
        except Exception:
            # No complete history/code evidence: do real reads; do not reuse.
            pass

    def get(self, sid):
        if not self.context:
            return None
        row = self.gm.db.conn.execute('''SELECT target_id FROM mail_reconciliation_items
            WHERE source_user=? AND source_id=? AND context=? AND outcome='VERIFIED' ''',
            (self.gm.source_user, sid, self.context)).fetchone()
        if not row or self.gm.db.get_target_id(self.gm.source_user, sid, 'message') != row[0]:
            return None
        receipt = self.gm.db.conn.execute('''SELECT status,target_id FROM migration_item_receipts
            WHERE source_user=? AND source_id=? AND service='gmail' ''', (self.gm.source_user,sid)).fetchone()
        if not receipt or tuple(receipt) != ('VERIFIED', row[0]):
            return None
        self.reused += 1
        return row[0]

    def save(self, sid, tid, outcome, detail):
        with self.gm.db.write() as c:
            c.execute('INSERT OR REPLACE INTO mail_reconciliation_items VALUES(?,?,?,?,?,?,?)',
                      (self.gm.source_user, sid, tid, self.context, outcome,
                       json.dumps(detail, sort_keys=True), utc_now()))


def outcome(error):
    text = str(error).lower()
    if 'absent or ambiguous' in text:
        return 'AMBIGUOUS_OR_MISSING'
    if 'message-id' in text:
        return 'IDENTITY_UNAVAILABLE'
    if 'content or labels' in text:
        return 'COMPARISON_DIFFERENCE'
    if 'link' in text:
        return 'REFERENCE_UNAVAILABLE'
    return 'READ_OR_ACCESS_ERROR'


def inventory(gm):
    """Complete source IDs and draft backing messages; pagination failures stay failures."""
    def pages(resource, kind):
        token, seen, ids = None, set(), set()
        while True:
            gm.limiter.acquire()
            response = gm._retry(lambda: resource.list(userId='me', maxResults=100 if kind=='drafts' else 500,
                                                       pageToken=token, includeSpamTrash=True).execute())
            for item in response.get(kind, []):
                mid = (item.get('message') or {}).get('id') if kind=='drafts' else item.get('id')
                if not mid:
                    raise RuntimeError('Source inventory omitted an item identity')
                ids.add(mid)
            token = response.get('nextPageToken')
            if not token:
                return ids
            if token in seen:
                raise RuntimeError('Source inventory repeated a page token')
            seen.add(token)
    return pages(gm.src.users().messages(), 'messages'), pages(gm.src.users().drafts(), 'drafts')
