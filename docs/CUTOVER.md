# Cutover: the moment a migration is declared at parity

The source stays live during a migration, so "at parity" is only true at a defined moment. This is that
moment, step by step. Every command is a step Bitport already has; the order is what makes the verdict mean
something. Account 3 is used as the example; use the account's own id.

## Before the window (days ahead)

1. The bulk run and the DMS have finished, and the parity gate's only failures are kinds Bitport repairs on
   its own, or none: `python parity_gate.py --account-id 3` (or the Parity gate card on Migration detail).
2. Run a delta every night until the cutover, so the final one is small:
   `python main.py --account-id 3 delta --services drive,calendar,contacts,tasks --days 2`.
   Mail is never in it: bulk mail moves only through the DMS (the operator's rule), so a plain `delta`
   (all services) is the wrong command here.
3. Tell the users when the freeze starts and that mail keeps arriving at the old address until the MX change
   is live. Bitport never notifies anyone itself.

## The window

| Step | What | Command | Done when |
|---|---|---|---|
| 1 | Freeze: users stop editing Drive files on the source (mail cannot be frozen and does not need to be) | announced by the operator | the announced time |
| 2 | Final Drive and calendar/contacts/tasks delta | `python main.py --account-id 3 delta --services drive,calendar,contacts,tasks --days 2` | the job exits 0 |
| 3 | Switch MX to the target (the customer's DNS) | at the registrar | `dig MX <domain>` shows the target |
| 4 | Final mail: a DMS delta picks up mail that reached the source until MX switched | start the DMS from the migration page (it runs as a delta on an existing map) | the DMS console reads 0 failed, or a remainder a delta no longer lowers |
| 5 | Verify with repair (the follow-on of the DMS) | starts on its own | the job exits 0 |
| 6 | The parity gate (the follow-on of the verify) | starts on its own; `python parity_gate.py --account-id 3` to read it again | **PARITY** |

The verdict's `asOf` (in `logs/reports/3/parity-gate-latest.json`) is the cutover moment. Any check older
than a later change to the same service reads STALE, so a verdict cannot outlive the state it describes.

## After

- Mail with a Drive link that arrived during the window reaches the target through the DMS as it was sent
  (its links name the source). The mail dead-link report lists such links; a link-mail pass can rewrite them.

- Mail that reached the source after step 4 (late MX propagation) is picked up by one more DMS delta; run
  the gate again after it.
- Approval of the migration is an operator's click on Final Report, never a timer. Teardown follows from it
  (dry run first).

## What does not reach the target, and says so

The gate lists it under "Not migrated, with the reason": Google's own import refusals (the DMS console's
failed count), files no API can copy (Sites, My Maps, Jamboards), grants to deleted accounts, encrypted
mail sent through the DMS as it is. Each is named; nothing is silently left.
