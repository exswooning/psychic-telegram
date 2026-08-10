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
export interface Operator { name: string; role: Role }

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

export interface ProvisionStatus {
  running: boolean; pid: number | null; created: number; failed: number
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

export const fetchBenchmarkRunning = () =>
  cpFetch<{ running: boolean; pid?: number; label?: string }>('/api/v2/benchmark/running')

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
