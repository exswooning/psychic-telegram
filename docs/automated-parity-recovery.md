# Automated parity diagnosis and guarded recovery

This implementation extends the existing migration engine and admitted per-user
repair workflow. It does not enable the paused repair policy or start a tenant
job. Chat stays outside account 3's current scope.

## Evidence flow

- Inline Gmail checks store item-level content/label evidence and deterministic
  diagnostic codes in migration_item_receipts. An inline failure queues only its
  own findings. It never replaces the saved mailbox result or invents a mailbox
  size. Item-scoped evidence cannot close unrelated findings.
- NEEDS_RECHECK receipts keep the user incomplete in both API and database
  rollups. A current, unsampled, error-free Gmail check with complete source
  coverage may supersede older blockers for unchanged destination mappings.
  Receipts newer than that check's start remain pending. Superseded receipts are
  not re-labelled VERIFIED: their old byte evidence remains historical.
- Failed service attempts remain expected verification services, even when they
  never entered services_done. Historical identical rows with unexplained extras
  are corrected when read, without editing their original saved evidence.
- Full-coverage indicators require declared per-user scope, comparison provenance,
  complete feature checks and no sampling or pending item receipts. Saved
  verdicts remain distinct from proof of tenant-wide full fidelity.

## Automatic diagnosis

parity_diagnostics.py produces stable codes and concrete next steps for storage
headers, label differences, mapping gaps, drafts, duplicates, target-only items,
Sheets, Forms, comments, read budgets and transient/not-found reads. These
summaries are saved with service results and displayed in the expanded user
findings. No model decides a verdict, closes a case or executes a repair.

Existing typed repair handlers still own writes: reconcile DMS before mail copy;
inspect draft IDs before recopy; compare and back up native structures; reconcile
comment delivery before posting. Unsupported/ambiguous cases remain findings.
The prior commits contain the accepted storage-header fix, item-aligned Forms
comparison and bounded Sheet field/cell diagnostics. None of this broadens the
operator's approved Form exceptions or authorizes deleting target extras.

## Label repair safeguards

The label handler freshly reads source and target message content, applies the
configured link rewrite to the expected content, and checks the exact mapping
again before changing labels. Changed content holds the repair. Readback errors
name remaining missing/extra labels rather than claiming a successful repair.

A durable per-item fingerprint permits one label write attempt for unchanged
normalised content, destination identity, desired/current labels and rule version.
Another invocation or process restart holds the same attempt. New evidence can
be investigated separately. Storage-header changes do not reset this fingerprint.
The existing account admission and repair policy remain mandatory. This does not
cancel a target request already in flight or provide atomic Google-side locking.

## Qualification and rollout

Tests use isolated fake Google transports and temporary ledgers. They cover
preserving prior mailbox summaries, pending findings, receipt timing, guarded
label changes, repeated attempts, historical extras and service scope gaps.
The UI tests cover visible diagnosis/next steps. These establish code behaviour,
not live parity, Google category-label persistence or a completed repair.

Deployment requires globally idle migrations, DMS, verification and repairs,
reviewed pushed source, actual installed-file baseline checks, rollback, unchanged
sync_vps.sh gates and checks of both UIs. Active jobs remain a rollout hold.
Do not deploy the older queued artifacts over this source or unexpected drift.
