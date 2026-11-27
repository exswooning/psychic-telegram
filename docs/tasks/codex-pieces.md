# Codex work pieces for review

## Piece 1 — changed-after target cases (3/9)

- Implemented `gmail.labels_missing`, `drive.mtime` and `drive.missing_on_target`: each has an explicit baseline migration act, a guarded target-side change, and a verification/repair or gate judgment. The runner now performs baseline → change → verify in that order and checks listed outcomes as well as automatic fixes.
- Files: `campaign.py`, `campaign_changes.py`, `campaign_scenarios.py`, `tests/test_campaign.py`, `tests/test_campaign_error_acts.py`, `docs/tasks/error-acts.md`.
- Tests: `.venv/bin/python -m pytest tests/test_campaign_error_acts.py tests/test_campaign.py -q` — **passed, 78 tests**.
- Tenant/live status: not run; offline fake-driver evidence only.

## Piece 3 — source-side changes (3/9)

- Implemented `gmail.unmapped_source`, `drive.content_changed` and `contacts.unmapped_source`. Each now has an explicit baseline migration, a domain-guarded write to a., and its verify/repair, delta, or listed-next-step judgment. All nine changed-after cases now have acts.
- Files: `campaign_changes.py`, `campaign_scenarios.py`, `tests/test_campaign_error_acts.py`, `docs/tasks/error-acts.md`.
- Tests: `.venv/bin/python -m pytest tests/test_campaign_error_acts.py tests/test_campaign.py -q` — **passed, 78 tests**.
- Tenant/live status: not run; offline fake-driver evidence only.

## Piece 2 — remaining target-side changes (3/9)

- Implemented `drive.grant_missing`, `contacts.missing_on_target` and `tasks.list_missing` with an explicit baseline migration, guarded target-side mutation, and the verify/repair or listed-next-step judge.
- Files: `campaign_changes.py`, `campaign_scenarios.py`, `tests/test_campaign_error_acts.py`, `docs/tasks/error-acts.md`.
- Tests: `.venv/bin/python -m pytest tests/test_campaign_error_acts.py tests/test_campaign.py -q` — **passed, 78 tests**.
- Tenant/live status: not run; offline fake-driver evidence only.
