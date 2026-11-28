"""Deterministic next steps from saved evidence. No model calls or tenant writes."""

def diagnose(service, result):
    findings = result.get('differences') or []
    diffs = [str(d) for item in findings if isinstance(item, dict) for d in item.get('diffs', [])]
    errors = [str(e) for e in result.get('errors', [])]
    out = []
    def add(code, summary, action):
        if not any(x['code'] == code for x in out):
            out.append({'code': code, 'summary': summary, 'nextAction': action})
    for d in diffs:
        text = d.lower()
        if 'header x-forwarded-encrypted:' in text:
            add('MAIL_STORAGE_HEADER', 'Old comparison rules flagged a Google storage header.',
                'Recheck these exact messages with the current comparer; retain body and label checks.')
        if 'labels ' in text or 'unread state differs' in text:
            add('MAIL_LABELS', 'Source and target labels or read state differ.',
                'Freshly compare content and mapping, then align permitted labels only when repairs are enabled; read back the result.')
        if 'native sheet' in text:
            add('SHEET_STRUCTURE', 'Entered Sheet data, formatting or native structures differ.',
                'Use bounded cell and structure comparisons. Preserve the target backup; repair only supported, unambiguous fields.')
        if 'form ' in text:
            add('FORM_ITEMS', 'Form items or settings differ.',
                'Align items before comparing. Report missing or ambiguous items explicitly and apply only the approved scoped exceptions.')
        if 'comment' in text:
            add('COMMENT_FIDELITY', 'Comment content, anchors or delivery differ.',
                'Reconcile existing target threads first; ambiguous delivery must not trigger another post.')
    missing = result.get('missing', [])
    if any(isinstance(x, dict) and x.get('type') == 'draft' for x in missing):
        add('MISSING_DRAFT', 'A mapped draft was absent from the target inventory.',
            'Read the exact draft ID on both sides, reconcile stale mappings, then use the guarded draft handler if repair is enabled.')
    if result.get('notCopied') and service == 'gmail':
        add('MAIL_MAPPING_GAP', 'Destination coverage is not established for source messages.',
            'Reconcile DMS deliveries by message identity and content before inserting anything.')
    if result.get('extras'):
        add('TARGET_ONLY_ITEMS', 'The target has items outside the recorded mapping.',
            'Reconcile DMS and known retained copies before classifying extras; do not delete them automatically.')
    if result.get('duplicates'):
        add('DUPLICATE_ITEMS', 'More than one target item may represent the source.',
            'Establish ownership and content equivalence before proposing any target cleanup.')
    for error in errors:
        text = error.lower()
        if 'budget' in text and ('allocated cells' in text or 'native sheet' in text):
            add('SHEET_READ_BUDGET', 'A Sheet exceeds the configured read budget.',
                'Measure its allocated grid and choose a bounded workload; no parity verdict until the required cells can be compared.')
        elif any(x in text for x in ('503', 'timed out', 'timeout', '429', 'ratelimit')):
            add('READ_RETRY_REQUIRED', 'A required read did not return usable evidence.',
                'Schedule a bounded affected-item recheck with quota pacing; keep the finding incomplete until a successful read.')
        elif '404' in text:
            add('READ_NOT_FOUND', 'A mapped resource or comment endpoint returned not found.',
                'Check the exact item type and source/target access; distinguish inaccessible data from genuinely missing data.')
        if 'labels did not match after repair' in text:
            add('MAIL_LABEL_READBACK', 'The label change did not pass readback.',
                'Compare fresh source labels and observed target labels; preserve the failure and avoid repeating the same repair indefinitely.')
    if result.get('itemScope'):
        add('ITEM_CHECK_ONLY', 'This evidence covers one item, not the mailbox.',
            'Retain the item finding separately and run an admitted mailbox check before publishing a user verdict.')
    return out
