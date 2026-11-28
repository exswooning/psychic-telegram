# F-reboot live qualification

This is the exact two-invocation qualification for Claude to run on the dedicated
fableplan2 VM after the reviewed branch is available there. It is account 3's
protected-source / sandbox-target pair:

```text
a.sarafgloabalexim.com  ->  b.sarafgloabalexim.com
```

The qualification is not a failpoint run. **Arm nothing:** do not set
`BITPORT_FAULT`, do not write `logs/failpoint`, and do not run another failure
scenario. `campaign_reboot` refuses to proceed if any failpoint is armed. The
test itself requests one real guest reboot after it proves the exact migration
process is still active and has completed its Drive pass.

## Before the first invocation

1. On the VM's local console, confirm it is a virtual guest and read its machine
   ID. Keep that ID in the shell only; do not put it in this document or a log.

   ```sh
   systemd-detect-virt --vm --quiet
   MACHINE_ID=$(cat /etc/machine-id)
   test -n "$MACHINE_ID"
   ```

2. Run from the deployed Bitport checkout on that VM. The account 3 settings,
   source protection, and explicit sandbox declaration must still identify the
   pair above. The code verifies account 3, the pinned VM ID, a supported twin
   and its exact source-to-target identity mapping before acting.
3. Wait until all accounts are idle. There must be no active or queued tenant
   jobs, no unfinished repair records, and no existing campaign checkpoint with
   the chosen campaign name. The runner checks these conditions across the
   configured account ledgers before it resets a twin.
4. Use one new campaign name and keep it unchanged after the reboot. No other
   operator action should start tenant work during the qualification.

## First invocation

From `/opt/bitport` on the VM, run:

```sh
cd /opt/bitport
.venv/bin/python campaign.py \
  --account-id 3 \
  --name f-reboot-20261127 \
  --only fail:F-reboot \
  --allow reboot \
  --reboot-guest "$MACHINE_ID"
```

The runner reserves the first unused migration twin from `t02` through `t20`,
records its source/target mapping and a durable checkpoint, then starts a full
fidelity migration of that one user on Drive, Gmail, Calendar, Contacts, and
Tasks. It waits for a Drive `PASS` from that migration's exact PID and archived
transcript. It then runs `systemctl reboot` on the VM. No other host is rebooted.

The first invocation is expected to stop at the reboot request and print an
`UNVERIFIED` message explaining that the checkpoint must be continued after
boot. That first status is not the qualification result. Do not rerun the first
invocation, delete the checkpoint, or manually start a migration. If the Drive
pass is not proven within the runner's one-hour wait, it holds without rebooting.

## After the VM returns

Confirm that the same machine ID is present and that the boot ID and boot time
have changed. From the same checkout, use the same name, account, scope, and
machine ID, adding only `--continue-reboot`:

```sh
cd /opt/bitport
.venv/bin/python campaign.py \
  --account-id 3 \
  --name f-reboot-20261127 \
  --only fail:F-reboot \
  --allow reboot \
  --reboot-guest "$MACHINE_ID" \
  --continue-reboot
```

The runner verifies a run-bound interrupted exit for the original PID, finds
exactly one audited post-boot Start for the same twin (autopilot or the
operator's Start), waits for its terminal result and follow-ons, and then runs a
read-only One-to-one check with repair disabled. If the Start receipt or exit
evidence is ambiguous, it holds and must not be restarted with a fresh name to
try again; preserve the checkpoint for review. It also refuses a second reboot.

## PASS bar

The final campaign row must be `PASS` for `fail:F-reboot`, with `rebootEvidence`
showing the original PID's interrupted exit, a unique resumed PID and action
receipt, the same twin, and `resumedThrough` equal to `autopilot` or `operator
Start`. The read-only full verification must complete, the twin must match the
control's service counts, and the judge must find no duplicate landing. A
missing or incomplete verification, unclear interruption, duplicate item,
different counts, wrong guest, wrong domain pair, or ambiguous Start is not a
PASS; retain the checkpoint and report the reason.

The offline unit qualification passed:

```text
.venv/bin/python -m pytest tests/test_campaign_reboot.py tests/test_campaign.py tests/test_campaign_executor.py -q
PASS (no failures)
```

No VM, box, tenant, or external service was contacted for that test run.
