/**
 * The numbers on the boxes, and only real ones.
 *
 * Each node's live line comes from a source that actually measures it: the
 * ledger's per-stage counters, the fleet's heartbeats, the queue, the dead-man
 * switch, and the metrics the migrating process records. A node with no
 * source gets no line -- never a placeholder that reads as a measurement.
 *
 * Two rules carried over from the rest of the app. Counts stay counts (a
 * stage says "running 129/300 users", never one averaged percentage). And a
 * metric from a run that has ended is labelled as such: the migrating process
 * records snapshots, and the last one freezes when the run stops, so without
 * this a finished run's rates would read as current.
 */
import type { GLive } from '@/components/NodeGraph'
import type { QueueSnapshot } from '@/api/client'
import type { DeadmanStatus, FleetNode, MetricsSnapshot } from '@/api/controlPlane'
import type { MigrationStage } from '@/types'
import { NODES } from './model'

const GREEN = '#3ddc84', BLUE = '#4aa8ff', RED = '#ff5c5c', AMBER = '#ffb02e', GREY = '#777'

const STATUS: Record<string, [string, string]> = {   // status -> [dot colour, word]
  completed: [GREEN, 'done'], verified: [GREEN, 'verified'],
  in_progress: [BLUE, 'running'], retrying: [BLUE, 'retrying'],
  failed: [RED, 'failed'], mismatch: [RED, 'mismatch'],
  needs_attention: [AMBER, 'attention'], paused: [AMBER, 'paused'],
  waiting: [GREY, 'waiting'], pending: [GREY, 'pending'], not_started: [GREY, 'not started'],
}
// Stages whose usersCompleted is a real per-user tally. Authentication,
// validation and the report are yes/no, so they get a status word only.
const COUNTED = new Set(['discovery', 'gmail', 'drive', 'calendar', 'contacts', 'chat', 'permissions'])

/** A migration records a snapshot every 15 s; past this it is not running. */
export const FRESH_SEC = 30 * 60

const compact = (n: number) =>
  new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 }).format(n)
const gb = (b: number) => `${Math.round(b / 1024 ** 3)}`

const ago = (sec: number) => sec < 3600 ? `${Math.round(sec / 60)}m ago`
  : sec < 86400 ? `${Math.round(sec / 3600)}h ago` : `${Math.round(sec / 86400)}d ago`

export interface LiveInput {
  stages: MigrationStage[] | null
  fleet: FleetNode[] | null
  metrics: MetricsSnapshot | null
  queue: QueueSnapshot | null
  deadman: DeadmanStatus | null
  now?: number
}

export function liveFor({ stages, fleet, metrics, queue, deadman, now = Date.now() }: LiveInput): Record<string, GLive> {
  const out: Record<string, GLive> = {}

  // Per-stage counters straight from the ledger.
  for (const n of NODES) {
    const st = n.stage ? stages?.find((x) => x.id === n.stage) : undefined
    if (!n.stage || !st) continue
    if (n.stage === 'user_creation') { out[n.id] = { color: GREY, text: 'not tracked' }; continue }
    const [color, word] = STATUS[st.status] ?? [GREY, st.status]
    out[n.id] = {
      color, active: st.status === 'in_progress',
      text: COUNTED.has(n.stage) ? `${word} ${st.usersCompleted}/${st.usersTotal} users` : word,
    }
  }

  if (fleet) {
    const running = fleet.reduce((s, f) => s + f.users_running, 0)
    out.fleet = { color: fleet.length ? GREEN : GREY, active: running > 0,
                  text: `${fleet.length} node(s) · ${running} running` }
  }

  if (queue) {
    out.runner = { color: queue.running.length ? BLUE : GREY, active: queue.running.length > 0,
                   text: queue.running.length ? `${queue.running.length} running` : 'idle' }
    out.admit = { color: queue.waiting.length ? AMBER : GREEN,
                  text: `${queue.running.length}/${queue.capacity} slots · ${queue.waiting.length} waiting` }
  }

  if (deadman) {
    const left = deadman.secondsRemaining
    const text = !deadman.armed ? 'not armed'
      : left == null ? 'armed'
      : `armed · ${left >= 86400 ? `${Math.floor(left / 86400)}d ${Math.floor((left % 86400) / 3600)}h`
        : `${Math.floor(left / 3600)}h ${Math.floor((left % 3600) / 60)}m`} left`
    out.deadman = { color: !deadman.armed ? GREY : left != null && left < 3600 ? RED
      : left != null && left < 86400 ? AMBER : GREEN, text }
  }

  if (metrics && !metrics.error) {
    const l = metrics.latest
    const age = l ? Math.max(0, (now - Date.parse(l.recordedAt)) / 1000) : null
    const fresh = age != null && !Number.isNaN(age) && age < FRESH_SEC

    // Ledger-derived, so true whether or not a run is going.
    if (metrics.volume) {
      const rows = metrics.volume.reduce((s, v) => s + v.count, 0)
      const failed = metrics.volume.filter((v) => v.status === 'FAILED' || v.status === 'BLOCKED')
        .reduce((s, v) => s + v.count, 0)
      out.audit_log = { color: failed ? AMBER : GREEN, text: `${compact(rows)} rows · ${compact(failed)} failed` }
    }
    if (metrics.mappings) {
      out.id_mapping = { color: GREEN, text: `${compact(metrics.mappings.reduce((s, m) => s + m.count, 0))} mapped` }
    }
    if (metrics.transfer && metrics.transfer.dailyCapBytes > 0) {
      const used = metrics.transfer.bytesToday
      const cap = metrics.transfer.dailyCapBytes
      out.quota = { color: used / cap > 0.9 ? RED : used / cap > 0.7 ? AMBER : GREEN,
                    text: `${gb(used)}/${gb(cap)} GB today` }
    }
    if (metrics.host) {
      out.sizing = metrics.host.underMemoryPressure
        ? { color: AMBER, text: 'memory pressure' }
        : { color: GREEN, text: `${metrics.host.userWorkers} workers · ${metrics.host.cores} cores` }
    }

    // Recorded by the migrating process: only current while it runs.
    for (const [id, key] of [['lim_src', 'source'], ['lim_tgt', 'target']] as const) {
      const lim = metrics.limiters?.[key]
      if (!lim) continue
      out[id] = fresh
        ? { color: lim.backoffs ? AMBER : GREEN, text: `${Math.round(lim.rate)}/s · ${lim.backoffs} pushbacks` }
        : { color: GREY, text: `idle · last ${Math.round(lim.rate)}/s` }
    }
    if (l) {
      out.pool = fresh
        ? { color: GREEN, active: true, text: `${l.requestsPerSec.toFixed(1)} req/s` }
        : { color: GREY, text: 'idle' }
      out.run_metrics = fresh
        ? { color: GREEN, text: `${l.requestsPerSec.toFixed(1)} req/s · p95 ${l.p95 >= 1 ? `${l.p95.toFixed(1)}s` : `${Math.round(l.p95 * 1000)}ms`}` }
        : { color: GREY, text: `last ${l.requestsPerSec.toFixed(1)} req/s · ${age != null ? ago(age) : '?'}` }
    }
  }
  return out
}
