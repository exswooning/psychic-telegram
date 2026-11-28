# Gmail speed benchmark for `3076e0b`

This qualifies the target metadata batching in `verify_sample.py`. The bounded
repair queue has an offline ordering test and is not exercised by this
read-only live benchmark. Do not merge `3076e0b` as part of this handoff. Run
this only on Bitport account 5,
the `a.sarafgloabalexim.com` → `b.sarafgloabalexim.com` sandbox. Account 3 is
historical evidence; leave its ledger and tenants alone.

## Users and preconditions

- `p002@a.sarafgloabalexim.com`: the largest mailbox in the five-user
  `org45` profile plan at seed 0 (40,191 messages before any configured
  mail-share factor). The live count can be lower if the seed used the planned
  15% mail factor.
- `c10@a.sarafgloabalexim.com`: the cohort's 5,000-message paging workload.

Before timing, confirm both source users still exist, their target identities
are mapped in account 5, and the exact live message counts from the report's
`sourceInventory` are recorded. Stop if either account is absent or maps outside
the a. → b. sandbox. The metadata batching path is only exercised when at least
100 target-only/unmapped messages are present across these two mailboxes; if
there are fewer, report the inventory result and mark that optimization
**NOT EXERCISED**. Do not manufacture mail or remove mappings for this timing
run.

## Baseline and candidate commands

Run the baseline on the branch immediately before `3076e0b`, then run the
candidate after testing that commit separately. Keep the mailbox, account,
environment, and command identical. These settings disable the history cache
and migration receipts, so both runs perform the full content comparison and
do not get a faster second pass by reusing the first pass's evidence. They do
not disable checks or accept a metadata-only verdict.

```bash
cd /opt/bitport
export VERIFY_GMAIL_BATCH_SIZE=10
export VERIFY_GMAIL_WINDOW=8
export VERIFY_GMAIL_PARALLEL_SIDES=1
export VERIFICATION_CACHE=0
export VERIFY_USE_RECEIPTS=0
.venv/bin/python verify_sample.py --account-id 5 \
  --user p002@a.sarafgloabalexim.com \
  --user c10@a.sarafgloabalexim.com \
  --services gmail
```

For each version, record start/end UTC, elapsed seconds, the exact source
inventory and checked counts, full Gmail completion/verdict, and counts of
differences, missing items, duplicates, target extras, not-copied items, and
errors from `logs/verify/5/latest.json`. Keep any known target-only fixture
IDs the same across versions. Do not report a timing comparison if either run
overlaps a migration, DMS pass, or another Gmail check.

## Pass bar

- Both reports say Gmail `COMPLETE`; the source inventory counts and checked
  mapped-message counts agree exactly between versions.
- There are zero check errors, missing mapped messages, or unexplained
  differences. Any pre-existing target extras must have the same IDs and
  classifications in both reports. An incomplete check is never a speed pass.
- With at least 100 target-only messages, the candidate's Gmail wall time is
  at least 20% lower than the baseline. If it is not, record the result as
  **speed-up not demonstrated**; do not weaken the completeness or fidelity
  criteria to make it pass. If fewer than 100 target-only messages exist, the
  batching result is **NOT EXERCISED**, regardless of elapsed time.

The benchmark is read-only on both tenants. Do not pass `--repair-after-user`.
The worker-feed change in `repair.py` has an offline ordering test
(`test_a_slow_message_does_not_block_repair_of_the_next_group`); a live repair
timing would write to b. and requires a separate approved recovery workload
with eligible source findings. If such findings occur during an authorized
sandbox run, record its attempted/mapped counts and errors separately; do not
mix that measurement into this read-only comparison.
