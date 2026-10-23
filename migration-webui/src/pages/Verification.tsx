import React, { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Box,
  Typography,
  Card,
  CardContent,
  Grid,
  LinearProgress,
  Chip,
  Paper,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Alert,
  Avatar,
  Stack,
  IconButton,
  Tooltip,
  Menu,
  MenuItem,
  ListItemIcon,
  ListItemText,
} from '@mui/material'
import {
  CheckCircle as VerifiedIcon, Warning as MismatchIcon, HourglassEmpty as PendingIcon,
  Block as NotStartedIcon, Assessment as ScoreIcon, Refresh as RefreshIcon,
  Language as DomainIcon, Grass as SeedIcon, RocketLaunch as MigrateIcon,
} from '@mui/icons-material'
import { useMigrationStore } from '@/store'
import { statusLabel, statusColor } from '@/utils/formatters'
import {
  fetchVerifiedDomains, VerifiedDomain, fetchMe, fetchHistory, HistoryRun, migrationOf,
} from '@/api/controlPlane'
import { fetchJob, JobStatus } from '@/api/client'
import JobRunner from '@/components/JobRunner'
import { fetchActions, ActionSpec } from '@/api/client'
import { VERIFICATION_KEYS } from '@/actionHomes'
import { tint } from '@/theme'
import { describeElapsed } from '@/hooks/useRunningJobs'
import { verificationEstimate } from '@/components/RunningJobDetail.utils'
import PairParityCard from '@/components/PairParityCard'

