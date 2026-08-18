import React, { useCallback, useEffect, useState } from 'react'
import {
  Box, Typography, Card, CardContent, Stack, Chip, IconButton, Tooltip,
  Collapse, LinearProgress, Divider, CircularProgress, Button, TextField,
  MenuItem, FormControlLabel, FormGroup, Switch, Checkbox, Alert,
} from '@mui/material'
import {
  Refresh as RefreshIcon, ExpandMore as ExpandIcon, Language as DomainIcon,
  Grass as SeedIcon, Key as KeyIcon, VpnKey as ScopeIcon,
  RocketLaunch as MigrateIcon, Science as DryRunIcon, Stop as StopIcon,
} from '@mui/icons-material'
import {
  fetchTenantConfigStatus, TenantConfigStatus,
  fetchVerifiedDomains, VerifiedDomain,
  fetchFullSetupStatus, FullSetupStatus,
  fetchDwdStatus, fetchFleet, FleetNode,
  fetchMe, startMigration, stopJob as stopFleetJob,
} from '@/api/controlPlane'
import {
  fetchJob, fetchJobHistory, runSeed, JobStatus, JobResult,
  stopJob as stopSeedJob,
} from '@/api/client'
import ReasonCodeDialog from '@/components/ReasonCodeDialog'
import SeedRunDashboard from '@/components/SeedRunDashboard'

const SEED_SCALES = ['tiny', 'small', 'medium', 'large', 'huge']
// main.py migrate --services help text is the source of truth: "drive,
// gmail,calendar,chat,contacts,tasks -- or 'all' for every per-user
// service." CLI default is drive,gmail,calendar.
const MIGRATE_SERVICES = ['drive', 'gmail', 'calendar', 'chat', 'contacts', 'tasks']
const DEFAULT_MIGRATE_SERVICES = ['drive', 'gmail', 'calendar']

type Health = 'running' | 'healthy' | 'propagating' | 'attention' | 'not_set_up' | 'unknown'

const HEALTH_LABEL: Record<Health, string> = {
  running: 'Running', healthy: 'Healthy', propagating: 'Propagating',
  attention: 'Needs attention', not_set_up: 'Not set up', unknown: 'Unknown',
}
const HEALTH_COLOR: Record<Health, 'success' | 'warning' | 'error' | 'info' | 'default'> = {
  running: 'info', healthy: 'success', propagating: 'warning',
  attention: 'error', not_set_up: 'default', unknown: 'default',
}

interface SideJob {
  side: 'source' | 'target'
  cfg: TenantConfigStatus | null
  dwd: VerifiedDomain | null
  setup: FullSetupStatus | null
  health: Health
  caveats: { api: string; note: string }[]
}

function deriveHealth(cfg: TenantConfigStatus | null, dwd: VerifiedDomain | null,
                      setup: FullSetupStatus | null): Health {
  if (setup?.running) return 'running'
  if (!cfg?.domain) return 'not_set_up'
  if (!cfg?.hasKey) return 'attention'
  if (!dwd) return 'unknown'
  if (dwd.status === 'verified') return 'healthy'
  if (dwd.status === 'pending') return 'propagating'
  if (dwd.status === 'not_set_up') return 'attention'
  return 'attention'
}

/**
 * One glance at every active tenant setup, seed, and delegation job --
 * this tool runs a migration across (at least) two tenants at once, and
 * before this page each one's health lived on a different page (Setup
 * Wizard's own Result box, Verification's Verified Domains, the seed
 * job's own transcript) with nothing tying them together. Reuses the
 * exact same endpoints those already call -- no new backend surface,
 * just one place that composes them.
 */
