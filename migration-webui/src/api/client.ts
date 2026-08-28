/**
 * client.ts
 * =========
 * The one place this app talks to the backend. Before this file existed,
 * nothing in `src/` called `fetch` at all -- every page read
 * `useMigrationStore`'s hardcoded seed data, and `useMigration.ts` mutated it
 * with `Math.random()` on a timer to look alive.
 *
 * Every endpoint here is served by webui.py / webui_spa.py, which read
 * migration.db (read-only) and process-local state (metrics.py,
 * resources.py) -- never a live call to Drive/Gmail/Calendar on a poll path.
 * See webui_spa.py's module docstring for why that boundary matters.
 */

import {
  User,
  MigrationStage,
  SystemMetrics,
  ActivityEvent,
  VerificationResult,
  FinalReport,
} from '@/types'

async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(path)
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`)
  return res.json() as Promise<T>
}

export async function fetchUsers(): Promise<User[]> {
  const data = await getJSON<{ error: string; users: User[] }>('/api/spa/users')
  if (data.error) throw new Error(data.error)
  return data.users
}

export async function fetchStages(): Promise<MigrationStage[]> {
  const data = await getJSON<{ error: string; stages: MigrationStage[] }>(
    '/api/spa/stages'
  )
  if (data.error) throw new Error(data.error)
  return data.stages
}

export async function fetchActivity(): Promise<ActivityEvent[]> {
  const data = await getJSON<{ error: string; activity: ActivityEvent[] }>(
    '/api/spa/activity'
  )
  if (data.error) throw new Error(data.error)
  return data.activity
}

export async function fetchMetrics(): Promise<SystemMetrics> {
  // No error envelope: metrics_payload() never touches the ledger in a way
  // that can be "not there yet" -- resources.py and metrics.py always answer.
  return getJSON<SystemMetrics>('/api/spa/metrics')
}

export async function fetchVerification(): Promise<VerificationResult[]> {
  const data = await getJSON<{ error: string; verification: VerificationResult[] }>(
    '/api/spa/verification'
  )
  if (data.error) throw new Error(data.error)
  return data.verification
}

export async function fetchReport(): Promise<FinalReport | null> {
  const data = await getJSON<{ error: string; report: FinalReport | null }>(
    '/api/spa/report'
  )
  if (data.error) throw new Error(data.error)
  return data.report
}

// -- actions: the same whitelist-only surface webui.py's own inline JS uses.
// The server maps a name to a fixed argv list; nothing typed here is ever
// concatenated into a shell string. See webui.py's module docstring.
export interface ActionSpec {
  label: string
  blurb: string
  destructive: boolean
  confirm: string
}

export async function fetchActions(): Promise<Record<string, ActionSpec>> {
  return getJSON<Record<string, ActionSpec>>('/api/actions')
}

export async function runAction(
  name: string,
  confirm?: string
): Promise<{ ok: boolean; error: string | null }> {
  const res = await fetch('/api/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: name, confirm }),
  })
  return res.json()
}

export async function stopJob(
  accountId?: string, force = false
): Promise<{ ok: boolean; msg: string }> {
  // force is SIGKILL, for a child that took the interrupt and hung anyway
  // -- see Job.stop. Never the first thing to try: the cooperative SIGINT
  // commits state so a re-run resumes cleanly.
  const res = await fetch('/api/stop', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ account_id: accountId, force }),
  })
  return res.json()
}

export interface JobStatus {
  running: boolean
  name: string
  rc: number | null
  // Frozen at completion by Job.snapshot() -- computing it live would make a
  // finished job's duration keep climbing with the clock.
  elapsed: number
  lines: string[]
  total: number
  // Only ever set for a seed job (its own printed lines) or a migrate/
  // delta/discover job (the ledger's completion fraction) -- see webui.py's
  // _job_progress(). null everywhere else, including when nothing is
  // running at all.
  progressPct: number | null
  // Linear extrapolation from elapsed time and progressPct -- only set
  // while the job is actually running (see webui.py's Job.snapshot()); a
  // stopped job's "time left" is meaningless.
  etaSeconds: number | null
  // true when this is NOT the caller's own account's job -- a system-wide
  // ps-scan fallback for when nothing is running under this account, with
  // no concept of which account actually started it. A caller that already
  // shows a properly account-attributed entry for the same job elsewhere
  // (job_admission.py's active_jobs, via fetchActiveJobs()) must not also
  // render this one -- confirmed live, both rendered for the same real
  // seed job simultaneously before this flag existed.
  external: boolean
}

export async function fetchJob(since = 0, accountId?: string): Promise<JobStatus> {
  // account is for an operator watching a job they started on somebody
  // else's tenant -- the wipe and reset cards can target one, and a job
  // you can start but not watch is worse than one you cannot start.
  const acct = accountId ? `&account=${encodeURIComponent(accountId)}` : ''
  return getJSON<JobStatus>(`/api/job?since=${since}${acct}`)
}

export interface JobResult {
  name: string
  rc: number | null
  started: number
  finished: number
  elapsed: number
  lines: string[]
}

// The last COMPLETED run of `name`, read back from disk -- covers what
// /api/job can't: a tab opened (or refreshed) after the server itself
// restarted, where nothing is running and nothing is left in memory.
// null when that job has never completed at all (or never run).
export async function fetchJobHistory(name: string): Promise<JobResult | null> {
  const { result } = await getJSON<{ result: JobResult | null }>(
    `/api/job_history?name=${encodeURIComponent(name)}`)
  return result
}

export async function setToggles(
  services: Record<string, boolean>,
  dryRun?: boolean
): Promise<{ ok: boolean }> {
  const res = await fetch('/api/toggles', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ services, dry_run: dryRun }),
  })
  return res.json()
}

// -- setup wizard: the same 9 steps and state machine webui.py's own inline
// wizard already drives (wizard.py's build_steps()), reached here through the
// identical endpoints rather than a second implementation of the logic.

export type StepState = 'done' | 'todo' | 'manual' | 'skip'

export interface WizardStep {
  n: number
  title: string
  state: StepState
  note: string
  help: string[]
  auto: string
  manual: boolean
  skipped: boolean
  actions: string[]
}

export interface StatusPayload {
  error?: string
  env: Record<string, string>
  steps: WizardStep[]
  done: number
  total: number
  migrated: number
  failed: number
  users_done: number
  users_total: number
}

export async function fetchStatus(): Promise<StatusPayload> {
  return getJSON<StatusPayload>('/api/status')
}

export interface CheckStepResult {
  ok: boolean
  state?: string
  title?: string
  detail?: string
  msg?: string
  error?: string
}

export async function checkStep(n: number): Promise<CheckStepResult> {
  const res = await fetch('/api/checkstep', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ step: n }),
  })
  return res.json()
}

export interface ConfigFields {
  source_domain: string
  target_domain: string
  source_admin: string
  target_admin: string
}

export interface RunModeSpec {
  label: string
  blurb: string
  runs: string[]
  setup: string[]
}

export interface DeployConfig {
  host: string
  user: string
  port: string
  key: string
  ui_port: string
}

export interface HostInfo {
  hostname: string
  ip: string
  location: string
  code_path: string
  commit: string
  pid: number
}

export interface LogsPayload {
  path: string
  lines: string[]
  /** Per-job transcripts on disk. A launched run's own stdout and stderr go
   *  here, so a traceback that kills a migration is in one of these files
   *  and in no other. */
  jobs?: { account: string; job: string; bytes: number; modified: number }[]
}

export async function fetchLogs(job?: string, account?: string):
    Promise<LogsPayload> {
  const q = job ? `?job=${encodeURIComponent(job)}&account=${
    encodeURIComponent(account || '')}` : ''
  return getJSON<LogsPayload>(`/api/logs${q}`)
}

export interface ConfigPayload {
  config: ConfigFields
  env_path: string
  uploads: Record<string, UploadStatus>
  auth_modes: string[]
  auth_mode: string
  run_mode: string
  run_modes: Record<string, RunModeSpec>
  deploy: DeployConfig
  host: HostInfo
}

export async function fetchConfig(): Promise<ConfigPayload> {
  return getJSON<ConfigPayload>('/api/config')
}

export async function saveConfig(
  fields: ConfigFields
): Promise<{ ok: boolean; error?: string; msg?: string }> {
  const res = await fetch('/api/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(fields),
  })
  return res.json()
}

export async function setRunMode(
  mode: string
): Promise<{ ok: boolean; error?: string; msg?: string }> {
  const res = await fetch('/api/runmode', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode }),
  })
  return res.json()
}

export interface UploadStatus {
  present: boolean
  valid: boolean
  detail?: { client_email?: string; [k: string]: unknown }
  error?: string
  warning?: string
}

export type UploadKind = 'source_key' | 'target_key' | 'oauth_client'

/** Reads the file as text client-side; the raw JSON is what /api/upload
 * validates (well-formed JSON, the right kind of credential) before writing
 * it to disk at mode 0600. Nothing here parses or trusts the content --
 * that happens once, server-side, in upload_credential(). */
export async function uploadCredential(
  kind: UploadKind,
  file: File
): Promise<{ ok: boolean; error?: string; msg?: string }> {
  const content = await file.text()
  const res = await fetch('/api/upload', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ kind, content }),
  })
  return res.json()
}

export interface DwdTenant {
  side: 'source' | 'target'
  domain: string
  admin: string
  client_id: string
  scopes: string
  scope_list: string[]
}

export interface SeedScopes {
  client_id: string
  shares_source_key: boolean
  scopes: string
  scope_list: string[]
  combined: string
  combined_list: string[]
}

export interface DwdPayload {
  tenants: DwdTenant[]
  target_provision?: { scopes: string; scope_list: string[] }
  seed?: SeedScopes
  // The "paste once, never revisit" scope lines: every scope source/target
  // could ever need across every transfer mode and optional-feature toggle,
  // not just whichever ones are on right now. See webui.py's dwd_payload().
  migrate_source_full?: string[]
  migrate_target_full?: string[]
}

export async function fetchDwd(): Promise<DwdPayload> {
  return getJSON<DwdPayload>('/api/dwd')
}

export async function checkDwdNow(): Promise<{ ok: boolean; status: StatusPayload }> {
  const res = await fetch('/api/check_dwd', { method: 'POST' })
  return res.json()
}

export interface ScopeCheck {
  scope: string
  ok: boolean
  error?: string
}

export interface ScopeDiagnosis {
  tenant: string
  domain?: string
  admin?: string
  combined_ok: boolean
  error: string
  scopes: ScopeCheck[]
}

/**
 * Bisects a tenant's combined OAuth scope request scope-by-scope when it
 * fails -- a single unauthorised (or not-yet-propagated) scope fails the
 * *whole* combined request with the same generic unauthorized_client
 * error, whatever else in it is fine. Mints one token per scope against
 * Google, so this is an explicit button click, never something polled.
 */
export async function diagnoseScopes(
  tenant: 'source' | 'target'
): Promise<{ ok: boolean; diagnosis: ScopeDiagnosis; error?: string }> {
  const res = await fetch('/api/scope_diagnosis', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tenant }),
  })
  return res.json()
}

export interface SeedResult {
  ok: boolean
  error?: string
}

export async function runSeed(
  confirmDomain: string,
  scale: string,
  createUsers: boolean,
  reset: boolean,
  allUsers?: boolean,
  createUntilFull?: boolean,
  workers?: string,
  localpartPrefix?: string
): Promise<SeedResult> {
  const res = await fetch('/api/seed', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      confirm_domain: confirmDomain,
      scale,
      create_users: createUsers,
      reset,
      all_users: allUsers,
      create_until_full: createUntilFull,
      // Blank = size to the machine. The server refuses anything past twice
      // its own recommendation; a seed the kernel kills hours in costs more
      // than a slow one.
      workers: workers || undefined,
      // A deleted Workspace address stays taken for 20 days, so a
      // wipe-and-recreate that reuses names fails until they age out.
      localpart_prefix: localpartPrefix || undefined,
    }),
  })
  return res.json()
}

/**
 * Empties the TARGET tenant's seeded data (reset_target.py), typed-domain
 * gated exactly like runSeed -- the browser must type the domain back, the
 * server re-checks it against TARGET_DOMAIN before building the command, and
 * reset_target.py's own assert_sandbox() checks a third time regardless of
 * what this call decides. Nothing here can point at the source: the domain
 * the server compares against comes from Settings(), never from this body.
 */
export async function runResetTarget(
  confirmDomain: string
): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch('/api/reset_target', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ confirm_domain: confirmDomain }),
  })
  return res.json()
}

/**
 * Deletes the SOURCE tenant's users -- the corpus being migrated. Only ever
 * right when reseeding under different usernames, where the old accounts
 * must be gone rather than merely emptied. Gated on the SOURCE domain, so
 * muscle memory from the target button cannot fire this one.
 */
export async function runWipeSource(
  confirmDomain: string, accountId?: string
): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch('/api/wipe_source', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ confirm_domain: confirmDomain, account_id: accountId }),
  })
  return res.json()
}

/**
 * Deletes the target tenant's provisioned USERS (wipe_target.py), which
 * runResetTarget leaves behind -- it empties their data, not their
 * accounts. Same typed-domain gate: the server compares against
 * TARGET_DOMAIN from Settings(), never from this body, and wipe_target.py
 * re-runs reset_target.assert_sandbox() itself.
 */
export async function runWipeTarget(
  confirmDomain: string, accountId?: string
): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch('/api/wipe_target', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ confirm_domain: confirmDomain, account_id: accountId }),
  })
  return res.json()
}

/** Mirrors runResetTarget exactly, against reset_drive_ledger.py --
 *  always the SOURCE domain (see reset_drive_ledger_argv's own docstring
 *  in webui.py: it operates on source_email keys regardless of which
 *  tenant's files were actually wiped). */
export async function runResetDriveLedger(
  confirmDomain: string, services?: string, accountId?: string
): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch('/api/reset_drive_ledger', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    // account_id is the migration being worked on. Resolved from the
    // session alone, a superadmin could only ever reset their own tenant --
    // which came back "set the source domain in step 2 first" rather than
    // saying the account was wrong.
    body: JSON.stringify({ confirm_domain: confirmDomain, services,
                           account_id: accountId || null }),
  })
  return res.json()
}

// -- scope matrix -------------------------------------------------------------
export interface ScopePayload {
  lines: string[]
  totals: Record<string, Record<string, number>>
  volume: Record<string, number>
}

export async function fetchScope(): Promise<ScopePayload> {
  return getJSON<ScopePayload>('/api/scope')
}

// -- identities ----------------------------------------------------------------
export interface IdentityRow {
  source_email: string
  target_email: string
  entity_type: string
  status: string
}

export async function fetchIdentities(): Promise<IdentityRow[]> {
  const data = await getJSON<{ error: string; rows: IdentityRow[] }>('/api/identities')
  if (data.error) throw new Error(data.error)
  return data.rows
}

export async function saveIdentityPair(
  sourceEmail: string, targetEmail: string
): Promise<{ ok: boolean; error?: string; total?: number }> {
  const res = await fetch('/api/identities/save', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ source_email: sourceEmail, target_email: targetEmail }),
  })
  return res.json()
}

export interface DeployFields {
  host: string
  user: string
  port: string
  key: string
  uiPort?: string
}

/**
 * Saves the VPS connection to env.sh (DEPLOY_HOST etc.) without deploying --
 * lets "add my VPS" and "actually deploy to it" be two separate actions.
 * webui.py's own inline UI writes the same env.sh entries; whichever surface
 * saves last is what fetchConfig()'s `deploy` field returns to both.
 */
export async function saveDeployConfig(
  fields: DeployFields
): Promise<{ ok: boolean; error?: string; msg?: string }> {
  const res = await fetch('/api/deploy_config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      host: fields.host, user: fields.user, port: fields.port,
      key: fields.key, ui_port: fields.uiPort,
    }),
  })
  return res.json()
}

/**
 * Copies this tool to the VPS and starts its web UI there (deploy_remote.py).
 * `confirm` must be the literal string "DEPLOY", typed by the operator, and
 * is only checked server-side when includeCredentials is set -- sending
 * service-account keys and OAuth tokens to another host is the one
 * irreversible part of this action.
 */
export interface DeployHistoryEntry {
  id: string
  startedAt: string
  finishedAt: string | null
  host: string
  user: string
  port: string
  uiPort: string
  includeCredentials: boolean
  commit: string
  rc: number | null
}

/**
 * Every past /api/deploy call, most recent first. Previously the only
 * answer to "did the last deploy actually work, and what commit is
 * running on that VPS right now" was SSHing in and checking by hand.
 */
export async function fetchDeployHistory(): Promise<DeployHistoryEntry[]> {
  const data = await getJSON<{ history: DeployHistoryEntry[] }>('/api/deploy_history')
  return data.history
}

export async function runDeploy(
  fields: DeployFields,
  includeCredentials: boolean,
  confirm?: string
): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch('/api/deploy', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      host: fields.host, user: fields.user, port: fields.port,
      key: fields.key, ui_port: fields.uiPort,
      include_credentials: includeCredentials, confirm,
    }),
  })
  return res.json()
}
