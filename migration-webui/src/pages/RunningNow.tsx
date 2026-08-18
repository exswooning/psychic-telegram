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
  stopJob as stopFleetJob,
} from '@/api/controlPlane'
import { fetchJob, stopJob as stopSeedJob } from '@/api/client'
import ReasonCodeDialog from '@/components/ReasonCodeDialog'

interface RunningJob {
  key: string; label: string; detail: string; pct: number | null
  stop: (reason: string) => Promise<void>
}

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
      const [srcCfg, tgtCfg, srcSetup, tgtSetup, job, nodes] = await Promise.all([
        fetchTenantConfigStatus('source').catch(() => null),
        fetchTenantConfigStatus('target').catch(() => null),
        fetchFullSetupStatus('source').catch(() => null),
        fetchFullSetupStatus('target').catch(() => null),
        fetchJob(0).catch(() => null),
        fetchFleet().catch(() => [] as FleetNode[]),
      ])
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
          pct: job.progressPct ?? null,
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
    if (!stopTarget) return
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
              <Button size="small" color="error" startIcon={<StopIcon />}
                      onClick={() => setStopTarget(j)}>
                Stop
              </Button>
            </Box>
            {typeof j.pct === 'number' && (
              <Box sx={{ px: 2, pb: 2 }}>
                <LinearProgress variant="determinate" value={j.pct} sx={{ height: 6, borderRadius: 3 }} />
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
