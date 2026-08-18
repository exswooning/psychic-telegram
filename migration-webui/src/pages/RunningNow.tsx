import React, { useCallback, useEffect, useState } from 'react'
import {
  Box, Typography, Card, CardContent, Stack, Chip, IconButton, Tooltip,
  LinearProgress, CircularProgress, Button,
} from '@mui/material'
import {
  Refresh as RefreshIcon, Bolt as RunningNowIcon, Stop as StopIcon,
} from '@mui/icons-material'
import {
  fetchTenantConfigStatus, fetchFullSetupStatus, fetchFleet, FleetNode,
  fetchActiveJobs, fetchMe, stopJob as stopFleetJob,
} from '@/api/controlPlane'
import { fetchJob, stopJob as stopSeedJob } from '@/api/client'
import ReasonCodeDialog from '@/components/ReasonCodeDialog'

interface RunningJob {
  key: string; label: string; detail: string; pct: number | null
  // Only ever populated for the webui.py Job entry (seed/reset target/
  // reset drive ledger) -- that's the one source here with real printed
  // output. full-setup's own progress file only ever carries a
  // pct+label checkpoint, not a transcript; fleet/cross-account entries
  // have no output source at all.
  lines?: string[]
  // Absent for a job admitted under a DIFFERENT account -- job_admission.py
  // never records a stoppable pid for seed/reset-target/full-setup (only
  // this account's own rich sources below know that), and stopping
  // someone else's job from here isn't a call this page should make anyway.
  stop?: (reason: string) => Promise<void>
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
function useRunningNow() {
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
      const myAccountId = me?.id ?? null
      const found: RunningJob[] = []
      if (srcSetup?.running) {
        found.push({
          key: 'setup-source', label: srcCfg?.domain || 'source',
          detail: srcSetup.progressLabel || 'setting up…', pct: srcSetup.progressPct ?? null,
          stop: async (reason) => {
            if (!srcSetup.pid) throw new Error('no pid recorded for this run yet -- try again shortly')
            const r = await stopFleetJob(srcSetup.pid, reason)
            if (!r.ok) throw new Error(r.detail || 'could not stop')
          },
        })
      }
      if (tgtSetup?.running) {
        found.push({
          key: 'setup-target', label: tgtCfg?.domain || 'target',
          detail: tgtSetup.progressLabel || 'setting up…', pct: tgtSetup.progressPct ?? null,
          stop: async (reason) => {
            if (!tgtSetup.pid) throw new Error('no pid recorded for this run yet -- try again shortly')
            const r = await stopFleetJob(tgtSetup.pid, reason)
            if (!r.ok) throw new Error(r.detail || 'could not stop')
          },
        })
      }
      if (job?.running && job.name) {
        found.push({
          key: `webui-${job.name}`, label: job.name, detail: `${job.elapsed}s elapsed`,
          pct: job.progressPct ?? null, lines: job.lines,
          stop: async () => { await stopSeedJob() },
        })
      }
      const fleet = nodes.find((n) => n.active_job && n.job_pid)
      if (fleet) {
        found.push({
          key: `fleet-${fleet.job_pid}`, label: fleet.active_job!,
          detail: `pid ${fleet.job_pid} on ${fleet.hostname || fleet.node_id}`, pct: null,
          stop: async (reason) => {
            const r = await stopFleetJob(fleet.job_pid!, reason)
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
        if (row.account_id === myAccountId) continue   // already shown above, richly
        found.push({
          key: `admission-${row.account_id}-${row.job_name}`,
          label: row.job_name,
          detail: `account #${row.account_id ?? 'legacy'} -- started ${new Date(row.started_at).toLocaleTimeString()}`,
          pct: null,
          // No stop: nothing here records a pid for another account's job,
          // and stopping someone else's run isn't this page's call to make.
        })
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

const RunningNow: React.FC = () => {
  const { jobs, loading, refresh } = useRunningNow()
  const [stopTarget, setStopTarget] = useState<RunningJob | null>(null)
  const [stopBusy, setStopBusy] = useState(false)
  const [stopError, setStopError] = useState<string | null>(null)

  const runStop = async (reason: string) => {
    if (!stopTarget?.stop) return
    setStopBusy(true); setStopError(null)
    try {
      await stopTarget.stop(reason)
      setStopTarget(null)
      refresh()
    } catch (e: any) {
      setStopError(e.message)
    } finally {
      setStopBusy(false)
    }
  }

  return (
    <Box>
      <Stack direction="row" alignItems="center" sx={{ mb: 0.5 }}>
        <Typography variant="h4" sx={{ fontWeight: 700, flexGrow: 1 }}>Running Now</Typography>
        <Tooltip title="Refresh">
          <span>
            <IconButton size="small" onClick={refresh} disabled={loading}>
              <RefreshIcon fontSize="small" />
            </IconButton>
          </span>
        </Tooltip>
      </Stack>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        Only one heavy job (seed, migrate, delta, full-setup) runs at a time
        across this box -- if starting something new says "capacity is full,
        try again shortly", it's because of whatever's listed here.
      </Typography>

      {loading && jobs.length === 0 && (
        <Box sx={{ display: 'flex', justifyContent: 'center', py: 6 }}>
          <CircularProgress size={28} />
        </Box>
      )}

      {!loading && jobs.length === 0 && (
        <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider' }}>
          <CardContent sx={{ textAlign: 'center', py: 5 }}>
            <RunningNowIcon color="disabled" sx={{ fontSize: 32, mb: 1 }} />
            <Typography variant="body2" color="text.secondary">
              Nothing is running right now -- the capacity slot is free.
            </Typography>
          </CardContent>
        </Card>
      )}

      <Stack spacing={1.5}>
        {jobs.map((j) => (
          <Card key={j.key} elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider' }}>
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, p: 2, flexWrap: 'wrap' }}>
              <CircularProgress size={16} />
              <Box sx={{ flexGrow: 1, minWidth: 160 }}>
                <Stack direction="row" spacing={1} alignItems="center">
                  <Typography variant="body1" sx={{ fontWeight: 600 }}>{j.label}</Typography>
                  <Chip size="small" label="running" color="info" variant="outlined" />
                </Stack>
                <Typography variant="caption" color="text.secondary">
                  {j.detail}{typeof j.pct === 'number' && ` — ${j.pct}%`}
                </Typography>
              </Box>
              {j.stop && (
                <Button size="small" color="error" startIcon={<StopIcon />}
                        onClick={() => setStopTarget(j)}>
                  Stop
                </Button>
              )}
            </Box>
            {typeof j.pct === 'number' && (
              <Box sx={{ px: 2, pb: j.lines?.length ? 1 : 2 }}>
                <LinearProgress variant="determinate" value={j.pct} sx={{ height: 6, borderRadius: 3 }} />
              </Box>
            )}
            {j.lines && j.lines.length > 0 && (
              <Box sx={{ px: 2, pb: 2 }}>
                <Box component="pre" sx={{
                  fontSize: 11, p: 1.5, bgcolor: 'action.hover', borderRadius: 1,
                  overflowX: 'auto', maxHeight: 260, whiteSpace: 'pre-wrap', m: 0,
                }}>
                  {j.lines.join('\n')}
                </Box>
              </Box>
            )}
          </Card>
        ))}
      </Stack>

      <ReasonCodeDialog
        open={!!stopTarget} busy={stopBusy} error={stopError} destructive
        title={`Stop ${stopTarget?.label ?? ''}`}
        description={
          <>Sends <strong>SIGINT</strong> to the running process. It finishes the item
          in flight and commits, so nothing already done is lost -- this is a pause,
          not a rollback.</>
        }
        onCancel={() => { setStopTarget(null); setStopError(null) }}
        onConfirm={runStop}
      />
    </Box>
  )
}

export default RunningNow
