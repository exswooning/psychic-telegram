import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
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
  fetchDwdStatus, fetchFleet, FleetNode, fetchActiveJobs,
  fetchMe, startMigration, stopJob as stopFleetJob,
} from '@/api/controlPlane'
import {
  fetchJob, fetchJobHistory, fetchCompletedJobs, runSeed, fetchSeedScopes,
  JobStatus, JobResult,
  stopJob as stopSeedJob,
} from '@/api/client'
import type { CompletedJob } from '@/api/client'
import ReasonCodeDialog from '@/components/ReasonCodeDialog'
import RunningJobCard from '@/components/RunningJobCard'
import RunningJobDetail from '@/components/RunningJobDetail'
import { useRunningJobs, jobKind } from '@/hooks/useRunningJobs'
import type { RunningJob } from '@/hooks/useRunningJobs'
import SeedRunDashboard from '@/components/SeedRunDashboard'
import { groupRunsByDomain } from '@/utils/groupRuns'

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

// The cheap poll: job state, tenant config, full-setup progress. Every
// endpoint behind it answers in well under a second.
const POLL_MS = 5000
// The expensive one -- see refreshScopes for why it cannot share the above.
const SCOPE_POLL_MS = 60000

type Caveat = { api: string; note: string }

/** Delegation state, which the fast poll deliberately does not carry. */
interface ScopeState {
  domains: VerifiedDomain[]
  srcCaveats: Caveat[]
  tgtCaveats: Caveat[]
}

/** What the fast poll alone can establish. */
interface RawSide {
  side: 'source' | 'target'
  cfg: TenantConfigStatus | null
  setup: FullSetupStatus | null
}

interface SideJob extends RawSide {
  dwd: VerifiedDomain | null
  health: Health
  caveats: Caveat[]
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
  // Every running job, from every source -- webui's own Job, the fleet,
  // job_admission rows, full-setup and provisioning. One hook, so this page
  // and nothing else has to know where a job can come from.
  const { jobs: running } = useRunningJobs()
  const [stopping, setStopping] = useState<RunningJob | null>(null)
  const [detail, setDetail] = useState<RunningJob | null>(null)
  // Completed runs, read off disk. A Job lives in this process's memory
  // only, so every deploy loses it -- the transcripts survive precisely so
  // this page does not have to.
  const [done, setDone] = useState<CompletedJob[]>([])
  useEffect(() => {
    fetchCompletedJobs().then(setDone).catch(() => setDone([]))
  }, [running.length])
  const [openDone, setOpenDone] = useState<RunningJob | null>(null)
  const [rawSides, setSides] = useState<RawSide[] | null>(null)
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
  // Delegation state, polled on its own much slower clock -- see SCOPE_POLL_MS.
  const [scopes, setScopes] = useState<ScopeState>(
    { domains: [], srcCaveats: [], tgtCaveats: [] })
  // Guards against a slow round overlapping itself. Deliberately a ref, not
  // state: it must be readable and writable inside the same tick without
  // scheduling a render.
  const busy = useRef(false)

  useEffect(() => { fetchMe().then((a) => setSeedEnabled(a.seed_enabled)).catch(() => {}) }, [])

  const refresh = useCallback(async () => {
    // Without this, a round slower than POLL_MS overlaps its own successor
    // and the overlap compounds: the interval keeps firing regardless of
    // whether the last one came back.
    if (busy.current) return
    busy.current = true
    setLoading(true)
    try {
      const [srcCfg, tgtCfg, srcSetup, tgtSetup, job, hist, nodes, activeJobs, me] = await Promise.all([
        fetchTenantConfigStatus('source').catch(() => null),
        fetchTenantConfigStatus('target').catch(() => null),
        fetchFullSetupStatus('source').catch(() => null),
        fetchFullSetupStatus('target').catch(() => null),
        fetchJob(0).catch(() => null),
        fetchJobHistory('seed').catch(() => null),
        fetchFleet().catch(() => [] as FleetNode[]),
        fetchActiveJobs().catch(() => []),
        fetchMe().catch(() => null),
      ])
      setSides([
        { side: 'source', cfg: srcCfg, setup: srcSetup },
        { side: 'target', cfg: tgtCfg, setup: tgtSetup },
      ])
      // `external` means "not in this server process's memory", NOT "not
      // mine": every webui restart drops the in-memory Job while the child
      // keeps running (KillMode=process), so an account's own seed reports
      // external within minutes. job_admission is the real ownership
      // record -- without consulting it, the account that started the seed
      // saw no seed card at all.
      const mine = job?.name
        ? (activeJobs as { account_id: number | null; job_name: string }[])
            .some((r) => r.account_id === (me?.id ?? null) && r.job_name === job.name)
        : false
      setSeedJob(job && job.name === 'seed' && (!job.external || mine) ? job : null)
      setSeedHistory(hist)
      // A node reports its own active job, so the report is only worth as
      // much as the node's liveness. This box carried active_job
      // "--account-id" against a dead pid with a 43-HOUR-old heartbeat, and
      // healthy:false on the record itself -- and the card rendered a
      // spinner, an indeterminate progress bar and a working Stop button
      // over the top of it, four hours after everything had finished. The
      // server already computes `healthy` from last_seen for exactly this
      // reason; not consulting it was the whole bug.
      setFleetJob(nodes.find(fleetJobIsLive) ?? null)
    } finally {
      setLoading(false)
      busy.current = false
    }
  }, [])

