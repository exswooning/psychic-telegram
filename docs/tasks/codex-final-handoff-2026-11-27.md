# Codex handoff — 27 Nov 2026

This handoff records the pieces I handled in this worktree and the test state
before the operator split B, C, and S into separate sessions. No VM or laptop
box was contacted, and no network, deploy, push, stage, or commit operation was
performed by Codex. The sandbox keeps `.git` read-only; Claude is responsible
for committing the finished work.

## Worktree and branch

- Branch: `codex/last-day-2026-11-27`.
- Latest branch commit observed: `79fa6d0`, committed by Claude for A.
- Codex's V, B, and D edits remain uncommitted. `dc44e8f` and `db48518` were
  integrated by applying their source changes manually; no Git metadata was
  changed. `3076e0b` remains unmerged.
- `docs/tasks/codex-pieces.md` records A, V, B, and D. V measurements and the
  reboot qualification steps are in their linked task documents.

## Findings not otherwise obvious from the task documents

- V's Sheets estimator samples response cells and retains a safety margin; a
  provider response-limit error halves the requested window and retries. This
  avoids serializing every cell just to estimate JSON size. The 10M-cell fake
  still completed the comparison and found a planted mismatch.
- Native Docs/Slides image fetches share one bounded pool across both tenants
  (maximum concurrency eight). Binary verification streams one-MiB chunks to
  MD5/SHA-256/size digests; `VERIFY_TRUST_MD5=0` now applies to files above
  25 MiB too. Fake timings do not predict Google's API latency.
- The live Gmail benchmark is read-only and uses account 5's a. → b. sandbox,
  with `p002` and `c10`. It requires at least 100 target-only messages to
  exercise the new inventory batching path. If that condition is absent, the
  result is explicitly NOT EXERCISED. The repair worker-feed improvement has
  an offline ordering test; a live repair timing needs a separate eligible
  sandbox workload because it writes copies on b.
- The initial broad suite attempt made clear that this laptop sandbox denies
  loopback binds and launching `ps`. Focused tests were made hermetic by
  exercising the stdlib web handler in memory, using a fake process table in
  status/deploy fixtures, and using `Popen.poll()` for the reattachment waiter
  test. `resources.py` now falls back to `sysconf`/`os.cpu_count()` when macOS
  `sysctl` is unavailable. These adjustments are in the shared working tree
  for the C session to review.

## Test results

- **A:** Claude reports the touched engine/campaign test files passed; see the
  A entry in `docs/tasks/codex-pieces.md`.
- **V focused suite:**
  `.venv/bin/python -m pytest tests/test_sheet_window_growth.py tests/test_native_images_compared.py tests/test_image_fetch_deadline.py tests/test_verify_streamed_binary.py tests/test_verify_sample.py tests/test_verify_unverified.py tests/test_verify_content_memory.py benchmarks/verify_at_scale_test.py -q`
  exited 0; 177 passed, no failures. The standalone five-workload scale
  harness also passed. See `docs/tasks/verify-at-scale.md` for measurements.
- **B reboot tests:**
  `.venv/bin/python -m pytest tests/test_campaign_reboot.py tests/test_campaign.py tests/test_campaign_executor.py -q`
  passed with no failures. `py_compile` for `campaign_reboot.py` passed. No
  live qualification was run.
- **C focused regression set:** after the sandbox fixes,
  `tests/test_deploy_refuses_while_busy.py`,
  `tests/test_follow_ons_survive_an_api_restart.py`, `tests/test_resources.py`,
  `tests/test_job_admission.py`, `tests/test_job_slot_reaping.py`,
  `tests/test_webui_requires_auth.py`, `tests/test_migrations_per_login.py`,
  and the bounded Sheet reconciliation test passed together. This is not a
  substitute for the full gate.
- **C full suite:** an earlier `.venv/bin/python -m pytest tests/ -q -rs`
  attempt before the focused fixes had 26 failed/error nodes and 7 skips. The
  failures included sandbox-denied `ps`/loopback calls, the unavailable macOS
  `sysctl` RAM figure, an outdated Sheet call-count assertion, and the V ruff
  unused-variable finding. The seven skips were the explicit macOS
  OS-isolation qualifications in `test_candidate_canary_api.py`,
  `test_candidate_qualification.py`, and `test_worker_qualification.py`.
  After the focused fixes, the exact full command was started again and the
  operator redirected the work at about 18%; it has no final counts and must
  be rerun by the C session. `data-generator/` and the frontend gates were not
  run by this session.
- **D:** `.venv/bin/python -m pytest tests/test_benchmark_run.py -q` passed
  (17 passed, no failures). Ruff passed for `benchmark_run.py` and
  `tests/test_benchmark_run.py`.
- **S:** not performed by this session; it belongs to the separate S session.

## Commits

- `79fa6d0` — Claude's A commit, latest branch commit observed.
- No Codex-authored commit was created in this worktree. The `db48518` and
  `dc44e8f` source diffs were integrated manually because Git writes are
  forbidden here; their source commits remain on their original refs. The
  `3076e0b` Gmail speed-up was not merged.

## Handoff cautions

- Do not infer a green full-suite gate from the focused results. The C session
  needs to run the exact full commands and report every skip reason, then run
  the frontend commands.
- The VM reboot qualification document is for Claude to run on the sandbox
  VM under its existing authority. No reboot or live qualification occurred
  here.
- Keep account 3's historical ledger untouched. The Gmail benchmark document
  uses account 5 only and never repairs or writes to either tenant.
