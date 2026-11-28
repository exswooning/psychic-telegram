# F-reboot live qualification

This run qualifies the reboot recovery path for FablePlan2 on the dedicated l3novo Bitport VM. It is a two-invocation test: the first invocation starts one selected-user migration and requests one reboot after its recorded Drive phase is running; Claude reruns the same campaign after boot to verify the recovery.

## Scope and twin

- Bitport account: **3**.
- Source: `a.sarafgloabalexim.com`.
- Target: `b.sarafgloabalexim.com`.
- Control: `t01`.
- Test twin: **`t02@a.sarafgloabalexim.com` → `t02@b.sarafgloabalexim.com`**.
- Use the fresh campaign name `fp2-vm-freboot-20261127`. With a new campaign directory its twin list is empty, so `LiveDriver` selects the first available twin, t02. It records that choice in `logs/campaigns/fp2-vm-freboot-20261127/twins.json` and the durable checkpoint in `F-reboot.json`.

Do not remove or edit an existing campaign directory or `F-reboot.json`. If this name already has state, keep it and inspect the state before choosing a new unique campaign name. The runner independently pins account 3, the tenant pair, the selected user's target mapping, and the ledger identity. It resets only the selected twin's target data and migration ledger before the initial Start; it does not wipe either tenant.

## Preconditions

1. Run from the VM's local console as root in `/opt/bitport`, after the unattended failure queue has finished. Do not run during another migration, queued job, repair, or campaign. The runner checks account activity and repair ledgers and refuses if anything is active.
2. Confirm account 3 is still configured from a. to b., a. remains protected as a source, and b. is declared as the permitted sandbox. The runner checks these safeguards before touching the twin.
3. Use the reviewed build containing `campaign_reboot.py`, scoped phase recording, and its tests. Do not start a separate deploy or restart for this qualification.
4. **Arm no failpoint.** Do not set `BITPORT_FAULT` or write a fault to `logs/failpoint`. F-reboot is caused by the host reboot itself. The runner checks that the failpoint arm file is empty and stops before resetting a twin if any other fault is armed. `--allow reboot` is the explicit permission for the one `systemctl reboot` request.
5. Confirm the name below is fresh so its first twin is t02. Leave the existing `t01` control untouched.

## First invocation: start, record Drive, reboot once

At the VM console:

```bash
cd /opt/bitport
.venv/bin/python campaign.py \
  --account-id 3 \
  --name fp2-vm-freboot-20261127 \
  --group failures \
  --only fail:F-reboot \
  --allow reboot \
  --reboot-guest "$(cat /etc/machine-id)"
```

The campaign uses the normal audited Start route with `users=[t02@a.sarafgloabalexim.com]`, services `drive,gmail,calendar,contacts,tasks`, full fidelity enabled, engine mail for this selected user, server-side Drive copy, and `record_phases=true`. It first resets the selected twin on b. and clears that twin's migration ledger. It then verifies the exact Start receipt, process, transcript, and live `drive` phase in the account's `run_records`/`run_phases` record. Only while that exact Drive phase is RUNNING does it persist the checkpoint and call `systemctl reboot`.

The VM going offline is expected. The first invocation is not the final verdict; its checkpoint records the reboot request, original PID and admission time, exact action receipt, twin mapping, ledger identity, and phase-record cursor. Do not run another campaign or request a second reboot.

## After boot: continue the same checkpoint

From the VM's local console, use the same account, name, scope, and guest identity, adding `--continue-reboot`:

```bash
cd /opt/bitport
.venv/bin/python campaign.py \
  --account-id 3 \
  --name fp2-vm-freboot-20261127 \
  --group failures \
  --only fail:F-reboot \
  --allow reboot \
  --reboot-guest "$(cat /etc/machine-id)" \
  --continue-reboot
```

The runner confirms this is the pinned VM's new boot, proves the original process was interrupted by that reboot (and not by a clean exit or audited Stop), and reads any automatic autopilot Start. If autopilot has not resumed it, the runner sends one audited Start for the same twin and exact scope. It never repeats an ambiguous Start. The migration opens the same phase record; because the reboot was injected during Drive, `run_phases.begin()` sets `resumedFrom=drive` and reruns that first not-DONE phase. The ledger may skip a recorded DONE phase only when the selected user's ledger proves that user no longer needs it. The later Gmail and calendar/contacts/tasks phases then complete in order.

After the migration and follow-ons are idle, the runner performs a read-only one-to-one verification for the twin (including Directory), with repair disabled, and compares it to control t01.

## PASS criteria

The result is **PASS** only when all of these are evidenced:

- The pre-boot migration was the exact account-3 Start for t02, and its recorded Drive phase was active at the time of the reboot.
- The same durable run record is reused after boot; its resume count increases by one, it names `resumedFrom=drive`, and `drive`, `after_drive`, `gmail`, and `calendar+contacts+tasks` all finish `DONE` under the resumed PID.
- The original migration has run-bound interruption evidence associated with the reboot; the post-boot Start has one exact audit receipt and a unique clean terminal result (exit code 0).
- The read-only verification and campaign evidence show t02 equal to control t01 across the expected service checks and counts, with **zero duplicate items**.

Any measured parity difference or duplicate is **FAIL**. Missing, conflicting, or unreadable evidence is **UNVERIFIED** and does not count as a pass. The final `PASS fail:F-reboot` line and result row are written to `logs/campaigns/fp2-vm-freboot-20261127/RESULTS.md` and `results.jsonl`; keep the `F-reboot.json` checkpoint with them.