const Jobs: React.FC = () => {
  const [sides, setSides] = useState<SideJob[] | null>(null)
  const [seedJob, setSeedJob] = useState<JobStatus | null>(null)
  const [seedHistory, setSeedHistory] = useState<JobResult | null>(null)
  // The migrate/delta/discover slot -- fleet_agent.py's own ps scan is what
  // finds this (main.py's pid isn't recorded anywhere else queryable), the
  // exact mechanism Mission Control's JobController already stops jobs
  // through. Only ever one node in this deployment, but the shape is a list.
  const [fleetJob, setFleetJob] = useState<FleetNode | null>(null)
  const [expanded, setExpanded] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [seedEnabled, setSeedEnabled] = useState(false)

  useEffect(() => { fetchMe().then((a) => setSeedEnabled(a.seed_enabled)).catch(() => {}) }, [])

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const [srcCfg, tgtCfg, dwd, srcSetup, tgtSetup, job, hist, srcDwdStatus, tgtDwdStatus, nodes] = await Promise.all([
        fetchTenantConfigStatus('source').catch(() => null),
        fetchTenantConfigStatus('target').catch(() => null),
        fetchVerifiedDomains().catch(() => ({ domains: [] as VerifiedDomain[] })),
        fetchFullSetupStatus('source').catch(() => null),
        fetchFullSetupStatus('target').catch(() => null),
        fetchJob(0).catch(() => null),
        fetchJobHistory('seed').catch(() => null),
        // An API can report ENABLED and still 404 every call -- Chat needs
        // an app configured in the Cloud console, which has no API, so this
        // is the only way to know before a seed/migrate run hits it. Same
        // check DwdSetup.tsx already surfaces in the Wizard; the Jobs page
        // needs its own copy since a seed can also be launched from here.
        fetchDwdStatus('source').catch(() => null),
        fetchDwdStatus('target').catch(() => null),
        fetchFleet().catch(() => [] as FleetNode[]),
      ])
      const byDwd = (side: 'source' | 'target') => dwd.domains.find((d) => d.side === side) ?? null
      setSides([
        { side: 'source', cfg: srcCfg, dwd: byDwd('source'), setup: srcSetup,
         health: deriveHealth(srcCfg, byDwd('source'), srcSetup),
         caveats: srcDwdStatus?.caveats ?? [] },
        { side: 'target', cfg: tgtCfg, dwd: byDwd('target'), setup: tgtSetup,
         health: deriveHealth(tgtCfg, byDwd('target'), tgtSetup),
         caveats: tgtDwdStatus?.caveats ?? [] },
      ])
      // !job.external: a seed job admitted under a DIFFERENT account shows
      // up here identically (same name, no account info) via webui.py's
      // own system-wide ps-scan fallback -- rendering it here too would
      // duplicate the account-attributed cross-account entry the source
      // side's caveats/admission handling already covers, and would offer
      // a Stop button for a job this account did not start.
      setSeedJob(job && job.name === 'seed' && !job.external ? job : null)
      setSeedHistory(hist)
      setFleetJob(nodes.find((n) => n.active_job && n.job_pid) ?? null)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    refresh()
    const id = setInterval(refresh, 5000)
    return () => clearInterval(id)
  }, [refresh])

  const toggle = (key: string) => setExpanded((cur) => (cur === key ? null : key))

  return (
    <Box>
      <Stack direction="row" alignItems="center" sx={{ mb: 0.5 }}>
        <Typography variant="h4" sx={{ fontWeight: 700, flexGrow: 1 }}>Jobs</Typography>
        <Tooltip title="Refresh">
          <span>
            <IconButton size="small" onClick={refresh} disabled={loading}>
              <RefreshIcon fontSize="small" />
            </IconButton>
          </span>
        </Tooltip>
      </Stack>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        Every tenant setup and seed job, in one place. Click a row for the full breakdown.
      </Typography>

      {sides === null && (
        <Box sx={{ display: 'flex', justifyContent: 'center', py: 6 }}>
          <CircularProgress size={28} />
        </Box>
      )}

      <Stack spacing={1.5}>
        {sides?.map((j) => (
          <SideJobCard key={j.side} job={j} open={expanded === j.side}
                      onToggle={() => toggle(j.side)}
                      seedEnabled={seedEnabled}
                      targetReady={sides.find((s) => s.side === 'target')?.cfg?.hasKey ?? false}
                      seedJob={j.side === 'source' ? seedJob : null}
                      fleetJob={j.side === 'source' ? fleetJob : null}
                      onStarted={refresh} />
        ))}

        {(seedJob?.name === 'seed' || seedHistory) && (
          <SeedJobCard job={seedJob} history={seedHistory}
                      open={expanded === 'seed'} onToggle={() => toggle('seed')}
                      onStopped={refresh} />
        )}
      </Stack>
    </Box>
  )
}

