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
