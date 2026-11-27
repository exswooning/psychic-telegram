# Codex pieces

## F-reboot checkpoint and recorded-phase resume

- **Files:** `campaign.py`, `campaign_failures.py`, `campaign_reboot.py`, `api_server.py`, `main.py`, `run_phases.py` (existing recorder, now used by the scoped qualification), `tests/gate.txt`, `tests/test_campaign.py`, `tests/test_campaign_executor.py`, `tests/test_campaign_reboot.py`, `tests/test_run_phases.py`.
- **Tests:** `.venv/bin/python -m pytest tests/test_campaign_reboot.py tests/test_run_phases.py tests/test_campaign.py tests/test_campaign_executor.py -q`
- **Result:** Passed. F-reboot now opts its selected twin into the durable phase record, pins the active recorded Drive phase before reboot, and requires the same run record to finish with `resumedFrom=drive` after boot. Ordinary selected-user runs remain unrecorded by default.

## F-reboot refuses unrelated failpoint arms

- **Files:** `campaign_reboot.py`, `tests/test_campaign_reboot.py`.
- **Tests:** `.venv/bin/python -m pytest tests/test_campaign_reboot.py -q`
- **Result:** Passed. The reboot qualification now stops before touching a twin if the local failpoint arm file is unreadable or contains any fault; the reboot itself is the only injected failure.

## F-reboot VM qualification runbook

- **Files:** `docs/tasks/reboot-qualification.md`, `docs/tasks/codex-pieces.md`.
- **Tests:** Documentation-only; checked the commands, twin selection, request scope, checkpoint path, and PASS evidence against `campaign.py`, `campaign_failures.py`, and `campaign_reboot.py`.
- **Result:** Written for account 3, a. → b., the fresh campaign's t02 twin, no failpoint arm, the pinned one-time reboot, same-checkpoint continuation, recorded `drive` resume, and the full PASS criteria.
