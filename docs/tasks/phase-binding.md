# Phase binding measurement

`phase_binding.py` reports throughput and the evidence for what limits one
account's run. It reads the account ledger read-only, its recorded phase rows,
the bounded `run_metrics` snapshots, the persisted limiter ceilings, run logs,
the DMS console history, and an optional `sampler.py` JSONL series. It does not
read or change tenant data.

## Run it beside a live job

Run both commands from the Bitport checkout with that checkout's Python. Start
the sampler in a second terminal before starting the job, and leave it running
until the phase finishes. The default interface is `wlp2s0`; the sample file
may live anywhere writable and should be kept with the run's evidence.

```sh
.venv/bin/python sampler.py \
  --interface wlp2s0 --interval 5 \
  --output logs/phase-binding/5/run-12.jsonl
```

After the migration opens its phase record, get the run number from the
account ledger:

```sh
sqlite3 data/accounts/5/migration.db \
  'SELECT run, started_at FROM run_records ORDER BY run DESC LIMIT 5;'
```

Then run the analyzer while the work is still in progress, or as soon as it
finishes:

```sh
.venv/bin/python phase_binding.py \
  --account-id 5 --run 12 \
  --sample logs/phase-binding/5/run-12.jsonl \
  --line-bytes-per-sec 900000
```

The analyzer finds the account's migrate, DMS, verify, tally, and seed job logs
under `logs/jobs/5/`; it finds DMS console readings in
`logs/dms/5/metrics.history.jsonl`. Add `--log PATH` for a log outside those
locations. `--json` emits the same report as JSON.

Seeding is a separate run and has no `run_phases` row. Analyze its latest saved
seed summary with:

```sh
.venv/bin/python phase_binding.py \
  --account-id 5 --kind seed \
  --sample logs/phase-binding/5/seed.jsonl \
  --line-bytes-per-sec 900000
```

Use `--run-key seed:PID:STARTED_AT` to select one exact seed summary when the
ledger contains several. The run key is in the JSON report's `run.id`.

Some work has no timestamped phase row. If the log does not let the analyzer
split DMS import from its local reconciliation, or a verify/tally job was run
separately, give its UTC start and end explicitly:

```sh
.venv/bin/python phase_binding.py --account-id 5 --run 12 \
  --sample logs/phase-binding/5/run-12.jsonl \
  --phase-window dms_import=2026-11-27T02:00:00Z,2026-11-27T08:00:00Z \
  --phase-window dms_check=2026-11-27T08:00:00Z,2026-11-27T11:00:00Z
```

The analyzer accepts one window per phase. A later `--phase-window` for the
same phase replaces an inferred window.

## What each phase counts

| Phase | Work count used for items/min |
|---|---|
| Drive pass | Mapped files and folders created during the phase window |
| Mail pass | Mapped messages created during the phase window; in split mode, this is the engine's link-mail work |
| DMS import | Positive change in the DMS console's `Emails imported` counter |
| DMS check | The `checked` total from the DMS delivery reconciliation log summary |
| Verify | Sum of `checked` items in `user_verification` written during the phase |
| Tally | Users tallied during the phase |
| Seeding | Files, folders, emails, events, contacts, and tasks in the seed summary log |

The historical ledger can prune successful audit rows, so Drive and mail use
the durable `id_mapping.created_at` timestamps first. Tally is a per-user
count, and it is labelled as such rather than presented as an item count.
If a phase count is not available, the report shows `?` and does not infer it.

`run_metrics` keeps only the newest 240 samples (about one hour at the normal
15-second flush interval). Run this live for old or long phases; after the
samples roll off, a completed run may still have its phase and item counts but
not enough limiter evidence for a verdict. The DMS history and sampler JSONL
are append-only and can retain the longer run.

## How to read the verdict

- **Google quota**: the phase-local limiter rejection delta is positive, or a limiter backoff event records quota pushback.
- **the line**: sampled transmit rate is at least 80% of the configured uplink capacity.
- **the CPU**: normalized host CPU is at least 85% while at most 15% of the reported worker slots are idle.
- **NOTHING**: the phase has idle worker slots, a measured zero limiter-rejection delta, CPU below 85%, and transmit rate below 80% of a known uplink capacity. That is a Bitport throughput defect to investigate.
- **UNKNOWN**: evidence is missing or the measurements do not meet one of those conditions. Do not treat it as a pass.

The first matching condition wins in that order: quota, line, CPU, then
NOTHING. Exact rejection counts use the change in the adaptive limiter's
cumulative counter between snapshots bracketing the phase. A recorded limiter
backoff is positive quota evidence even when a counter baseline is missing;
the report leaves the exact count unknown in that case. Empty backoff events
do not prove zero rejections, so missing counter baselines produce UNKNOWN,
not NOTHING. The `rate_limiter_ceiling` table supplies the last saved ceiling
when the phase snapshots do not have one. A saved ceiling is not itself
evidence that Google pushed back during this phase.

CPU share is the host's aggregate busy percentage across all logical CPUs.
The sampler reports RX and TX separately and their combined bytes/sec. The
line verdict compares **TX** to `--line-bytes-per-sec`, because the known
0.9 MB/s limit is an upload limit. The Wi-Fi radio's negotiated link rate is
not the Internet uplink capacity; update the option if the measured uplink
has changed. The default is the plan's measured 900,000 bytes/sec baseline.

Worker share means the idle fraction of the phase's executor slots. Migration
and seed report concurrent user slots; verification and tally report their
user-level checking/counting slots; DMS delivery reconciliation reports its
message-check pool. Google's DMS import runs in Google's service, so it has no
local DMS worker pool to count. Where no pool activity is recorded, the report
leaves the share unknown. `metrics.py`'s `workers` value is live Python thread
count and is not substituted for busy/idle worker slots.

## Baselines in this checkout

Calling `phase_binding.historical_baselines()` against the P2.10 figures gives:

| Phase | Recorded work | Time | Rate | Binding verdict |
|---|---:|---:|---:|---|
| Drive pass | 38,513 files | 103 min | 373.9 files/min | UNKNOWN |
| Mail pass | Message count not recorded | 34 min | Unknown | UNKNOWN |
| DMS import | 141,977 messages | about 23 h | 102.9 messages/min | UNKNOWN |
| DMS check | 830 messages/min | 1 min equivalent | 830 messages/min | UNKNOWN |

Those baselines predate CPU, interface-byte, limiter-rejection, and worker
occupancy series, so assigning a bottleneck would be guesswork. The
`tests/fixtures` directory is absent in this checkout. The closest inline
metrics example is the seed heartbeat test in
`data-generator/test_seed_sandbox.py` (1,000 calls, 45 retries, 9.54 requests/sec); it has no phase
window, limiter state, CPU/network sample, or worker occupancy, so it also
remains UNKNOWN rather than supplying a binding verdict.

## Current scope

The analyzer and sampler are ready for live use. The old 45-user rates above
are the only phase baselines the repository contains. A comparable twin run
with the sampler active is still needed to complete P2.10's live measurement
and use any NOTHING verdict to drive a speed fix.
