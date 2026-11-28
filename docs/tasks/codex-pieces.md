# Codex pieces — 2026-11-27

## Prevent the seeder from shadowing the guarded verifier

- **Files:** `seed_loader.py`, `reset_target.py`, `campaign_changes.py`, `separate_credentials.py`, `verify_scopes.py`, `tests/test_seed_loader.py`, `tests/test_reset_target.py`
- **Tests:** `.venv/bin/python -m pytest tests/test_seed_loader.py tests/test_reset_target.py tests/test_separate_credentials.py tests/test_verify_scopes.py tests/test_campaign_executor.py -q -rs`
- **Result:** Passed. Seeder imports now use one cached file-path loader that removes `data-generator/` from `sys.path` after module execution; scope and campaign callers no longer leave the shadowing directory behind.

# Codex pieces

## 2026-11-27 — Source write guard red team

- **Piece:** Added a fake-transport attack matrix over 831 discovery write-method/route combinations, transport and upload bypass probes, strict expected failures for current leaks, and the credential-backed `source_attack_probe.py` dry-run/live CLI.
- **Files:** `tests/test_source_write_attacks.py`, `source_attack_probe.py`, `docs/tasks/source-guard-redteam.md`, `docs/tasks/codex-pieces.md`.
- **Tests and result:**
  - `.venv/bin/python -m pytest -o addopts=--tb=short tests/test_source_write_attacks.py -q` — **281 passed, 567 xfailed** in 8.85s. The strict xfails record the currently observed leak routes, including the incomplete move update; the document lists each route and reason.
  - `.venv/bin/python -m py_compile source_attack_probe.py tests/test_source_write_attacks.py` — passed.
  - `.venv/bin/python source_attack_probe.py --help` — passed.
  - Invoked live mode with `dharan.gov.np` — refused with exit code 2 before credential construction, as required. The fake-transport dry-run was tested; no credential-backed dry-run, live writes, or network requests were run.

## 2026-11-27 — Align source attack regressions to the transport guard

- **Piece:** Removed all xfails from the 831 discovery cases; accepted the three read-only POST endpoints in the leak classifier; counted `MediaUpload` refusal at request construction; removed bare `auth._http()` because it carries no source credentials. Updated the route findings to reflect the merged guard.
- **Files:** `tests/test_source_write_attacks.py`, `source_attack_probe.py`, `docs/tasks/source-guard-redteam.md`, `docs/tasks/codex-pieces.md`.
- **Tests and result:**
  - `.venv/bin/python -m pytest --runxfail -o addopts=--tb=short tests/test_source_write_attacks.py -q` — **847 passed** in 5.40s.
  - `.venv/bin/python -m pytest -o addopts=--tb=short tests/test_source_write_attacks.py tests/test_source_transport_guard.py -q` — **878 passed** in 6.92s.
- No credential-backed dry-run or live probe was run; no network requests were made.


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