const runWhen = (value: string | null): string => {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

const runDuration = (run: HistoryRun): string => {
  const start = run.startedAt ? Date.parse(run.startedAt) : NaN
  if (Number.isNaN(start)) return '—'
  const end = run.finishedAt ? Date.parse(run.finishedAt) : Date.now()
  return describeElapsed(Math.max(0, Math.round((end - start) / 1000)))
}

const RunOutcome: React.FC<{ run: HistoryRun }> = ({ run }) => {
  if (run.running) return <Chip size="small" color="info" label="running" />
  if (run.rc === null) return <Chip size="small" color="warning" label="result unknown" />
  if (run.rc === 0) return <Chip size="small" color="success" label="finished; inspect result" />
  return <Chip size="small" color="error" label={run.rc < 0 ? `stopped (signal ${-run.rc})` : `failed (exit ${run.rc})`} />
}

/** Verification and its follow-up repair history, plus a live ETA while a
 *  one-to-one run is still checking its current user. */
const VerificationRuns: React.FC = () => {
  const [accountId, setAccountId] = useState<number | undefined>()
  const [runs, setRuns] = useState<HistoryRun[] | null>(null)
  const [job, setJob] = useState<JobStatus | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    fetchMe().then((me) => setAccountId(migrationOf(me)))
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
  }, [])

  const load = useCallback(() => {
    if (!accountId) return
    Promise.all([
      fetchHistory(accountId),
      fetchJob(0, String(accountId), 'verify').catch(() => null),
    ]).then(([history, current]) => {
      setRuns(history.runs.filter((r) => r.jobName === 'verify' || r.jobName === 'repair'))
      setJob(current)
      setError('')
    }).catch((e) => setError(e instanceof Error ? e.message : String(e)))
  }, [accountId])

  useEffect(() => {
    load()
    const timer = window.setInterval(load, 15000)
    return () => window.clearInterval(timer)
  }, [load])

  const estimate = job?.running
    ? verificationEstimate(job.lines, job.elapsed) : null

  return (
    <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', mb: 3 }}>
      <CardContent sx={{ p: 3 }}>
        <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1 }}>
          <VerifiedIcon color="action" fontSize="small" />
          <Typography variant="h6" sx={{ fontWeight: 600, flexGrow: 1 }}>Verification runs</Typography>
          <IconButton size="small" onClick={load} aria-label="refresh verification runs">
            <RefreshIcon fontSize="small" />
          </IconButton>
        </Stack>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          Every recorded verification run and its repair follow-up. A successful process exit does not by itself mean the data matched; open the one-to-one results for the verdict.
        </Typography>

        {error && <Alert severity="warning" sx={{ mb: 2 }}>{error}</Alert>}
        {job?.running && (
          <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
            <Stack direction="row" alignItems="center" flexWrap="wrap" gap={2}>
              <Chip size="small" color="info" label="Verification in progress" />
              <Typography variant="body2" color="text.secondary">
                Elapsed {describeElapsed(job.elapsed)}
              </Typography>
              {estimate && (
                <>
                  <Typography variant="body2" sx={{ fontVariantNumeric: 'tabular-nums' }}>
                    {estimate.etaSec != null
                      ? `ETA about ${describeElapsed(Math.round(estimate.etaSec))}`
                      : 'ETA calibrating'}
                  </Typography>
                  <Typography variant="caption" color="text.secondary">
                    {Math.floor(estimate.doneServices)} of {estimate.totalServices} service checks; early projection, recalculated as checks finish
                  </Typography>
                </>
              )}
              {!estimate && <Typography variant="body2" color="text.secondary">ETA is calibrating until item progress appears.</Typography>}
            </Stack>
            {estimate && <LinearProgress variant="determinate" value={estimate.progressPct} sx={{ mt: 1.5, height: 6, borderRadius: 3 }} />}
          </Paper>
        )}

        <TableContainer sx={{ maxHeight: 420 }}>
          <Table size="small" stickyHeader>
            <TableHead>
              <TableRow>
                <TableCell>Run</TableCell>
                <TableCell>Started</TableCell>
                <TableCell>Finished</TableCell>
                <TableCell>Duration</TableCell>
                <TableCell>Process status</TableCell>
                <TableCell>Details</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {(runs ?? []).map((run, i) => (
                <TableRow key={`${run.jobName}-${run.pid}-${run.startedAt}-${i}`} hover>
                  <TableCell><Chip size="small" variant="outlined" label={run.jobName === 'repair' ? 'repair' : 'verify'} /></TableCell>
                  <TableCell><Typography variant="body2">{runWhen(run.startedAt)}</Typography></TableCell>
                  <TableCell><Typography variant="body2">{runWhen(run.finishedAt)}</Typography></TableCell>
                  <TableCell><Typography variant="body2" sx={{ fontVariantNumeric: 'tabular-nums' }}>{runDuration(run)}</Typography></TableCell>
                  <TableCell><RunOutcome run={run} /></TableCell>
                  <TableCell sx={{ maxWidth: 320 }}>
                    {run.detail && <Tooltip title={run.detail}><Typography variant="body2" color="text.secondary" noWrap>{run.detail}</Typography></Tooltip>}
                  </TableCell>
                </TableRow>
              ))}
              {(runs ?? []).length === 0 && (
                <TableRow><TableCell colSpan={6}>
                  <Typography variant="body2" color="text.secondary" sx={{ py: 2 }}>
                    {runs ? 'No verification or repair runs recorded yet.' : 'Loading…'}
                  </Typography>
                </TableCell></TableRow>
              )}
            </TableBody>
          </Table>
        </TableContainer>
      </CardContent>
    </Card>
  )
}

/**
 * Which domain(s) this account has actually finished setting up (via the
 * Setup/Seed Wizard) and can use right now -- distinct from the data
 * reconciliation checks below, which need a completed migration run to
 * mean anything. This needs only a completed DWD grant: it re-asks Google
 * whether tokens for the required scopes still issue, the same functional
 * check dwd_status runs, so "verified" here means genuinely working, not
 * just "a domain was typed into a form once".
 */