// Whatever's running for this side, regardless of which of the three
// separate job systems owns it (full_setup.py's own progress file,
// webui.py's per-account Job, or main.py found live via fleet_agent.py's ps
// scan) -- so the collapsed header never has to be expanded just to learn
// something is in flight, and Stop always has one consistent place to live.
type ActiveRun = {
  kind: 'setup' | 'seed' | 'fleet'; label: string; pct: number | null
  stop: (reason: string) => Promise<void>
}

const SideJobCard: React.FC<{
  job: SideJob; open: boolean; onToggle: () => void
  seedEnabled: boolean; targetReady: boolean
  seedJob: JobStatus | null; fleetJob: FleetNode | null
  onStarted: () => void
}> = ({ job, open, onToggle, seedEnabled, targetReady, seedJob, fleetJob, onStarted }) => {
    const { side, cfg, dwd, setup, health } = job
    const label = cfg?.domain || `${side} (not set up)`
    const [stopAsk, setStopAsk] = useState(false)
    const [stopBusy, setStopBusy] = useState(false)
    const [stopError, setStopError] = useState<string | null>(null)

    const active: ActiveRun | null = setup?.running
      ? {
          kind: 'setup', pct: setup.progressPct ?? null,
          label: setup.progressLabel || 'setting up…',
          stop: async (reason) => {
            if (!setup.pid) throw new Error('no pid recorded for this run yet -- try again shortly')
            const r = await stopFleetJob(setup.pid, reason)
            if (!r.ok) throw new Error(r.detail || 'could not stop')
          },
        }
      : seedJob?.running
      ? {
          kind: 'seed', pct: seedJob.progressPct ?? null, label: 'seeding…',
          stop: async () => { await stopSeedJob() },
        }
      : fleetJob
      ? {
          kind: 'fleet', pct: null, label: `${fleetJob.active_job} running…`,
          stop: async (reason) => {
            const r = await stopFleetJob(fleetJob.job_pid!, reason)
            if (!r.ok) throw new Error(r.detail || 'could not stop')
          },
        }
      : null

    const runStop = async (reason: string) => {
      if (!active) return
      setStopBusy(true); setStopError(null)
      try {
        await active.stop(reason)
        setStopAsk(false)
        onStarted()
      } catch (e: any) {
        setStopError(e.message)
      } finally {
        setStopBusy(false)
      }
    }

    return (
      <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider' }}>
        <Box onClick={onToggle} sx={{
          display: 'flex', alignItems: 'center', gap: 1.5, p: 2, cursor: 'pointer',
          '&:hover': { bgcolor: 'action.hover' },
        }}>
          <DomainIcon color="action" fontSize="small" />
          <Box sx={{ flexGrow: 1, minWidth: 0 }}>
            <Stack direction="row" spacing={1} alignItems="center">
              <Typography variant="body1" sx={{ fontWeight: 600 }} noWrap>{label}</Typography>
              <Chip size="small" label={side} variant="outlined" sx={{ textTransform: 'capitalize' }} />
            </Stack>
            {active ? (
              <Typography variant="caption" color="text.secondary">
                {active.label}{typeof active.pct === 'number' && ` — ${active.pct}%`}
              </Typography>
            ) : (
              <Typography variant="caption" color="text.secondary">
                {cfg?.hasKey ? 'SA key on file' : 'no SA key yet'}
                {dwd && ` · ${dwd.live}/${dwd.total} DWD scopes live`}
              </Typography>
            )}
          </Box>
          {active && <CircularProgress size={16} />}
          {active && (
            <Button size="small" color="error" startIcon={<StopIcon />}
                    onClick={(e) => { e.stopPropagation(); setStopAsk(true) }}>
              Stop
            </Button>
          )}
          <Chip size="small" label={HEALTH_LABEL[health]} color={HEALTH_COLOR[health]}
               variant={health === 'healthy' ? 'filled' : 'outlined'} />
          <ExpandIcon sx={{ transform: open ? 'rotate(180deg)' : 'none', transition: 'transform 0.15s' }} />
        </Box>

        {active && (
          <Box sx={{ px: 2, pb: open ? 0 : 2 }}>
            {typeof active.pct === 'number' ? (
              <LinearProgress variant="determinate" value={active.pct} sx={{ height: 6, borderRadius: 3 }} />
            ) : (
              <LinearProgress sx={{ height: 6, borderRadius: 3 }} />
            )}
          </Box>
        )}

        <ReasonCodeDialog
          open={stopAsk} busy={stopBusy} error={stopError} destructive
          title={active ? `Stop ${active.kind === 'setup' ? 'setup' : active.kind === 'seed' ? 'seeding' : active.label.replace(' running…', '')}` : 'Stop'}
          description={
            <>Sends <strong>SIGINT</strong> to the running process for <strong>{label}</strong>.
            It finishes the item in flight and commits, so nothing already done is lost --
            this is a pause, not a rollback.</>
          }
          onCancel={() => { setStopAsk(false); setStopError(null) }}
          onConfirm={runStop}
        />

        <Collapse in={open}>
          <Divider />
          <CardContent sx={{ pt: 2 }}>
            <Stack spacing={2}>
              <Box>
                <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.5 }}>
                  <KeyIcon fontSize="small" color="action" />
                  <Typography variant="subtitle2" sx={{ fontWeight: 600 }}>Service account</Typography>
                </Stack>
                {cfg?.hasKey ? (
                  <Typography variant="body2" color="text.secondary" sx={{ fontFamily: 'ui-monospace, monospace' }}>
                    client ID {cfg.clientId}
                  </Typography>
                ) : (
                  <Typography variant="body2" color="text.secondary">
                    No key on file yet -- run Setup Wizard for this side.
                  </Typography>
                )}
              </Box>

              <Box>
                <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.5 }}>
                  <ScopeIcon fontSize="small" color="action" />
                  <Typography variant="subtitle2" sx={{ fontWeight: 600 }}>
                    Domain-wide delegation {dwd && `(${dwd.live}/${dwd.total})`}
                  </Typography>
                </Stack>
                <Typography variant="body2" color="text.secondary">
                  {!dwd ? 'Not checked yet.'
                    : dwd.status === 'verified' ? 'All required scopes are live.'
                    : dwd.status === 'pending' ? 'Granted, still propagating on Google\'s side.'
                    : dwd.status === 'not_set_up' ? 'No delegation attempted yet.'
                    : dwd.error || `${dwd.total - dwd.live} scope(s) not live.`}
                </Typography>
                {job.caveats.map((c) => (
                  <Alert key={c.api} severity="warning" sx={{ mt: 1 }}>
                    <strong>{c.api}</strong> is enabled but not yet usable. {c.note}
                  </Alert>
                ))}
              </Box>

              {setup?.result && (
                <Box>
                  <Typography variant="subtitle2" sx={{ fontWeight: 600, mb: 0.5 }}>
                    Last setup run
                    <Chip size="small" sx={{ ml: 1 }} label={setup.result.ok ? 'ok' : 'failed'}
                         color={setup.result.ok ? 'success' : 'error'}
                         variant={setup.result.ok ? 'outlined' : 'filled'} />
                  </Typography>
                  <Box component="pre" sx={{
                    fontSize: 11, p: 1.5, bgcolor: 'action.hover', borderRadius: 1,
                    overflowX: 'auto', maxHeight: 220, whiteSpace: 'pre-wrap', m: 0,
                  }}>
                    {setup.result.phases.map((p) =>
                      `${p.status === 'ok' ? 'ok  ' : p.status === 'failed' ? 'FAIL' : p.status === 'skipped' ? '--  ' : '..  '} `
                      + `${p.name}${p.detail ? '  ' + p.detail : ''}`
                    ).join('\n')}
                  </Box>
                </Box>
              )}

              {side === 'source' && cfg && cfg.hasKey && (
                <>
                  <Divider />
                  {seedEnabled && <SeedPanel domain={cfg.domain} onStarted={onStarted} />}
                  <MigratePanel domain={cfg.domain} targetReady={targetReady} onStarted={onStarted} />
                </>
              )}
            </Stack>
          </CardContent>
        </Collapse>
      </Card>
    )
  }

