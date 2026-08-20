/**
 * controlPlane.ts
 * ===============
 * Client for api_server.py (port 8090) -- the Migration Command Center.
 *
 * Separate from client.ts on purpose. That file talks to webui.py (8080,
 * stdlib, polling); this one talks to the FastAPI control plane over a
 * WebSocket. Keeping them apart means the existing pages keep working
 * untouched if the control plane is not running, which is the normal state
 * on a node that is only executing migrations.
 */

// Overridable at runtime (not just at build time via VITE_CP_BASE) so a
// dashboard already loaded in a browser can be pointed at a different
// tunnel/port without a rebuild -- see the "VPS Connection" panel in
// Settings, which is the only place this setter is called from.
const CP_BASE_DEFAULT = import.meta.env.VITE_CP_BASE ?? 'http://localhost:8090'
let CP_BASE = localStorage.getItem('cp_base') || CP_BASE_DEFAULT

export const getCpBase = () => CP_BASE
export const setCpBase = (url: string) => {
  CP_BASE = url.replace(/\/+$/, '') || CP_BASE_DEFAULT
  localStorage.setItem('cp_base', CP_BASE)
}
const cpWs = () => CP_BASE.replace(/^http/, 'ws') + '/ws'

/** Set once at login. Sent on every request; the server resolves it to a
 *  role and records it against every action in operator_actions_log. */
let OPERATOR = localStorage.getItem('cp_operator') ?? ''
export const setOperator = (name: string) => {
  OPERATOR = name
  localStorage.setItem('cp_operator', name)
}
export const getOperator = () => OPERATOR

/**
 * Probes an arbitrary base URL (not necessarily the one currently in use)
 * so the Settings panel can test a candidate address before committing to
 * it with setCpBase. /api/v2/whoami is the cheapest real round-trip: it
 * needs no reason code, mutates nothing, and still proves RBAC and CORS are
 * both working, not just that something is listening on the port.
 */
export async function checkConnection(base: string): Promise<{ ok: true; role: Role; ms: number } | { ok: false; error: string }> {
  const url = base.replace(/\/+$/, '') || CP_BASE_DEFAULT
  const started = performance.now()
  try {
    const res = await fetch(`${url}/api/v2/whoami`, {
      headers: { 'X-Operator': OPERATOR },
      credentials: 'include',
      signal: AbortSignal.timeout(5000),
    })
    const ms = Math.round(performance.now() - started)
    if (!res.ok) return { ok: false, error: `HTTP ${res.status}` }
    const op = (await res.json()) as Operator
    return { ok: true, role: op.role, ms }
  } catch (e: any) {
    return { ok: false, error: e.name === 'TimeoutError' ? 'timed out after 5s' : e.message }
  }
}

