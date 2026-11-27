# Codex worktree pieces

## Phase binding analyzer and telemetry

- **Piece:** Read-only, per-account phase analysis with measured limiter, CPU, line, and worker-slot inputs. Missing signals stay `UNKNOWN`; they do not produce a `NOTHING` verdict.
- **Files:** `phase_binding.py`, `metrics.py`, `main.py`, `db.py`, `verify_sample.py`, `tally.py`, `split_mail.py`, `dms_light_proof.py`, `data-generator/seed_sandbox.py`, `tests/test_phase_binding.py`.
- **Tests:** `.venv/bin/python -m pytest tests/test_phase_binding.py tests/test_sampler.py -q` — **13 passed**. `.venv/bin/python -m py_compile phase_binding.py sampler.py metrics.py main.py tally.py split_mail.py dms_light_proof.py verify_sample.py db.py data-generator/seed_sandbox.py tests/test_phase_binding.py tests/test_sampler.py` — **passed**.
- **Result:** Crafted ledger and metric/sampler series cover Google quota, line, CPU, NOTHING, and incomplete evidence. Worker occupancy now records busy user/task slots in migration, verification, tally, DMS reconciliation, and seeding pools. Seed metrics are persisted as bounded snapshots and retain a per-run summary.

## Sampler and live runbook

- **Piece:** Add the standalone Linux JSONL CPU/interface sampler and document how to capture/analyze one account run, the verdict thresholds, evidence limits, historical baseline rates, and the inline test metric sample.
- **Files:** `sampler.py`, `tests/test_sampler.py`, `data-generator/test_seed_sandbox.py`, `docs/tasks/phase-binding.md`, `docs/tasks/synthetic-10-plan.md`, `docs/tasks/codex-pieces.md`.
- **Tests:** `.venv/bin/python -m pytest tests/test_phase_binding.py tests/test_sampler.py -q` — **13 passed**; `.venv/bin/python -m pytest data-generator/test_seed_sandbox.py::test_seed_metrics_persist_the_sample_series_and_worker_slots -q` — **1 passed**. The existing regression selection across metrics, verifier, tally, DMS light proof, and split-mail tests also exited **0**.
- **Result:** Baseline rates are 373.9 Drive files/min, 102.9 DMS messages/min, and 830 DMS-check messages/min. Historical and inline fixture metrics lack enough evidence for a binding verdict, so the analyzer reports UNKNOWN.

## Limiter evidence attribution

- **Piece:** Keep cumulative limiter rejections phase-local; an unbased counter or empty event list cannot claim a quota result or a clean NOTHING verdict.
- **Files:** `phase_binding.py`, `tests/test_phase_binding.py`, `docs/tasks/phase-binding.md`, `docs/tasks/codex-pieces.md`.
- **Tests:** `.venv/bin/python -m pytest tests/test_phase_binding.py tests/test_sampler.py -q` — **13 passed**.
- **Result:** Exact rejection counts require a counter delta across a phase boundary. An observed limiter backoff remains positive quota evidence when its exact rejection count is unavailable. Missing baseline evidence stays UNKNOWN.