  // Delegation is verified FUNCTIONALLY -- Google offers no way to read a
  // delegation entry back, so each of these mints a real token per scope.
  // Measured against this deployment: verified-domains 29.2s, dwd/status
  // source 18.2s, target 14.3s. On the old 5s loop that meant roughly six
  // rounds in flight at once, permanently, each holding three live Google
  // round-trips; first paint was 12-30s of blank spinner and `loading`
  // never cleared, so Refresh sat disabled forever. A finished 5.5-hour,
  // 200-user seed read as "nothing here". Delegation changes when somebody
  // edits it in the Admin Console, not second to second -- a minute is far
  // more resolution than it needs.
  const refreshScopes = useCallback(async () => {
    const [dwd, srcDwdStatus, tgtDwdStatus] = await Promise.all([
      fetchVerifiedDomains().catch(() => ({ domains: [] as VerifiedDomain[] })),
      // An API can report ENABLED and still 404 every call -- Chat needs an
      // app configured in the Cloud console, which has no API, so this is
      // the only way to know before a seed/migrate run hits it.
      fetchDwdStatus('source').catch(() => null),
      fetchDwdStatus('target').catch(() => null),
    ])
    setScopes({
      domains: dwd.domains,
      srcCaveats: srcDwdStatus?.caveats ?? [],
      tgtCaveats: tgtDwdStatus?.caveats ?? [],
    })
  }, [])

  useEffect(() => {
    refresh()
    const id = setInterval(refresh, POLL_MS)
    return () => clearInterval(id)
  }, [refresh])

  useEffect(() => {
    refreshScopes()
    const id = setInterval(refreshScopes, SCOPE_POLL_MS)
    return () => clearInterval(id)
  }, [refreshScopes])

  // Composed from both clocks. Until the slow one has answered once, dwd is
  // null and deriveHealth reports 'unknown' -- which is the truth at that
  // moment, and is shown rather than withholding the whole page behind it.
  const sides: SideJob[] | null = useMemo(() => rawSides?.map((r) => {
    const dwd = scopes.domains.find((d) => d.side === r.side) ?? null
    return {
      ...r,
      dwd,
      health: deriveHealth(r.cfg, dwd, r.setup),
      caveats: r.side === 'source' ? scopes.srcCaveats : scopes.tgtCaveats,
    }
  }) ?? null, [rawSides, scopes])

  const toggle = (key: string) => setExpanded((cur) => (cur === key ? null : key))

  const [openDomain, setOpenDomain] = useState<string | null>(null)

  // Grouped by the tenant it happened to. The rule lives in
  // groupRunsByDomain so its test exercises that code rather than a copy.
  const byDomain = useMemo(() => groupRunsByDomain(
    done,
    sides?.find((x) => x.side === 'source')?.cfg?.domain,
    sides?.find((x) => x.side === 'target')?.cfg?.domain,
  ), [done, sides])