const SeedPanel: React.FC<{ domain: string; onStarted: () => void }> = ({ domain, onStarted }) => {
  const [scale, setScale] = useState('small')
  const [createUsers, setCreateUsers] = useState(false)
  const [ask, setAsk] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)

  const launch = async () => {
    setBusy(true); setError(null)
    try {
      const r = await runSeed(domain, scale, createUsers, false)
      if (!r.ok) throw new Error(r.error || 'seed failed')
      setDone('Seed started -- see "Seed source tenant" below for live output.')
      setAsk(false)
      onStarted()
    } catch (e: any) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Box>
      <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.5 }}>
        <SeedIcon fontSize="small" color="action" />
        <Typography variant="subtitle2" sx={{ fontWeight: 600 }}>Seed this tenant</Typography>
      </Stack>
      <Stack direction="row" spacing={2} sx={{ flexWrap: 'wrap', gap: 1, alignItems: 'center' }}>
        <TextField select size="small" label="Scale" value={scale}
                   onChange={(e) => setScale(e.target.value)} sx={{ width: 110 }}>
          {SEED_SCALES.map((s) => <MenuItem key={s} value={s}>{s}</MenuItem>)}
        </TextField>
        <FormControlLabel
          control={<Switch checked={createUsers} onChange={(e) => setCreateUsers(e.target.checked)} />}
          label={<Typography variant="body2">Create users</Typography>}
        />
        <Button size="small" variant="contained" startIcon={<SeedIcon />} onClick={() => setAsk(true)}>
          Seed now
        </Button>
      </Stack>
      {done && <Alert severity="success" sx={{ mt: 1 }} onClose={() => setDone(null)}>{done}</Alert>}

      <ReasonCodeDialog
        open={ask} busy={busy} error={error} destructive confirmPhrase="SEED"
        title={`Seed ${domain}`}
        description={
          <>Writes test data into <strong>{domain}</strong> at the <strong>{scale}</strong> scale.
          No password needed -- uses the service account key already on file.</>
        }
        onCancel={() => { setAsk(false); setError(null) }}
        onConfirm={launch}
      />
    </Box>
  )
}

