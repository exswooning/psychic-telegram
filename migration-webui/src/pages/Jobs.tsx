import React, { useCallback, useEffect, useState } from 'react'
import {
  Box, Typography, Card, CardContent, Stack, Chip, IconButton, Tooltip,
  Collapse, LinearProgress, Divider, CircularProgress, Button, TextField,
  MenuItem, FormControlLabel, FormGroup, Switch, Checkbox, Alert,
} from '@mui/material'
import {
  Refresh as RefreshIcon, ExpandMore as ExpandIcon, Language as DomainIcon,
  Grass as SeedIcon, Key as KeyIcon, VpnKey as ScopeIcon,
  RocketLaunch as MigrateIcon, Science as DryRunIcon,
} from '@mui/icons-material'
import {
  fetchTenantConfigStatus, TenantConfigStatus,
  fetchVerifiedDomains, VerifiedDomain,
  fetchFullSetupStatus, FullSetupStatus,
  fetchDwdStatus,
  fetchMe, startMigration,
} from '@/api/controlPlane'
import { fetchJob, fetchJobHistory, runSeed, JobStatus, JobResult } from '@/api/client'
import ReasonCodeDialog from '@/components/ReasonCodeDialog'

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
  const [expanded, setExpanded] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [seedEnabled, setSeedEnabled] = useState(false)

  useEffect(() => { fetchMe().then((a) => setSeedEnabled(a.seed_enabled)).catch(() => {}) }, [])

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const [srcCfg, tgtCfg, dwd, srcSetup, tgtSetup, job, hist, srcDwdStatus, tgtDwdStatus] = await Promise.all([
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
      setSeedJob(job && job.name === 'seed' ? job : null)
      setSeedHistory(hist)
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
                      onStarted={refresh} />
        ))}

        {(seedJob?.name === 'seed' || seedHistory) && (
          <SeedJobCard job={seedJob} history={seedHistory}
                      open={expanded === 'seed'} onToggle={() => toggle('seed')} />
        )}
      </Stack>
    </Box>
  )
}

const SideJobCard: React.FC<{
  job: SideJob; open: boolean; onToggle: () => void
  seedEnabled: boolean; targetReady: boolean; onStarted: () => void
}> = ({ job, open, onToggle, seedEnabled, targetReady, onStarted }) => {
    const { side, cfg, dwd, setup, health } = job
    const label = cfg?.domain || `${side} (not set up)`

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
            {setup?.running ? (
              <Typography variant="caption" color="text.secondary">
                {setup.progressLabel || 'working…'}
                {typeof setup.progressPct === 'number' && ` — ${setup.progressPct}%`}
              </Typography>
            ) : (
              <Typography variant="caption" color="text.secondary">
                {cfg?.hasKey ? 'SA key on file' : 'no SA key yet'}
                {dwd && ` · ${dwd.live}/${dwd.total} DWD scopes live`}
              </Typography>
            )}
          </Box>
          {setup?.running && <CircularProgress size={16} />}
          <Chip size="small" label={HEALTH_LABEL[health]} color={HEALTH_COLOR[health]}
               variant={health === 'healthy' ? 'filled' : 'outlined'} />
          <ExpandIcon sx={{ transform: open ? 'rotate(180deg)' : 'none', transition: 'transform 0.15s' }} />
        </Box>

        {setup?.running && (
          <Box sx={{ px: 2, pb: open ? 0 : 2 }}>
            {typeof setup.progressPct === 'number' ? (
              <LinearProgress variant="determinate" value={setup.progressPct} sx={{ height: 6, borderRadius: 3 }} />
            ) : (
              <LinearProgress sx={{ height: 6, borderRadius: 3 }} />
            )}
          </Box>
        )}

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
}> = ({ job, history, open, onToggle }) => {
  const running = !!job?.running
  const rc = job?.rc ?? history?.rc ?? null
  const label = running ? 'Running' : rc === 0 ? 'ok' : rc === null ? 'Unknown' : `exit ${rc}`
  const color = running ? 'info' : rc === 0 ? 'success' : rc === null ? 'default' : 'error'
  const lines = (running ? job?.lines : history?.lines) ?? []

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
      <Collapse in={open}>
        <Divider />
        <CardContent sx={{ pt: 2 }}>
          {lines.length > 0 ? (
            <Box component="pre" sx={{
              fontSize: 11, p: 1.5, bgcolor: 'action.hover', borderRadius: 1,
              overflowX: 'auto', maxHeight: 260, whiteSpace: 'pre-wrap', m: 0,
            }}>
              {lines.join('\n')}
            </Box>
          ) : (
            <Typography variant="body2" color="text.secondary">No output recorded.</Typography>
          )}
        </CardContent>
      </Collapse>
    </Card>
  )
}

export default Jobs