const VERIFIED_DOMAIN_LABEL: Record<VerifiedDomain['status'], string> = {
  verified: 'Verified', pending: 'Propagating', not_verified: 'Not verified',
  not_set_up: 'Not set up', error: 'Check failed',
}
const VERIFIED_DOMAIN_COLOR: Record<VerifiedDomain['status'], 'success' | 'warning' | 'error' | 'default'> = {
  verified: 'success', pending: 'warning', not_verified: 'error',
  not_set_up: 'default', error: 'error',
}

const VerifiedDomains: React.FC = () => {
  const navigate = useNavigate()
  const [domains, setDomains] = useState<VerifiedDomain[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [seedEnabled, setSeedEnabled] = useState(false)
  const [menuFor, setMenuFor] = useState<{ side: 'source' | 'target'; anchor: HTMLElement } | null>(null)

  // Skipped while one is already in flight. This endpoint mints a live token
  // per delegated scope to prove delegation still works -- 25 of them on this
  // tenant -- and takes about six seconds to answer. Polled every five, each
  // call overlapped the last and never stopped, so an open tab was issuing
  // roughly five token mints a second against Google for as long as it stayed
  // open. The poll is cheap to skip and expensive to double up.
  const inFlight = useRef(false)
  const refresh = useCallback(() => {
    if (inFlight.current) return
    inFlight.current = true
    setLoading(true); setError(null)
    fetchVerifiedDomains()
      .then((r) => setDomains(r.domains ?? []))
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => { inFlight.current = false; setLoading(false) })
  }, [])

  // Auto-poll, not fetch-once -- DWD status (verified/pending/error) is
  // exactly the kind of thing that changes while this page stays open
  // (propagation finishing, a scope going live), and a one-shot fetch left
  // it showing whatever was true at page load until a manual re-check.
  useEffect(() => {
    refresh()
    // 30s, not 5s: delegation propagating is a minutes-scale event, and the
    // check is not free -- see refresh above.
    const id = setInterval(refresh, 30000)
    return () => clearInterval(id)
  }, [refresh])
  useEffect(() => { fetchMe().then((a) => setSeedEnabled(a.seed_enabled)).catch(() => {}) }, [])

  if (domains !== null && domains.length === 0 && !error) return null

  const closeMenu = () => setMenuFor(null)
  const goSeed = () => { closeMenu(); navigate('/wizard?mode=seed') }
  const goMigrate = () => { closeMenu(); navigate('/mission-control') }

  return (
    <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', mb: 3 }}>
      <CardContent sx={{ p: 3 }}>
        <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1.5 }}>
          <DomainIcon color="action" fontSize="small" />
          <Typography variant="h6" sx={{ fontWeight: 600, flexGrow: 1 }}>Verified Domains</Typography>
          <Tooltip title="Re-check">
            <span>
              <IconButton size="small" onClick={refresh} disabled={loading}>
                <RefreshIcon fontSize="small" />
              </IconButton>
            </span>
          </Tooltip>
        </Stack>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          Domains set up through the wizard, with a live check of whether
          their domain-wide delegation is actually working -- not just
          granted, but still issuing tokens right now. Click a domain for
          seed/migrate options.
        </Typography>

        {loading && domains === null && <LinearProgress sx={{ mb: 1 }} />}
        {error && <Alert severity="warning">{error}</Alert>}

        {domains && domains.length > 0 && (
          <Stack spacing={1}>
            {domains.map((d) => (
              <Box key={d.side}
                   onClick={(e) => setMenuFor({ side: d.side, anchor: e.currentTarget })}
                   sx={{
                     display: 'flex', alignItems: 'center', gap: 1.5, p: 1.5,
                     borderRadius: 1, bgcolor: 'action.hover', flexWrap: 'wrap',
                     cursor: 'pointer', '&:hover': { bgcolor: 'action.selected' },
                   }}>
                <Chip size="small" label={d.side} variant="outlined" sx={{ textTransform: 'capitalize' }} />
                <Typography variant="body2" sx={{ fontWeight: 600, flexGrow: 1, minWidth: 160 }}>
                  {d.domain}
                </Typography>
                {d.total > 0 && (
                  <Typography variant="caption" color="text.secondary" sx={{ fontVariantNumeric: 'tabular-nums' }}>
                    {d.live}/{d.total} scopes
                  </Typography>
                )}
                <Chip size="small" label={VERIFIED_DOMAIN_LABEL[d.status]}
                      color={VERIFIED_DOMAIN_COLOR[d.status]}
                      variant={d.status === 'verified' ? 'filled' : 'outlined'} />
              </Box>
            ))}
          </Stack>
        )}

        <Menu anchorEl={menuFor?.anchor} open={!!menuFor} onClose={closeMenu}>
          {menuFor?.side === 'source' && seedEnabled && (
            <MenuItem onClick={goSeed}>
              <ListItemIcon><SeedIcon fontSize="small" /></ListItemIcon>
              <ListItemText>Seed this tenant</ListItemText>
            </MenuItem>
          )}
          <MenuItem onClick={goMigrate}>
            <ListItemIcon><MigrateIcon fontSize="small" /></ListItemIcon>
            <ListItemText>Migrate</ListItemText>
          </MenuItem>
        </Menu>
      </CardContent>
    </Card>
  )
}