const MigratePanel: React.FC<{ domain: string; targetReady: boolean; onStarted: () => void }> =
  ({ domain, targetReady, onStarted }) => {
    const [services, setServices] = useState<Set<string>>(new Set(DEFAULT_MIGRATE_SERVICES))
    // null = dialog closed; true/false while open carries which mode was asked for.
    const [ask, setAsk] = useState<boolean | null>(null)
    const [busy, setBusy] = useState(false)
    const [error, setError] = useState<string | null>(null)
    const [done, setDone] = useState<string | null>(null)

    const toggleService = (s: string) => setServices((prev) => {
      const next = new Set(prev)
      next.has(s) ? next.delete(s) : next.add(s)
      return next
    })

    const launch = async (reason: string) => {
      setBusy(true); setError(null)
      try {
        const r = await startMigration(reason, Array.from(services), [], ask === true)
        if (!r.ok) throw new Error(r.detail || 'could not start')
        setDone(`${ask ? 'Dry run' : 'Migration'} started -- track live per-user progress on Mission Control.`)
        setAsk(null)
        onStarted()
      } catch (e: any) {
        setError(e.message)
      } finally {
        setBusy(false)
      }
    }

    return (
      <Box>
        <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.5 }}>
          <MigrateIcon fontSize="small" color="action" />
          <Typography variant="subtitle2" sx={{ fontWeight: 600 }}>Migrate</Typography>
        </Stack>
        <FormGroup row sx={{ mb: 0.5 }}>
          {MIGRATE_SERVICES.map((s) => (
            <FormControlLabel key={s}
              control={<Checkbox size="small" checked={services.has(s)} onChange={() => toggleService(s)} />}
              label={<Typography variant="body2" sx={{ textTransform: 'capitalize' }}>{s}</Typography>}
            />
          ))}
        </FormGroup>
        <Stack direction="row" spacing={1} alignItems="center" sx={{ flexWrap: 'wrap', gap: 1 }}>
          <Button size="small" startIcon={<DryRunIcon />} disabled={!services.size || !targetReady}
                  onClick={() => setAsk(true)}>
            Dry run
          </Button>
          <Button size="small" variant="contained" startIcon={<MigrateIcon />}
                  disabled={!services.size || !targetReady} onClick={() => setAsk(false)}>
            Start migration
          </Button>
          {!targetReady && (
            <Typography variant="caption" color="text.secondary">
              Target tenant isn't set up yet.
            </Typography>
          )}
        </Stack>
        {done && <Alert severity="success" sx={{ mt: 1 }} onClose={() => setDone(null)}>{done}</Alert>}

        <ReasonCodeDialog
          open={ask !== null} busy={busy} error={error}
          title={ask ? 'Start dry run' : 'Start migration'}
          description={
            <>
              {ask ? 'Logs every intended write and performs none. '
                : <>Copies real data from <strong>{domain}</strong> into the target tenant,
                  resuming any users already in progress. </>}
              Services: <strong>{Array.from(services).join(', ') || 'none selected'}</strong>.
            </>
          }
          onCancel={() => { setAsk(null); setError(null) }}
          onConfirm={launch}
        />
      </Box>
    )
  }