async function cpFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${CP_BASE}${path}`, {
    ...init,
    // Carries the bp_session cookie an account signed in with -- it lives
    // on this server's own origin (CP_BASE), a different one than the page
    // itself (webui.py's), so the browser needs this opt-in to send it at
    // all. See api_server.py's CORS setup: allow_credentials=True exists
    // specifically to accept it back.
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      'X-Operator': OPERATOR,
      ...(init?.headers ?? {}),
    },
  })
  if (!res.ok) {
    // FastAPI puts the human-readable cause in `detail`; surfacing the raw
    // status alone ("403") tells an operator nothing about what to do next.
    let detail = `HTTP ${res.status}`
    try {
      const body = await res.json()
      detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
    } catch { /* non-JSON error body -- keep the status */ }
    throw new Error(detail)
  }
  return res.json() as Promise<T>
}

// -- types -----------------------------------------------------------------
export type Role = 'admin' | 'viewer'
export interface Operator { name: string; role: Role; account_id: number | null }

// -- SaaS accounts -----------------------------------------------------------
export interface Account {
  id: number; email: string; name: string; plan: string; created_at: string
  subscription_active: boolean; is_superadmin: boolean
  /** Opt-in per account (default off) -- whether this account may seed a
   *  tenant with fabricated test data. See accounts_auth.set_seed_enabled. */
  seed_enabled: boolean
}

export const signup = (email: string, password: string, name: string, plan = 'trial') =>
  cpFetch<{ ok: boolean; accountId: number }>('/api/v2/auth/signup', {
    method: 'POST',
    body: JSON.stringify({ email, password, name, plan }),
  })

export const login = (email: string, password: string) =>
  cpFetch<{ ok: boolean; accountId: number }>('/api/v2/auth/login', {
    method: 'POST',
    body: JSON.stringify({ email, password }),
  })

// -- Admin (superadmin only -- see require_superadmin in api_server.py) -----
export const fetchAdminAccounts = () => cpFetch<Account[]>('/api/v2/admin/accounts')

export const setAccountSubscription = (accountId: number, active: boolean, reason: string) =>
  cpFetch<ActionResult>(`/api/v2/admin/accounts/${accountId}/subscription`, {
    method: 'POST',
    body: JSON.stringify({ reason, active }),
  })

export const setAccountSeedEnabled = (accountId: number, enabled: boolean, reason: string) =>
  cpFetch<ActionResult>(`/api/v2/admin/accounts/${accountId}/seed`, {
    method: 'POST',
    body: JSON.stringify({ reason, enabled }),
  })

export const logout = () => cpFetch<{ ok: boolean }>('/api/v2/auth/logout', { method: 'POST' })

export const fetchMe = () => cpFetch<Account>('/api/v2/auth/me')

export interface FleetNode {
  node_id: string
  hostname: string | null
  location: string | null
  code_commit: string | null
  last_seen: string
  cpu_pct: number | null
  ram_pct: number | null
  disk_pct: number | null
  active_job: string | null
  job_pid: number | null
  transfer_mode: string | null
  users_done: number
  users_running: number
  users_failed: number
  error_rate: number
  /** Derived server-side from last_seen, not stored -- a node that dies
   *  cannot mark itself down, so liveness has to be computed on read. */
  healthy: boolean
  secondsSinceHeartbeat: number | null
}

export interface UserProgress {
  source_email: string
  target_email: string
  status: 'PENDING' | 'RUNNING' | 'DONE' | 'FAILED'
  services_done: string
  itemsDone: number
  itemsFailed: number
  itemsSkipped: number
  percent: number
}

export interface FailureRow {
  id: number
  source_user: string
  item_id: string
  item_type: string
  status: string
  error_message: string | null
  timestamp: string
}

export interface ForensicDetail {
  sourceUser: string
  itemId: string
  attempts: Array<{
    id: number; item_type: string; status: string
    error_message: string | null; timestamp: string; bytes_moved: number
  }>
  mapping: { target_id: string; type: string; source_name: string | null } | null
  identity: { target_email: string; status: string } | null
  /** A FAILED row that nonetheless has a mapping was fixed by a later pass.
   *  Without this the UI invites operators to retry finished work. */
  supersededBySuccess: boolean
}

export interface PublicShare {
  id: number; tenant: string; user_email: string; file_id: string
  file_name: string | null; grant_type: string; role: string; detected_at: string
}

export interface OperatorAction {
  id: number; started_at: string; finished_at: string | null
  actor: string; actor_role: string; action: string; target: string | null
  reason: string; outcome: 'PENDING' | 'OK' | 'FAILED' | 'REFUSED'; detail: string | null
}

// -- reads -----------------------------------------------------------------
export const fetchWhoami = () => cpFetch<Operator>('/api/v2/whoami')
export const fetchFleet = () => cpFetch<FleetNode[]>('/api/v2/fleet')

export interface ActiveJobRow {
  account_id: number | null; job_name: string; pid: number | null; started_at: string
}
// job_admission.py's admission table, unscoped by caller -- the one place
// that shows what's occupying the shared capacity slot regardless of which
// account is asking. See RunningNow.tsx: the other sources here (webui.py's
// per-account Job, full-setup/status's own ps scan) only ever see the
// CALLING account's own job, so another account's run -- exactly what a
// "capacity is full" refusal is usually caused by -- was invisible.
export const fetchActiveJobs = () => cpFetch<ActiveJobRow[]>('/api/v2/active-jobs')
export const fetchUsers = () => cpFetch<UserProgress[]>('/api/v2/users')
export const fetchFailures = (user?: string) =>
  cpFetch<FailureRow[]>(`/api/v2/failures${user ? `?source_user=${encodeURIComponent(user)}` : ''}`)
export const fetchForensics = (user: string, itemId: string) =>
  cpFetch<ForensicDetail>(`/api/v2/forensics/${encodeURIComponent(user)}/${encodeURIComponent(itemId)}`)
export const fetchPublicShares = (tenant = 'target') =>
  cpFetch<PublicShare[]>(`/api/v2/public-shares?tenant=${tenant}`)
export const fetchActions = () => cpFetch<OperatorAction[]>('/api/v2/actions')

// -- writes ----------------------------------------------------------------
// Every one takes a `reason`. The server rejects a request without one at the
// type layer (422) before any handler logic runs, so this is not merely a
// UI convention that a future caller could skip.
export interface ActionResult { ok: boolean; actionId: number; detail: string }

export const startMigration = (reason: string, services: string[], users: string[], dryRun = false) =>
  cpFetch<ActionResult>('/api/v2/migrate/start', {
    method: 'POST',
    body: JSON.stringify({ reason, services, users, dry_run: dryRun }),
  })

export const stopJob = (pid: number, reason: string) =>
  cpFetch<ActionResult>(`/api/v2/jobs/${pid}/stop`, {
    method: 'POST', body: JSON.stringify({ reason }),
  })

export const retryItem = (sourceUser: string, itemId: string, reason: string) =>
  cpFetch<ActionResult>('/api/v2/retry', {
    method: 'POST',
    body: JSON.stringify({ reason, source_user: sourceUser, item_id: itemId }),
  })

export interface ProvisionUser {
  email: string
  state: 'created' | 'existing' | 'failed'
  detail: string
}

export interface ProvisionStatus {
  running: boolean; pid: number | null; created: number; failed: number
  /** Accounts that were already there. Counted separately from `created`
   *  because "nothing to do" and "nothing happened" look identical as a
   *  bare 0. */
  existing?: number
  /** The accounts themselves, so the panel can show which addresses are
   *  being created rather than only a fraction. Passwords are stripped
   *  server-side -- provision.py prints each new one once, and this
   *  response goes to a browser. */
  users?: ProvisionUser[]
  total: number; tail: string[]
}

export const startProvision = (reason: string, tenant: 'source' | 'target' = 'target', dryRun = false) =>
  cpFetch<ActionResult>('/api/v2/provision/start', {
    method: 'POST',
    body: JSON.stringify({ reason, tenant, dry_run: dryRun }),
  })

export const fetchProvisionStatus = (tenant: 'source' | 'target' = 'target') =>
  cpFetch<ProvisionStatus>(`/api/v2/provision/status?tenant=${tenant}`)

export interface BenchmarkResult {
  file: string; label: string; startedAt: string; passed: boolean
  elapsedS: number; secPerFile: number; totalFiles: number
  driveFileWorkers: number | null; fidelityPct: number | null
  extraGrants: number | null; failures: string[]
  migrateReturnCode: number | null
  /** The verdict was recomputed under the current gates and disagrees with
   *  the one stored at the time. Shown, not hidden: a row that silently
   *  flipped from PASS to FAIL is indistinguishable from a bug in the table. */
  verdictRestated?: boolean
  storedPassed?: boolean
}

export interface StartBenchmarkBody {
  reason: string; label: string; confirm_domain: string; services: string
  drive_file_workers: number; drive_write_qps: number; skip_wipe: boolean
}

export const startBenchmark = (body: StartBenchmarkBody) =>
  cpFetch<ActionResult>('/api/v2/benchmark/start', {
    method: 'POST', body: JSON.stringify(body),
  })

export const fetchBenchmarkResults = () =>
  cpFetch<BenchmarkResult[]>('/api/v2/benchmark/results')

/** Live state of an in-flight benchmark. `phase` is derived server-side from
 *  which child process is alive, so it stays right regardless of where the
 *  run's own output was redirected. */
export interface BenchmarkRunning {
  running: boolean
  pid?: number
  label?: string
  elapsedS?: number
  phase?: 'starting' | 'wipe' | 'ledger' | 'migrate' | 'audit' | 'judging'
  phaseLabel?: string
  phases?: string[]
  progress?: { files: number; folders: number; failed: number; aclFailed: number }
}

export const fetchBenchmarkRunning = () =>
  cpFetch<BenchmarkRunning>('/api/v2/benchmark/running')

// -- AI diagnostics ---------------------------------------------------------
export interface AiStatus {
  configured: boolean; keyMask: string; model: string; logFile: string | null
}
export interface AiAnalysis {
  markdown: string; error: string; context: string; model: string; actor: string
}

export const fetchAiStatus = () => cpFetch<AiStatus>('/api/v2/ai/status')

export const saveAiKey = (reason: string, key: string) =>
  cpFetch<ActionResult>('/api/v2/ai/key', {
    method: 'POST', body: JSON.stringify({ reason, key }),
  })

/** The exact payload analyze() would send, without sending it. The log tail
 *  contains real addresses and file names, so this exists to let an operator
 *  read what leaves the building first. */
export const fetchAiContext = (prompt = '') =>
  cpFetch<{ context: string; chars: number }>('/api/v2/ai/context', {
    method: 'POST', body: JSON.stringify({ prompt }),
  })

export const runAiAnalysis = (prompt = '') =>
  cpFetch<AiAnalysis>('/api/v2/ai/analyze', {
    method: 'POST', body: JSON.stringify({ prompt }),
  })

// -- coverage audit ----------------------------------------------------------
export interface CoverageRow {
  service: string; item: string; status: string; verdict: string
  count: number | null; note: string
}
export interface CoverageResult {
  rows: CoverageRow[]
  counts: { covered: number; absent: number; unprobed: number }
  errors: Record<string, string>
  externalSharedWithMe: number
  migrateExternalShares: boolean
}
export interface CoverageStatus {
  running: boolean; file?: string; result: CoverageResult | null
}

export const startCoverage = (reason: string) =>
  cpFetch<ActionResult>('/api/v2/coverage/start', {
    method: 'POST', body: JSON.stringify({ reason }),
  })

export const fetchCoverageStatus = () =>
  cpFetch<CoverageStatus>('/api/v2/coverage/status')

// -- Cloud provisioning -------------------------------------------------------
export interface GcpStep { name: string; status: string; detail: string }
export interface GcpSide {
  side: string; project: string; saEmail?: string; keyPath?: string
  clientId: string; ok: boolean; steps: GcpStep[]
}
export interface GcpResult {
  ok: boolean; error?: string; account?: string; org?: string
  sides: GcpSide[]
}
export interface GcpStatus { running: boolean; result: GcpResult | null }

export const startGcpProvision = (
  reason: string, sourceDomain: string, targetDomain: string,
  orgId = '', dryRun = true, force = false,
) =>
  cpFetch<ActionResult>('/api/v2/gcp/provision', {
    method: 'POST',
    body: JSON.stringify({
      reason, source_domain: sourceDomain, target_domain: targetDomain,
      org_id: orgId, dry_run: dryRun, force,
    }),
  })

export const fetchGcpStatus = () => cpFetch<GcpStatus>('/api/v2/gcp/status')

export const enableApis = (reason: string) =>
  cpFetch<ActionResult>('/api/v2/apis/enable', {
    method: 'POST', body: JSON.stringify({ reason }),
  })

// -- Full setup: one tenant, one call ------------------------------------------
export interface FullSetupPhase { name: string; status: string; detail: string }
export interface FullSetupResult {
  side: string; ok: boolean; phases: FullSetupPhase[]
  clientId?: string; missingScopes?: string[]
}
export interface FullSetupStatus {
  running: boolean; result: FullSetupResult | null
  /** Live progress while running -- null before the first checkpoint, and
   * whenever nothing is currently running (see api_server.py's
   * full_setup_status: a finished/crashed run's last checkpoint would
   * otherwise read as this run's own progress). */
  progressPct?: number | null; progressLabel?: string | null
  /** The running process's pid, found by the same ps scan that sets
   * `running` -- null whenever nothing is running. Nothing else records
   * this anywhere queryable later, so it's what makes Stop possible via
   * the generic /api/v2/jobs/{pid}/stop (SIGINT-by-pid). */
  pid?: number | null
}

/**
 * Project -> APIs -> service account -> key -> delegation -> verified, in
 * one call. Only works wherever the control plane process itself has
 * gcloud and a real browser -- it cannot run on a headless VPS, and fails
 * cleanly there rather than hanging (see full_setup.py's own docstring).
 *
 * `adminPassword` is used exactly once by the server to fill dwd_helper's
 * sign-in form and is never stored: not in this client, not in
 * localStorage, not in the audit log (the API schema excludes it from
 * serialization at the type level specifically so no future write endpoint
 * can forget and log it in plaintext).
 */
export const startFullSetup = (
  reason: string, side: 'source' | 'target', domain: string,
  adminEmail: string, adminPassword: string, opts: {
    orgId?: string; dryRun?: boolean; seed?: boolean; seedScale?: string
    createUsers?: boolean; provisionUsers?: boolean
    /** Build a NEW Cloud project even though a key is on file. Mints a new
     *  service account and client ID, so the delegation in place stops
     *  applying until this run re-grants it. Requires confirmDomain. */
    reprovision?: boolean; confirmDomain?: string
    /** Operator-chosen scope line. Required scopes are unioned back in by
     *  the server regardless -- omitting one does not narrow the migration,
     *  it breaks it. */
    scopes?: string[]
  } = {},
) =>
  cpFetch<ActionResult>('/api/v2/full-setup/start', {
    method: 'POST',
    body: JSON.stringify({
      reason, side, domain, admin_email: adminEmail,
      admin_password: adminPassword,
      org_id: opts.orgId ?? '', dry_run: opts.dryRun ?? true,
      seed: opts.seed ?? false, seed_scale: opts.seedScale ?? 'small',
      create_users: opts.createUsers ?? false,
      provision_users: opts.provisionUsers ?? false,
      reprovision: opts.reprovision ?? false,
      confirm_domain: opts.confirmDomain ?? '',
      scopes: opts.scopes ?? [],
    }),
  })

export interface ScopeOptions {
  side: 'source' | 'target'
  /** Cannot be deselected: a delegated token request fails WHOLE if any
   *  requested scope is ungranted, so dropping one of these does not make a
   *  narrower migration, it makes a tenant that cannot migrate. */
  required: string[]
  optional: string[]
  default: string[]
}

export const fetchScopeOptions = (side: 'source' | 'target') =>
  cpFetch<ScopeOptions>(`/api/v2/setup/scope-options?side=${side}`)

export const fetchFullSetupStatus = (side: 'source' | 'target') =>
  cpFetch<FullSetupStatus>(`/api/v2/full-setup/status?side=${side}`)

// -- GCP / DWD teardown -- the reverse of full-setup ---------------------------
export interface TeardownResult {
  ok: boolean
  phases: FullSetupPhase[]
}

export interface TeardownStatus {
  running: boolean; result: TeardownResult | null
  progressPct?: number | null; progressLabel?: string | null
}

/** Superadmin-only server-side (require_superadmin) -- deletes a real GCP
 *  project (soft-deleted, 30-day recovery) and/or revokes a real,
 *  non-undoable Admin Console delegation entry. Either can be omitted. */
export const startTeardown = (
  reason: string, adminEmail: string, adminPassword: string,
  opts: { project?: string; clientId?: string } = {},
) =>
  cpFetch<ActionResult>('/api/v2/teardown/start', {
    method: 'POST',
    body: JSON.stringify({
      reason, admin_email: adminEmail, admin_password: adminPassword,
      project: opts.project ?? '', client_id: opts.clientId ?? '',
    }),
  })

export const fetchTeardownStatus = () =>
  cpFetch<TeardownStatus>('/api/v2/teardown/status')

// -- Client-side Cloud provisioning handoff ----------------------------------
// The Cloud project itself is created on the admin's own machine (their own
// gcloud identity, their own org permissions -- see provision_gcp.py, which
// runs standalone). This browser tab's already-authenticated session is what
// uploads the resulting service-account key; no separate token ever touches
// a terminal.
export interface TenantConfigStatus {
  side: 'source' | 'target'; domain: string; adminEmail: string
  hasKey: boolean; clientId: string; scopes: string[]
}

export const fetchTenantConfigStatus = (side: 'source' | 'target') =>
  cpFetch<TenantConfigStatus>(`/api/v2/setup/tenant-config?side=${side}`)

// -- Tenant inventory --------------------------------------------------------
// Two live Google calls per account, so this is explicit-trigger only and
// must never go on a poll loop (same rule as every other live-API read).
export interface TenantInventoryUser {
  email: string
  emails: number | null
  threads: number | null
  driveBytes: number | null
  license: string
  error: string
  // Deep-scan only -- absent until a deep scan has run.
  driveKinds?: Record<string, number>
  shared?: number
  external?: number
  anyone?: number
  calendarEvents?: number | null
  calendars?: number | null
  chatSpaces?: number | null
  chatMessages?: number | null
}

export interface TenantInventory {
  side: 'source' | 'target'
  domain: string
  accounts: number
  users: TenantInventoryUser[]
  // `covered` is the number of accounts the totals are actually built from.
  // It can be lower than `accounts` -- a suspended or never-provisioned
  // mailbox answers 400/401 -- and the UI must show the denominator rather
  // than presenting a partial sum as the whole tenant.
  totals: {
    emails: number; threads: number; driveBytes: number; covered: number
    // Deep-scan only.
    shared?: number; external?: number; anyone?: number
    calendarEvents?: number; chatSpaces?: number; chatMessages?: number
    driveKinds?: Record<string, number>
  }
  truncated: boolean
  error: string
  deep: boolean
  /** How many accounts the deep scan actually walked. Always fewer than
   *  `accounts` -- one account's Drive took 180s to walk on a real tenant,
   *  so the sharing numbers are this sample's, never the tenant's. */
  deepSampled: number
  // Licences come from a scope most tenants have never granted, so "could
  // not read" and "this tenant has none" must stay distinguishable.
  licenseCounts: Record<string, number>
  licenseError: string
}

/** A deep scan runs for minutes to hours -- walking every file every account
 *  owns -- so it is a background job, not a request. Doing it synchronously
 *  502'd: the proxy gave up after 91s with nothing to show for the API calls
 *  already spent. */
export interface TenantScanState extends Partial<TenantInventory> {
  running: boolean
  present: boolean
  elapsed?: number
  error?: string
}

export const startTenantScan = (side: 'source' | 'target', accounts = 0) =>
  cpFetch<{ started: boolean; detail: string }>(
    `/api/v2/setup/tenant-inventory/scan?side=${side}&accounts=${accounts}`,
    { method: 'POST' })

export const fetchTenantScan = (side: 'source' | 'target') =>
  cpFetch<TenantScanState>(
    `/api/v2/setup/tenant-inventory/scan?side=${side}`)

export const fetchTenantInventory = (
  side: 'source' | 'target', limit = 250, deep = false,
) =>
  cpFetch<TenantInventory>(
    `/api/v2/setup/tenant-inventory?side=${side}&limit=${limit}&deep=${deep}`)

export const uploadCredentials = (
  reason: string, side: 'source' | 'target', domain: string,
  serviceAccountKey: Record<string, unknown>,
) =>
  cpFetch<ActionResult>('/api/v2/setup/credentials', {
    method: 'POST',
    body: JSON.stringify({
      reason, side, domain, service_account_key: serviceAccountKey,
    }),
  })

// -- DWD scope status ---------------------------------------------------------
export interface DwdStatus {
  tenant: string; checked: boolean; clientId?: string
  live?: number; total?: number; missing?: string[]; error?: string
  /** APIs that report ENABLED but still need a manual console step before
   *  they work at all (Chat needs an app configured, or every call 404s). */
  caveats?: { api: string; note: string }[]
}

export const fetchDwdStatus = (tenant: 'source' | 'target') =>
  cpFetch<DwdStatus>(`/api/v2/dwd/status?tenant=${tenant}`)

// -- Verified domains ---------------------------------------------------------
// Which domain(s) this account has actually finished setting up and can use
// right now -- the same functional scope check as DwdStatus above, but
// scoped to whoever is asking (not always the legacy env.sh tenant) and
// covering both sides in one call.
export interface VerifiedDomain {
  side: 'source' | 'target'; domain: string; adminEmail: string
  status: 'verified' | 'pending' | 'not_verified' | 'not_set_up' | 'error'
  live: number; total: number; error?: string
}

export const fetchVerifiedDomains = () =>
  cpFetch<{ domains: VerifiedDomain[] }>('/api/v2/setup/verified-domains')

export const revertPublicShares = (reason: string, tenant = 'target') =>
  cpFetch<ActionResult>('/api/v2/emergency/revert-public', {
    method: 'POST',
    body: JSON.stringify({ reason, tenant, confirm: 'REVERT' }),
  })

// -- websocket -------------------------------------------------------------
export type CPEventType =
  | 'SNAPSHOT' | 'JOB_PROGRESS' | 'NODE_HEARTBEAT'
  | 'CRITICAL_ALERT' | 'ACTION_COMPLETE' | 'TAILER_ERROR'

export interface CPEvent<T = any> { type: CPEventType; ts: string; data: T }

/**
 * Auto-reconnecting socket. Returns a close function.
 *
 * Reconnect backoff matters more than it looks: the control plane runs on the
 * same box as the migration, so a restart during a long run is normal, and a
 * tight reconnect loop from several open dashboards is a self-inflicted load
 * spike on a host that is already busy moving terabytes.
 */
export function connectCP(
  onEvent: (e: CPEvent) => void,
  onStatus?: (connected: boolean) => void,
): () => void {
  let ws: WebSocket | null = null
  let closed = false
  let attempt = 0
  let keepalive: ReturnType<typeof setInterval> | null = null

  const open = () => {
    if (closed) return
    ws = new WebSocket(cpWs())
    ws.onopen = () => {
      attempt = 0
      onStatus?.(true)
      // The server is push-only; this exists so an idle proxy does not reap
      // the connection as dead.
      keepalive = setInterval(() => ws?.readyState === 1 && ws.send('ping'), 25_000)
    }
    ws.onmessage = (ev) => {
      try { onEvent(JSON.parse(ev.data) as CPEvent) } catch { /* ignore junk */ }
    }
    ws.onclose = () => {
      if (keepalive) clearInterval(keepalive)
      onStatus?.(false)
      if (closed) return
      const delay = Math.min(1000 * 2 ** attempt++, 30_000)
      setTimeout(open, delay)
    }
    ws.onerror = () => ws?.close()
  }
  open()
  return () => { closed = true; if (keepalive) clearInterval(keepalive); ws?.close() }
}
