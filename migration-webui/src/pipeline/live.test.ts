import { describe, it, expect } from 'vitest'
import { FRESH_SEC, liveFor } from './live'
import type { MetricsSnapshot } from '@/api/controlPlane'

const NOW = Date.parse('2026-09-26T12:00:00Z')
const none = { stages: null, fleet: null, metrics: null, queue: null, deadman: null, now: NOW }
const stage = (id: string, status: string, done: number) =>
  ({ id, name: id, description: '', status, progress: 0, usersCompleted: done, usersTotal: 300, expanded: false })
const metrics = (ageSec: number, extra: Partial<MetricsSnapshot> = {}): MetricsSnapshot => ({
  accountId: 1, error: '', operations: [], history: [],
  latest: { recordedAt: new Date(NOW - ageSec * 1000).toISOString(), elapsedSec: 1, calls: 1, workers: 45,
            requestsPerSec: 66.4, requestsPerSecPerWorker: 1, p50: 0.5, p95: 1.7, p99: 2, retries: 0, failures: 0 },
  limiters: { source: { rate: 1200, floor: 5, ceiling: 1200, rejections: 0, backoffs: 0 },
              target: { rate: 45.2, floor: 5, ceiling: 1200, rejections: 9, backoffs: 6 } },
  ...extra,
} as MetricsSnapshot)

describe('live readings', () => {
  it('shows nothing at all when nothing is measured -- no placeholders', () => {
    expect(liveFor(none)).toEqual({})
  })

  it('shows stage counts as counts, never a percentage', () => {
    const l = liveFor({ ...none, stages: [stage('drive', 'in_progress', 129), stage('report', 'waiting', 0)] as never })
    expect(l.drive.text).toBe('running 129/300 users')
    expect(l.drive.active).toBe(true)
    expect(l.report.text).toBe('waiting')            // yes/no stage: a word, no count
    expect(JSON.stringify(l)).not.toContain('%')
  })

  it('says an untracked stage is untracked', () => {
    expect(liveFor({ ...none, stages: [stage('user_creation', 'not_started', 0)] as never }).provision.text)
      .toBe('not tracked')
  })

  it('reports the fleet, the queue and the dead-man switch from their own sources', () => {
    const l = liveFor({
      ...none,
      fleet: [{ users_running: 24 }, { users_running: 0 }] as never,
      queue: { capacity: 2, running: [{ jobName: 'seed' }], waiting: [], recent: [] } as never,
      deadman: { armed: true, secondsRemaining: 3 * 86400 + 4 * 3600 } as never,
    })
    expect(l.fleet.text).toBe('2 node(s) · 24 running')
    expect(l.admit.text).toBe('1/2 slots · 0 waiting')
    expect(l.runner.text).toBe('1 running')
    expect(l.deadman).toMatchObject({ text: 'armed · 3d 4h left', color: '#3ddc84' })
  })

  it('turns the dead-man switch red when little time is left, and grey when unarmed', () => {
    expect(liveFor({ ...none, deadman: { armed: true, secondsRemaining: 600 } as never }).deadman.color).toBe('#ff5c5c')
    expect(liveFor({ ...none, deadman: { armed: false, secondsRemaining: null } as never }).deadman.text).toBe('not armed')
  })

  describe('rates recorded by the migrating process', () => {
    it('show as current while a run is recent', () => {
      const l = liveFor({ ...none, metrics: metrics(60) })
      expect(l.lim_tgt.text).toBe('45/s · 6 pushbacks')
      expect(l.pool.text).toBe('66.4 req/s')
      expect(l.run_metrics.text).toBe('66.4 req/s · p95 1.7s')
    })

    it('are labelled idle once the run has ended, never presented as current', () => {
      const l = liveFor({ ...none, metrics: metrics(FRESH_SEC + 60) })
      expect(l.lim_tgt.text).toBe('idle · last 45/s')
      expect(l.pool.text).toBe('idle')
      expect(l.run_metrics.text).toMatch(/^last 66\.4 req\/s · \d+m ago$/)
      expect(l.run_metrics.color).toBe('#777')
    })
  })

  it('reads ledger-derived figures whether or not a run is going', () => {
    const l = liveFor({ ...none, metrics: metrics(999999, {
      volume: [{ itemType: 'file', status: 'SUCCESS', count: 1_200_000 },
               { itemType: 'file', status: 'FAILED', count: 87_000 },
               { itemType: 'file', status: 'SKIPPED_TOO_LARGE', count: 5 }],
      mappings: [{ type: 'file', count: 604_000 }],
      transfer: { bytesToday: 310 * 1024 ** 3, dailyCapBytes: 750 * 1024 ** 3 },
      host: { cores: 2, userWorkers: 45, underMemoryPressure: false } as never,
    }) })
    expect(l.audit_log.text).toBe('1.3M rows · 87K failed')
    expect(l.id_mapping.text).toBe('604K mapped')
    expect(l.quota.text).toBe('310/750 GB today')
    expect(l.sizing.text).toBe('45 workers · 2 cores')
  })

  it('flags memory pressure and a nearly spent daily cap', () => {
    const l = liveFor({ ...none, metrics: metrics(1, {
      transfer: { bytesToday: 700 * 1024 ** 3, dailyCapBytes: 750 * 1024 ** 3 },
      host: { cores: 2, userWorkers: 1, underMemoryPressure: true } as never }) })
    expect(l.sizing.text).toBe('memory pressure')
    expect(l.quota.color).toBe('#ff5c5c')
  })

  it('ignores a metrics response that carries an error', () => {
    expect(liveFor({ ...none, metrics: metrics(1, { error: 'no metrics recorded yet' }) })).toEqual({})
  })

  it('keeps every reading short enough for its box', () => {
    const l = liveFor({ ...none, metrics: metrics(1), stages: [stage('chat', 'needs_attention', 300)] as never })
    for (const [id, v] of Object.entries(l)) expect(v.text.length, `${id}: ${v.text}`).toBeLessThanOrEqual(25)
  })
})