const SeedJobCard: React.FC<{
  job: JobStatus | null; history: JobResult | null; open: boolean; onToggle: () => void
  onStopped: () => void
}> = ({ job, history, open, onToggle, onStopped }) => {
  const running = !!job?.running
  const rc = job?.rc ?? history?.rc ?? null
  const label = running ? 'Running' : rc === 0 ? 'ok' : rc === null ? 'Unknown' : `exit ${rc}`
  const color = running ? 'info' : rc === 0 ? 'success' : rc === null ? 'default' : 'error'
  const lines = (running ? job?.lines : history?.lines) ?? []
  const [stopAsk, setStopAsk] = useState(false)
  const [stopBusy, setStopBusy] = useState(false)
  const [stopError, setStopError] = useState<string | null>(null)

  const runStop = async () => {
    setStopBusy(true); setStopError(null)
    try {
      await stopSeedJob()
      setStopAsk(false)
      onStopped()
    } catch (e: any) {
      setStopError(e.message)
    } finally {
      setStopBusy(false)
    }
  }

  return (
    <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider' }}>
      <Box onClick={onToggle} sx={{
        display: 'flex', alignItems: 'center', gap: 1.5, p: 2, cursor: 'pointer',
        '&:hover': { bgcolor: 'action.hover' },
      }}>
        <SeedIcon color="action" fontSize="small" />
        <Box sx={{ flexGrow: 1 }}>
          <Typography variant="body1" sx={{ fontWeight: 600 }}>Seed source tenant</Typography>
          <Typography variant="caption" color="text.secondary">
            {running ? `${job?.elapsed ?? 0}s elapsed` : history
              ? `${new Date(history.finished * 1000).toLocaleString()} · ${history.elapsed}s` : 'no run yet'}
          </Typography>
        </Box>
        {running && <CircularProgress size={16} />}
        {running && (
          <Button size="small" color="error" startIcon={<StopIcon />}
                  onClick={(e) => { e.stopPropagation(); setStopAsk(true) }}>
            Stop
          </Button>
        )}
        <Chip size="small" label={label} color={color as any}
             variant={label === 'ok' ? 'filled' : 'outlined'} />
        <ExpandIcon sx={{ transform: open ? 'rotate(180deg)' : 'none', transition: 'transform 0.15s' }} />
      </Box>
      {running && (
        <Box sx={{ px: 2, pb: open ? 0 : 2 }}>
          {typeof job?.progressPct === 'number' ? (
            <LinearProgress variant="determinate" value={job.progressPct} sx={{ height: 6, borderRadius: 3 }} />
          ) : (
            <LinearProgress sx={{ height: 6, borderRadius: 3 }} />
          )}
        </Box>
      )}

      <ReasonCodeDialog
        open={stopAsk} busy={stopBusy} error={stopError} destructive
        title="Stop seeding"
        description={
          <>Stops the seed run against the source tenant. Users and data already
          written stay -- this only stops writing more.</>
        }
        onCancel={() => { setStopAsk(false); setStopError(null) }}
        onConfirm={runStop}
      />

      <Collapse in={open}>
        <Divider />
        <CardContent sx={{ pt: 2 }}>
          {lines.length > 0 ? (
            <>
              <SeedRunDashboard lines={lines} elapsedSec={job?.elapsed ?? history?.elapsed} />
              <Box component="pre" sx={{
                fontSize: 11, p: 1.5, bgcolor: 'action.hover', borderRadius: 1,
                overflowX: 'auto', maxHeight: 260, whiteSpace: 'pre-wrap', m: 0, mt: 1.5,
              }}>
                {lines.join('\n')}
              </Box>
            </>
          ) : (
            <Typography variant="body2" color="text.secondary">No output recorded.</Typography>
          )}
        </CardContent>
      </Collapse>
    </Card>
  )
}

export default Jobs
