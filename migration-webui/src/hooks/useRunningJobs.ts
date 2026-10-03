import { useCallback, useEffect, useState } from 'react'
import {
  fetchTenantConfigStatus, fetchFullSetupStatus, fetchFleet, FleetNode,
  fetchActiveJobs, fetchMe, stopJob as stopFleetJob,
  fetchProvisionStatus, ProvisionStatus,
} from '@/api/controlPlane'
import { fetchJob, stopJob as stopSeedJob } from '@/api/client'

/** What the job IS, so a card can say so rather than showing a bare name.
 *  Derived from the job's own name because that is the only thing every
 *  source here agrees on -- webui Jobs, the fleet, job_admission rows and
 *  full-setup all name themselves differently otherwise. */
export type JobKind = 'seed' | 'migrate' | 'setup' | 'reset' | 'provision' | 'other'

export function jobKind(name: string): JobKind {
  const n = (name || '').toLowerCase()
  if (n.includes('seed')) return 'seed'
  if (n.includes('reset') || n.includes('wipe')) return 'reset'
  if (n.includes('provision')) return 'provision'
  if (n.includes('setup')) return 'setup'
  if (n.includes('migrate') || n.includes('delta')) return 'migrate'
  return 'other'
}

export interface RunningJob {
  /** The server already sent this job a Stop: the next one forces it. */
  stopAsked?: boolean
  key: string; label: string; detail: string; pct: number | null
  /** seed | migrate | ... -- what the rectangle announces. */
  kind: JobKind
  /** The tenant this is happening to, when the source knows it. */
  domain?: string
  // Only ever populated for the webui.py Job entry (seed/reset target/
  // reset drive ledger) -- that's the one source here with real printed
  // output. full-setup's own progress file only ever carries a
  // pct+label checkpoint, not a transcript; fleet/cross-account entries
  // have no output source at all.
  lines?: string[]
  /** Wall-clock seconds so far, for the observed-throughput figures the
   *  seed dashboard derives. Same source as `detail`, kept numeric. */
  elapsedSec?: number
  /** This run is over. Separate from finishedAt because a run recovered
   *  from a transcript has no recorded finish TIME, and inferring "still
   *  running" from a missing timestamp is how a card marked completed sat
   *  next to a dialog reporting "running" under a bar that never stopped. */
  done?: boolean
  /** Epoch seconds this run ended, and its exit code, when known. */
  finishedAt?: number
  rc?: number | null
  /** Helper machines seeding the SAME domain from outside main.py/webui.py
   *  entirely -- SeedRunDashboard's own Nodes section, so a helper's
   *  progress sits on the same page as the run it is helping with, not as
   *  an unrelated-looking idle row on /nodes. */
  nodes?: FleetNode[]
  // Absent for a job admitted under a DIFFERENT account -- job_admission.py
  // never records a stoppable pid for seed/reset-target/full-setup (only
  // this account's own rich sources below know that), and stopping
  // someone else's job from here isn't a call this page should make anyway.
  stop?: (reason: string, force?: boolean) => Promise<void>
}

/** "32m 08s", because "1928s elapsed" makes a reader do arithmetic. */
export const describeElapsed = (sec: number): string => {
  if (!sec || sec < 0) return '0s'
  const h = Math.floor(sec / 3600)
  const m = Math.floor((sec % 3600) / 60)
  const s = Math.floor(sec % 60)
  if (h) return `${h}h ${String(m).padStart(2, '0')}m`
  if (m) return `${m}m ${String(s).padStart(2, '0')}s`
  return `${s}s`
}

/** The newest line that says something, skipping blanks and the warnings
 *  every Google client prints on import -- those are the last lines of a
 *  transcript far more often than anything about the actual work. */
const latestLine = (lines?: string[]): string | null => {
  if (!lines?.length) return null
  for (let i = lines.length - 1; i >= 0; i--) {
    const t = lines[i].trim()
    if (!t) continue
    if (/^(warnings\.warn|FutureWarning|\s*$)/.test(t)) continue
    if (t.startsWith('/') && t.includes('site-packages')) continue
    return t.length > 160 ? `${t.slice(0, 157)}…` : t
  }
  return null
}