  // One finished run as a card. Extracted because it is rendered from
  // inside a per-domain group now, and inlining it there put the whole
  // thing three levels deep in a map inside a map.
  const renderDoneCard = (d: CompletedJob) => {
    // The id, when there is one: two wipes of the same tenant are two runs,
    // and keying on the name alone made React render one and silently drop
    // the other.
    const key = d.runId || `name-${d.name}`
    return (
      <RunningJobCard
        key={key}
        job={{
          key: `done-${key}`, kind: jobKind(d.name), label: d.name,
          detail: `${d.lineCount.toLocaleString()} line(s) of output`
            + (d.fromTranscript ? ' — read from the transcript' : ''),
          pct: null, elapsedSec: d.elapsed,
        }}
        finished={{ rc: d.rc, when: d.finished }}
        onOpen={async () => {
          // The lines are fetched only when one is opened: most never are,
          // and a finished seed carries thousands.
          const full = await fetchJobHistory(d.name, d.runId).catch(() => null)
          setOpenDone({
            key: `done-${key}`, kind: jobKind(d.name), label: d.name,
            detail: `exit ${d.rc ?? '?'} — ${d.lineCount.toLocaleString()} line(s)`,
            pct: null, elapsedSec: d.elapsed,
            done: true, finishedAt: d.finished, rc: d.rc,
            lines: full?.lines ?? [],
          })
        }}
      />
    )
  }

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
        Every job in one place — seeds, migrations, setups and resets.
        Anything currently running is at the top; click a row below for the
        full breakdown of a tenant.
      </Typography>

      {/* Running Now used to be a separate page, so a job started here was
          watched somewhere else. It is a label on these cards now. */}
      {running.length > 0 && (
        <Box sx={{ mb: 3 }} data-testid="running-jobs">
          <Typography variant="overline" color="text.secondary">
            Running now
          </Typography>
          <Stack direction="row" flexWrap="wrap" gap={1.5} sx={{ mt: 0.5 }}>
            {running.map((j) => (
              <RunningJobCard
                key={j.key} job={j}
                onOpen={() => setDetail(j)}
                action={j.stop ? (
                  <Tooltip title="Stop this job">
                    <span>
                      <IconButton size="small" color="error"
                                  data-testid={`stop-${j.key}`}
                                  onClick={() => setStopping(j)}>
                        <StopIcon fontSize="small" />
                      </IconButton>
                    </span>
                  </Tooltip>
                ) : undefined}
              />
            ))}
          </Stack>
        </Box>
      )}

      {sides === null && (
        <Box sx={{ display: 'flex', justifyContent: 'center', py: 6 }}>
          <CircularProgress size={28} />
        </Box>
      )}

      {/* Kept fresh while open: the hook repolls every 5s, and a detail
          view frozen at the moment it was opened is worse than none. */}
      <RunningJobDetail
        job={detail ? (running.find((r) => r.key === detail.key) ?? detail) : null}
        onClose={() => setDetail(null)} />

      {/* A finished run's detail is fixed, so it is not re-read from the
          live list the way a running one is. */}
      <RunningJobDetail job={openDone} onClose={() => setOpenDone(null)} />

      <ReasonCodeDialog
        open={!!stopping}
        title={stopping ? `Stop ${stopping.label}` : ''}
        description={<>This ends the running job. Work already done is kept —
          seeds and migrations are resumable — but anything in flight stops
          where it is.</>}
        onCancel={() => setStopping(null)}
        onConfirm={async (reason) => {
          const j = stopping
          setStopping(null)
          if (j?.stop) await j.stop(reason)
        }}
      />

