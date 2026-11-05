# Migration error-to-fix workflow

Open **All tools → Error resolution**, also linked from **Failures**.
Start migrations normally. The run watcher reads new log bytes on its existing
30-second tick. It records structured progress/metrics and legacy error lines,
groups recurring errors, and preserves affected scope and workflow history.
The migration engine's existing retry and repair policies remain authoritative;
this feature never silently re-enables a paused policy or writes to a source.

## What runs automatically

- Bounded log ingestion with persistent offsets, partial-line handling, rotation
  detection and continued draining after a job finishes.
- Error classification: quota, transient API/network, authentication, permissions,
  missing source, unsupported capability, integrity finding, code or unknown.
- One case per account/service/error family, with user/item scope and repeated
  occurrence counts. Heartbeat echoes do not count as fresh failures.
- Known temporary failures remain with the engine's bounded recovery. Existing
  verification repair queues/handlers continue under their own admission/policy.
- Code and unknown errors with a recorded release version enter a coding queue.
  Historical logs without a trustworthy version are collected but not sent to AI.
- Fresh, complete verification for every affected user/service can close a case.
  Sampling, empty unknown scope, process exit 0, count parity, test success and
  deployment alone never close one. Owner-required and unsupported findings stay open.

Detailed telemetry is bounded to the newest 20,000 events per account. Case,
affected-item and attempt records remain durable. Metrics absent from a transcript
remain unknown: the collector does not invent byte totals, timings or quota units.

## Connect a coding worker

In Error resolution, an authorised administrator selects **Connect coding worker**,
enters an audit reason and downloads a one-time configuration. This is a revocable
scoped token, not a Bitport admin session or a Google credential. It can only claim
redacted cases and report its own leased attempts. Account-scoped workers cannot
read another account; only a superadmin can connect an all-account worker.

On the **source laptop checkout**, keep the config outside the repository:

```
chmod 600 /private/path/bitport-coding-runner.json
python3 incident_worker.py --config /private/path/bitport-coding-runner.json \
  --repo /path/to/bitport --watch
```

Codex CLI must be installed and authenticated on that machine. A dedicated worker
OS account is recommended: the source worktree contains no copied Google keys or
runtime databases, but normal Codex workspace sandboxing is not a replacement for
separating the host account's own credentials. The Bitport transport token is never
passed into agent or test environments. HTTP is permitted only over private,
loopback or Tailscale addresses; redirects are refused to avoid token leakage.

The worker runs at lower CPU priority, claims no work while migrations,
verifications, queued jobs or repairs are active, and yields active investigation
when higher-priority work appears. There is one coding attempt globally at a time,
three investigations/day by default, at most two attempts/case, bounded process
time/output, and expiring leases. These are attempt limits, not a dollar guarantee.
Policy and revocation are available in the UI. No model runs for each routine log line.

## Fix and release gates

1. The coding agent edits an isolated local worktree, with redacted logs explicitly
   marked as untrusted data. It is instructed to reproduce, add a regression and fix
   the cause. It does not receive authority to deploy or access tenant data.
2. The trusted worker rejects deleted/sensitive/oversized changes, modifications of
   existing tests, and protected auth/source-guard/deployment/worker modules. Such
   changes require separate review. Automatic candidates require both a source fix
   and a new regression test.
3. New regressions must fail on the original source with pytest exit 1. The patched
   suite must then pass under OS isolation, with syntax checks. On macOS the built-in
   Seatbelt launcher allows source/venv/temp reads and temporary writes, with no
   network or tenant credentials. Unsupported OS isolation leaves the attempt held.
4. The tested candidate is committed locally and pushed to a separate
   `bitport/incident-<case>-<attempt>` branch. The primary source branch is unchanged.
5. **Qualification and deployment are held by default.** Optional trusted local
   config entries `qualifyCommand` and `deployCommand` are argv arrays, not shell
   strings. Placeholders `{worktree}`, `{commit}`, `{case_id}` come only from the
   worker, never from the incident. They must point to reviewed external wrappers.
   Qualification must exercise explicitly authorised sandbox tenants in isolation.
   The deployment wrapper must verify exact candidate hashes, retain rollback,
   preserve UI/credentials/data, use the unchanged normal sync_vps.sh gates with no
   overrides, verify release/service health, and stamp the full 40-character
   DEPLOYED_COMMIT only after matching the installed artifact hashes. Neither an agent's claim nor unit
   test success is qualification evidence.
6. The server checks that the candidate matches its installed release before
   accepting DEPLOYED. It schedules a full read-only affected-scope verification
   when globally idle. Cases with broad/unknown scope require scope selection in
   Verification. Nonmatching rechecks stay open. Worker disconnects/failing tests
   do not trigger endless retries or automatically resolve incidents.

This first implementation uses Codex. Existing copyable incident briefs remain
usable by Claude; an unattended Claude execution adapter is not included.

## Limits

Permission/consent, missing owner access and unsupported platform features cannot
be made equivalent merely by changing code. They remain explicit blockers.
Redaction removes common secrets and pseudonymises export identifiers; it is not
a general proof that arbitrary application log text contains no personal data.
Avoid logging message bodies or secrets at the source. Original detailed logs are
available only through their existing authenticated UI.