// job_admission.py job_name -> a readable label for the fallback,
// cross-account entry (see below). 'migrate'/'delta'/'discover' are
// deliberately absent: fleet_agent.py's ps scan already finds those for
// ANY account, so they never need this fallback in the first place.
const ACCOUNT_SCOPED_JOB_NAMES = new Set([
  'seed', 'reset target', 'reset drive ledger', 'full_setup',
])

/**
 * job_admission.py caps the whole box at ONE heavy job (seed, reset
 * target, migrate, delta, full-setup) running at a time -- "capacity is
 * full, another job is already running, try again shortly" is a real,
 * common refusal, and before this page there was nowhere to see WHAT that
 * job actually is, only that something was blocking a new one. Same three
 * sources Jobs.tsx already reads (full_setup's own progress file, webui.py's
 * per-account Job, main.py found live via fleet_agent.py's ps scan) -- no
 * new backend surface. A dedicated page rather than a banner buried inside
 * Activity, since capacity refusals can happen from anywhere in the app,
 * not just while looking at that one page.
 */
export function useRunningJobs() {
  const [jobs, setJobs] = useState<RunningJob[]>([])
  const [loading, setLoading] = useState(false)

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const [srcCfg, tgtCfg, srcSetup, tgtSetup, job, nodes, activeJobs, me] = await Promise.all([
        fetchTenantConfigStatus('source').catch(() => null),
        fetchTenantConfigStatus('target').catch(() => null),
        fetchFullSetupStatus('source').catch(() => null),
        fetchFullSetupStatus('target').catch(() => null),
        fetchJob(0).catch(() => null),
        fetchFleet().catch(() => [] as FleetNode[]),
        fetchActiveJobs().catch(() => []),
        fetchMe().catch(() => null),
      ])
      const provisions: Array<['source' | 'target', ProvisionStatus | null]> =
        await Promise.all((['source', 'target'] as const).map(async (t) =>
          [t, await fetchProvisionStatus(t).catch(() => null)] as
            ['source' | 'target', ProvisionStatus | null]))
      const myAccountId = me?.id ?? null
      const found: RunningJob[] = []
      if (srcSetup?.running) {
        found.push({
          key: 'setup-source', kind: 'setup', domain: srcCfg?.domain,
          label: srcCfg?.domain || 'source',
          detail: srcSetup.progressLabel || 'setting up…', pct: srcSetup.progressPct ?? null,
          stop: async (reason, force) => {
            if (!srcSetup.pid) throw new Error('no pid recorded for this run yet -- try again shortly')
            const r = await stopFleetJob(srcSetup.pid, reason, force)
            if (!r.ok) throw new Error(r.detail || 'could not stop')
          },
        })
      }
      if (tgtSetup?.running) {
        found.push({
          key: 'setup-target', kind: 'setup', domain: tgtCfg?.domain,
          label: tgtCfg?.domain || 'target',
          detail: tgtSetup.progressLabel || 'setting up…', pct: tgtSetup.progressPct ?? null,
          stop: async (reason, force) => {
            if (!tgtSetup.pid) throw new Error('no pid recorded for this run yet -- try again shortly')
            const r = await stopFleetJob(tgtSetup.pid, reason, force)
            if (!r.ok) throw new Error(r.detail || 'could not stop')
          },
        })
      }
      // `external` means "not in this server process's memory" -- NOT "not
      // mine". Every webui restart (i.e. every deploy) drops the in-memory
      // Job while systemd's KillMode=process keeps the child running, so an
      // account's OWN job starts reporting external within minutes of
      // starting it. job_admission is the actual ownership record, so ask
      // it. Gating on `external` alone meant the owning account fell
      // through both branches -- skipped here as "not mine", skipped below
      // as "already shown above" -- and saw nothing at all for a seed it
      // had started itself, which is the very failure this page exists to
      // prevent.
      // The admission row that describes the job /api/job returned -- the
      // same process, not merely one of the same name. For the caller's own
      // job that is (account, name). For an external one (found by a
      // machine-wide ps scan, so it may belong to ANY account -- an operator
      // sees the seed of the tenant they are working on) it is the row whose
      // pid the scan also found, or, for a row that never recorded one, the
      // name. Matching only the caller's own account left an operator seeing
      // one process twice: once from the scan, once from its admission row.
      const sameJob = (r: (typeof activeJobs)[number]) =>
        !!job?.running && r.job_name === job.name && (
          r.account_id === myAccountId
          || (!!job.external
              && (r.pid == null || (job.pids ?? [job.pid]).includes(r.pid))))
      const admission = activeJobs.find(sameJob)
      // A running job with no admission row at all is still a running job.
      // The row is gone whenever the process outlived the restart that
      // forgot it, and requiring one meant the page showed nothing for a
      // job the server could see and name -- which is the single thing
      // this page exists to answer. It is labelled for what it is instead.
      const jobIsMine = !!job?.running && !!job.name
      if (jobIsMine && job) {
        const kind = jobKind(job.name)
        // A job that names its side acts on that side: "reset target", "wipe target
        // data", "delete all target users". Resets alone used to be read this way, so
        // deleting target2's users was shown against the source domain.
        const jobDomain = kind !== 'migrate' && /\btarget\b/i.test(job.name)
          ? tgtCfg?.domain
          // A migration (or delta) reads the source and writes the target -- naming only
          // one tenant left "where is this going?" answered nowhere on the card or the
          // detail view it opens onto.
          : kind === 'migrate' && srcCfg?.domain && tgtCfg?.domain
            ? `${srcCfg.domain} → ${tgtCfg.domain}`
            : srcCfg?.domain
        // A helper node running seed_sandbox.py directly reports its own
        // seed_domain via fleet_agent.py's --seed-log flag -- matched here
        // by domain, not job name, since the helper's own label is
        // whatever --seed-domain was given it, not this job's name.
        const seedNodes = kind === 'seed' && jobDomain
          ? nodes.filter((n) => n.seed_domain === jobDomain && n.healthy)
          : undefined
        found.push({
          key: `webui-${job.name}`,
          kind,
          // Every other job acts on one tenant, so naming it is the whole answer; a
          // migration or delta reads one and writes the other, so jobDomain above
          // already carries both.
          domain: jobDomain,
          label: job.name,
          // "1928s elapsed" was the whole description of a 32-minute run.
          // The job's own newest line says what it is doing right now --
          // which user, how many items -- and that is the thing an operator
          // is actually looking for when they open this page.
          detail: [
            describeElapsed(job.elapsed),
            job.etaSeconds ? `~${describeElapsed(job.etaSeconds)} left` : null,
            job.external && !admission
              ? 'detached — outlived the restart that started it'
              : job.external && admission && admission.account_id !== myAccountId
                ? `account #${admission.account_id ?? 'legacy'}`
                : null,
            latestLine(job.lines),
          ].filter(Boolean).join(' · '),
          pct: job.progressPct ?? null, lines: job.lines, elapsedSec: job.elapsed,
          nodes: seedNodes,
          // Its own admission row: a Stop already sent from anywhere makes the next one force.
          stopAsked: !!admission?.stop_asked_at,
          stop: async (_reason, force) => {
            await stopSeedJob(undefined, force, job.external ? job.pid ?? undefined : undefined)
          },
        })
      }
      // healthy, or the claim is as old as the heartbeat that made it. A
      // node that stopped reporting kept its last active_job forever, so
      // this page listed a migration that finished hours earlier -- with a
      // Stop button for a pid that no longer exists.
      //
      // Not the same job as the webui entry above, though: fleet_agent.py's ps scan runs
      // on every node, INCLUDING this one, so a migrate this account started (found via
      // fetchJob, already pushed above with its own rich detail) is also found here, by
      // the same scan, as a second card for the identical pid. Skipped by pid, not by
      // kind or domain -- a genuinely different node running a genuinely different
      // migration must still show.
      const myPids = jobIsMine && job ? (job.pids ?? (job.pid != null ? [job.pid] : [])) : []
      const fleet = nodes.find((n) => n.active_job && n.job_pid && n.healthy
                                     && !myPids.includes(n.job_pid))
      if (fleet) {
        found.push({
          key: `fleet-${fleet.job_pid}`, kind: jobKind(fleet.active_job!),
          domain: srcCfg?.domain, label: fleet.active_job!,
          detail: `pid ${fleet.job_pid} on ${fleet.hostname || fleet.node_id}`, pct: null,
          stop: async (reason, force) => {
            const r = await stopFleetJob(fleet.job_pid!, reason, force)
            if (!r.ok) throw new Error(r.detail || 'could not stop')
          },
        })
      }

      // Everything above only ever sees the VIEWER's own account (webui.py's
      // per-account Job, full-setup/status's own ps scan) -- migrate is the
      // one exception, already caught above regardless of account since
      // fleet_agent.py's scan isn't account-scoped at all. So a seed/reset/
      // full-setup job admitted under a DIFFERENT account -- exactly what a
      // "capacity is full" refusal is usually caused by -- was invisible
      // here entirely. job_admission's own table has no such blind spot.
      for (const row of activeJobs) {
        if (!ACCOUNT_SCOPED_JOB_NAMES.has(row.job_name)) continue
        // Skip only what was ACTUALLY rendered above, not everything
        // belonging to this account -- see jobIsMine's own comment.
        if (jobIsMine && sameJob(row)) continue
        found.push({
          key: `admission-${row.account_id}-${row.job_name}`,
          kind: jobKind(row.job_name),
          label: row.job_name,
          detail: `account #${row.account_id ?? 'legacy'} -- started ${new Date(row.started_at).toLocaleTimeString()}`,
          pct: null,
          // No stop: nothing here records a pid for another account's job,
          // and stopping someone else's run isn't this page's call to make.
        })
      }
      // Provisioning, which registers nowhere else.
      //
      // It deliberately does not go through job_admission -- that gate holds
      // ONE heavy job across the whole box, and creating a handful of
      // accounts must not block a migration from starting. But "not a heavy
      // job" was silently taken to mean "not worth showing", so a run that
      // was genuinely in flight appeared on no page at all. This page's job
      // is to answer "what is happening right now", and the honest answer
      // includes this.
      for (const [tenant, st] of provisions) {
        if (!st?.running) continue
        found.push({
          key: `provision-${tenant}`,
          kind: 'provision',
          domain: tenant === 'target' ? tgtCfg?.domain : srcCfg?.domain,
          label: `provision users — ${tenant}`,
          detail: `${st.created} of ${st.total} created`
            + (st.existing ? `, ${st.existing} already existed` : '')
            + (st.failed ? `, ${st.failed} failed` : ''),
          pct: st.total ? Math.round(100 * (st.created + (st.existing ?? 0)) / st.total) : null,
          stop: async (reason, force) => {
            if (!st.pid) throw new Error('no pid recorded for this run yet -- try again shortly')
            const r = await stopFleetJob(st.pid, reason, force)
            if (!r.ok) throw new Error(r.detail || 'could not stop')
          },
        })
      }

      // A Stop already sent, by any page or browser, is on the job's admission row
      // (job_admission.note_stop): the next press forces, and the card says so.
      const asked = new Set(activeJobs.filter((a) => a.stop_asked_at && a.pid != null).map((a) => a.pid))
      for (const j of found) {
        const pid = Number((j.key.match(/(\d+)$/) || [])[1])
        if (pid && asked.has(pid)) j.stopAsked = true
      }
      setJobs(found)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    refresh()
    const id = setInterval(refresh, 5000)
    return () => clearInterval(id)
  }, [refresh])

  return { jobs, loading, refresh }
}