const Verification: React.FC = () => {
  // Lives on the page, not in VerifiedDomains -- the card renders at the
  // bottom of the page body, and the state has to be in the same component.
  const [checks, setChecks] = useState<Record<string, ActionSpec>>({})
  useEffect(() => { fetchActions().then(setChecks).catch(() => {}) }, [])
  const { verification } = useMigrationStore()
  const overallConfidence = verification.length > 0
    ? Math.round(verification.reduce((sum, v) => sum + v.confidence, 0) / verification.length)
    : 0

  const formatAge = (seconds: number): string => {
    if (seconds < 3600) return `${Math.round(seconds / 60)}m old`
    if (seconds < 86400) return `${Math.round(seconds / 3600)}h old`
    return `${Math.round(seconds / 86400)}d old`
  }
  // acl_audit.py is a standalone script that never runs automatically during
  // migrate/delta, so this row's numbers can describe a run from days ago
  // rather than what is happening right now -- flagged past 10 minutes so a
  // stale snapshot is never mistaken for live progress.
  const isStale = (seconds?: number | null) => seconds != null && seconds > 600

  const statusIcon = (status: string) => {
    switch (status) {
      case 'verified': return <VerifiedIcon color="success" />
      case 'mismatch': return <MismatchIcon color="warning" />
      case 'pending': return <PendingIcon color="action" />
      default: return <NotStartedIcon color="disabled" />
    }
  }

  return (
    <Box>
      <Typography variant="h4" sx={{ fontWeight: 700, mb: 0.5 }}>Verification</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>Data integrity checks across all migrated services</Typography>

      <PairParityCard />

      <VerifiedDomains />

      <VerificationRuns />

      <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', mb: 3 }}>
        <CardContent sx={{ p: 3, display: 'flex', alignItems: 'center', gap: 3 }}>
          <Avatar sx={[{ width: 64, height: 64 }, tint(overallConfidence >= 95 ? 'success' : overallConfidence >= 80 ? 'warning' : 'error')]}>
            <ScoreIcon sx={{ fontSize: 32 }} />
          </Avatar>
          <Box>
            <Typography variant="caption" color="text.secondary">Overall Confidence</Typography>
            <Typography variant="h3" sx={{ fontWeight: 700 }}>{overallConfidence}%</Typography>
            <Typography variant="body2" color="text.secondary">
              {verification.filter((v) => v.status === 'verified').length} verified · {verification.filter((v) => v.status === 'mismatch').length} mismatches · {verification.filter((v) => v.status === 'pending').length} pending
            </Typography>
          </Box>
        </CardContent>
      </Card>

      <Grid container spacing={2}>
        {verification.map((item) => (
          <Grid item xs={12} sm={6} md={4} key={item.type}>
            <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider' }}>
              <CardContent>
                <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', mb: 1 }}>
                  <Typography variant="subtitle2" fontWeight={600} sx={{ textTransform: 'capitalize' }}>{item.type}</Typography>
                  {statusIcon(item.status)}
                </Box>
                <LinearProgress variant="determinate" value={item.confidence} sx={{ height: 8, borderRadius: 4, mb: 1 }} />
                <Typography variant="caption" color="text.secondary">Confidence: {item.confidence}%</Typography>
                <Box sx={{ display: 'flex', justifyContent: 'space-between', mt: 1 }}>
                  <Typography variant="caption" color="text.secondary">Source: {item.sourceCount}</Typography>
                  <Typography variant="caption" color="text.secondary">Target: {item.targetCount}</Typography>
                </Box>
                <Box sx={{ display: 'flex', gap: 0.5, mt: 1, flexWrap: 'wrap' }}>
                  <Chip label={statusLabel(item.status)} size="small" color={statusColor(item.status)} variant="outlined" />
                  {isStale(item.ageSeconds) && (
                    <Chip label={`stale · ${formatAge(item.ageSeconds!)}`} size="small" color="warning" variant="outlined" />
                  )}
                </Box>
              </CardContent>
            </Card>
          </Grid>
        ))}
      </Grid>

      <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', mt: 3 }}>
        <CardContent>
          <Typography variant="h6" sx={{ fontWeight: 600, mb: 2 }}>Detailed Results</Typography>
          <TableContainer>
            <Table>
              <TableHead>
                <TableRow>
                  <TableCell sx={{ fontWeight: 600 }}>Type</TableCell>
                  <TableCell sx={{ fontWeight: 600 }}>Status</TableCell>
                  <TableCell sx={{ fontWeight: 600 }}>Source</TableCell>
                  <TableCell sx={{ fontWeight: 600 }}>Target</TableCell>
                  <TableCell sx={{ fontWeight: 600 }}>Confidence</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {verification.map((item) => (
                  <TableRow key={item.type}>
                    <TableCell sx={{ fontWeight: 500 }}>{item.type}</TableCell>
                    <TableCell><Chip label={statusLabel(item.status)} size="small" color={statusColor(item.status)} variant="outlined" /></TableCell>
                    <TableCell>{item.sourceCount}</TableCell>
                    <TableCell>{item.targetCount}</TableCell>
                    <TableCell>
                      <LinearProgress variant="determinate" value={item.confidence} sx={{ height: 6, borderRadius: 3, width: 100 }} />
                      <Typography variant="caption">{item.confidence}%</Typography>
                      {isStale(item.ageSeconds) && (
                        <Typography variant="caption" color="warning.main" display="block">
                          stale snapshot, {formatAge(item.ageSeconds!)} · not live
                        </Typography>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>
        </CardContent>
      </Card>
    
      {/* The read-only "is this right?" family. Every one of these had an
          ACTIONS entry and no button in this app -- verify, acl_audit,
          verify_ledger, ui_check, the two scope probes and the external-share
          report were reachable only from the legacy wizard. */}
      {VERIFICATION_KEYS.some((k) => checks[k]) && (
        <Card sx={{ mt: 3 }}>
          <CardContent>
            <Typography variant="h6" sx={{ fontWeight: 600, mb: 0.5 }}>
              Checks you can run
            </Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
              All read-only. Each asks the tenants or the ledger a question and
              reports what it found.
            </Typography>
            <Stack spacing={3}>
              {VERIFICATION_KEYS.filter((k) => checks[k]).map((k) => (
                <JobRunner key={k} name={k} spec={checks[k]} />
              ))}
            </Stack>
          </CardContent>
        </Card>
      )}
</Box>
  )
}

export default Verification
