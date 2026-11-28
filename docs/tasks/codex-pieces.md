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

