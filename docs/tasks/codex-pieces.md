# Codex pieces — 2026-11-27

## Prevent the seeder from shadowing the guarded verifier

- **Files:** `seed_loader.py`, `reset_target.py`, `campaign_changes.py`, `separate_credentials.py`, `verify_scopes.py`, `tests/test_seed_loader.py`, `tests/test_reset_target.py`
- **Tests:** `.venv/bin/python -m pytest tests/test_seed_loader.py tests/test_reset_target.py tests/test_separate_credentials.py tests/test_verify_scopes.py tests/test_campaign_executor.py -q -rs`
- **Result:** Passed. Seeder imports now use one cached file-path loader that removes `data-generator/` from `sys.path` after module execution; scope and campaign callers no longer leave the shadowing directory behind.
