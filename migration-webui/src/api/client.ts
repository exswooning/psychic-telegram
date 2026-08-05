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

export async function stopJob(): Promise<{ ok: boolean; msg: string }> {
  const res = await fetch('/api/stop', { method: 'POST' })
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
}

export async function fetchJob(since = 0): Promise<JobStatus> {
  return getJSON<JobStatus>(`/api/job?since=${since}`)
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

export interface ConfigPayload {
  config: ConfigFields
  env_path: string
  uploads: Record<string, UploadStatus>
  auth_modes: string[]
  auth_mode: string
  run_mode: string
  run_modes: Record<string, RunModeSpec>
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

export interface DwdPayload {
  tenants: DwdTenant[]
  target_provision?: { scopes: string; scope_list: string[] }
  seed?: { scopes: string; scope_list: string[]; combined: string; combined_list: string[] }
}

export async function fetchDwd(): Promise<DwdPayload> {
  return getJSON<DwdPayload>('/api/dwd')
}

export async function checkDwdNow(): Promise<{ ok: boolean; status: StatusPayload }> {
  const res = await fetch('/api/check_dwd', { method: 'POST' })
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
  reset: boolean
): Promise<SeedResult> {
  const res = await fetch('/api/seed', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      confirm_domain: confirmDomain,
      scale,
      create_users: createUsers,
      reset,
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