      {done.length > 0 && (
        <Box sx={{ mb: 3 }} data-testid="completed-jobs">
          <Stack direction="row" alignItems="baseline" gap={1}>
            <Typography variant="overline" color="text.secondary">
              Finished runs
            </Typography>
            <Typography variant="caption" color="text.disabled">
              {done.length.toLocaleString()} recorded across{' '}
              {byDomain.length} tenant{byDomain.length === 1 ? '' : 's'}
            </Typography>
          </Stack>

          {/* Grouped by the tenant it happened TO, because that is the
              question being asked of this list -- "what has been done to
              this domain" -- and a flat newest-first list answers it only
              by reading every card. One row per tenant, expanded on click. */}
          <Stack spacing={1} sx={{ mt: 0.5 }}>
            {byDomain.map(({ domain, runs }) => {
              const open = openDomain === domain
              const newest = runs[0]
              return (
                <Card key={domain} variant="outlined"
                      data-testid={`domain-runs-${domain}`}>
                  <CardContent sx={{ py: 1.25, '&:last-child': { pb: 1.25 } }}>
                    <Stack direction="row" alignItems="center" spacing={1}
                           sx={{ cursor: 'pointer' }}
                           onClick={() => setOpenDomain(open ? null : domain)}>
                      <DomainIcon fontSize="small" color="action" />
                      <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>
                        {domain}
                      </Typography>
                      <Chip size="small" variant="outlined"
                            label={`${runs.length} run${runs.length === 1 ? '' : 's'}`} />
                      <Typography variant="caption" color="text.secondary"
                                  sx={{ flexGrow: 1 }}>
                        {newest?.finished
                          ? `last: ${newest.name} · ${new Date(newest.finished * 1000).toLocaleString()}`
                          : `last: ${newest?.name ?? '--'}`}
                      </Typography>
                      <ExpandIcon fontSize="small"
                                  sx={{ transform: open ? 'rotate(180deg)' : 'none',
                                        transition: 'transform .15s' }} />
                    </Stack>
                    <Collapse in={open}>
                      <Stack direction="row" flexWrap="wrap" gap={1.5}
                             sx={{ mt: 1.5 }}>
                        {runs.map((d) => renderDoneCard(d))}
                      </Stack>
                    </Collapse>
                  </CardContent>
                </Card>
              )
            })}
          </Stack>
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
// fleet_agent.py posts on a 30s interval, so a node that has not been heard
// from in several intervals is not reporting -- whatever it last said about
// a running job is a stale claim, not an observation.
const HEARTBEAT_STALE_AFTER_S = 150

function fleetJobIsLive(n: FleetNode): boolean {
  if (!n.active_job || !n.job_pid) return false
  if (!n.healthy) return false
  // null means the server could not derive an age; treat unknown as stale
  // rather than live -- the failure mode of the opposite default is a Stop
  // button pointed at a process that no longer exists.
  if (n.secondsSinceHeartbeat == null) return false
  return n.secondsSinceHeartbeat <= HEARTBEAT_STALE_AFTER_S
}

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
                    {/* Undated, a failure from the previous evening reads
                        as one from a minute ago. */}
                    {setup.resultAt && (
                      <Typography component="span" variant="caption"
                                  color="text.secondary" sx={{ ml: 1 }}>
                        {new Date(setup.resultAt * 1000).toLocaleString()}
                      </Typography>
                    )}
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
  const [groups, setGroups] = useState(false)
  const [ask, setAsk] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)
  // Whether the group scope is actually delegated. seed_scopes_payload has
  // advertised this capability all along; the checkbox says plainly when
  // the grant behind it is missing, rather than letting someone tick it and
  // find out from a line in the transcript.
  const [groupScope, setGroupScope] = useState<boolean | null>(null)
  useEffect(() => {
    fetchSeedScopes()
      .then((r) => setGroupScope(
        r.capabilities.find((c) => c.flag === '--groups')?.granted ?? null))
      .catch(() => setGroupScope(null))
  }, [])

  const launch = async () => {
    setBusy(true); setError(null)
    try {
      const r = await runSeed(domain, scale, createUsers, false, { groups })
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
        <Tooltip title={groupScope === false
          ? 'admin.directory.group is not delegated — the seed will say so and '
            + 'carry on without groups'
          : 'Creates Google Groups, their members, one nested group, and the '
            + 'group-typed Drive ACLs that need them to exist first'}>
          <FormControlLabel
            control={<Switch checked={groups} data-testid="seed-groups"
                             onChange={(e) => setGroups(e.target.checked)} />}
            label={
              <Typography variant="body2"
                          color={groupScope === false ? 'text.disabled' : undefined}>
                Groups{groupScope === false ? ' (scope not granted)' : ''}
              </Typography>}
          />
        </Tooltip>
        <Button size="small" variant="contained" startIcon={<SeedIcon />} onClick={() => setAsk(true)}>
          Seed now
        </Button>
      </Stack>
      {done && <Alert severity="success" sx={{ mt: 1 }} onClose={() => setDone(null)}>{done}</Alert>}

      <ReasonCodeDialog
        open={ask} busy={busy} error={error} destructive confirmPhrase="SEED"
        title={`Seed ${domain}`}
        description={
          <>Writes test data into <strong>{domain}</strong> at the <strong>{scale}</strong> scale
          {groups ? ', including groups and group-typed Drive shares' : ''}.
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
  // "exit 2" on its own is a number, not a finding. seed_sandbox.py states
  // its own outcome on the way out ("PARTIAL: 200 of 201 users seeded; 1
  // failed"), and that sentence is the actual answer to why the code is
  // non-zero -- it was sitting in the transcript, below the fold, unread.
  const outcome = !running && rc !== 0
    ? [...lines].reverse().find((l) => /^\s*(PARTIAL|FAILED|ABORTED)\b/.test(l))?.trim()
    : undefined
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
          {outcome && (
            <Typography variant="caption" color="error.main" sx={{ display: 'block', fontWeight: 600 }}>
              {outcome}
            </Typography>
          )}
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
              <SeedRunDashboard lines={lines} running={running}
                                elapsedSec={job?.elapsed ?? history?.elapsed} />
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
