import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Alert, Box, Button, Chip, LinearProgress, Paper, Stack, TextField, Tooltip,
  Typography, Divider,
} from '@mui/material'
import {
  Dns as NodeIcon, Circle as DotIcon, Bolt as LiveIcon,
  AddToDrive as DriveIcon, Mail as MailIcon, CalendarMonth as CalendarIcon,
  Contacts as ContactsIcon, Chat as ChatIcon, VpnKey as PermissionsIcon,
} from '@mui/icons-material'
import {
  CPEvent, FleetNode, UserProgress, PublicShare, FailureRow,
  connectCP, fetchFleet, fetchUsers, fetchPublicShares, fetchFailures,
} from '@/api/controlPlane'
import { useMigrationStore } from '@/store'
import { MigrationStatus, ServiceProgress } from '@/types'
import JobController from '@/components/JobController'
import EmergencyBrake from '@/components/EmergencyBrake'
import ForensicModal from '@/components/ForensicModal'
import BenchmarkRunner from '@/components/BenchmarkRunner'
import ProvisionUsers from '@/components/ProvisionUsers'
import AiDiagnostics from '@/components/AiDiagnostics'
import DwdSetup from '@/components/DwdSetup'
import CoverageAudit from '@/components/CoverageAudit'
import NextActions from '@/components/NextActions'

/**
 * Mission Control — one screen instead of six tabs.
 *
 * This amalgamates the pages an operator actually watches *during* a live
 * run: Dashboard's service-status bento, Users' roster, ActivityFeed's live
 * tail, Verification's fidelity cards, SystemHealth's resource numbers, and
 * everything already built for the Command Center (fleet, job control,
 * forensics, benchmarks, provisioning, the emergency brake). One page, one
 * scroll, instead of re-orienting on every tab switch mid-incident.
 *
 * Deliberately NOT folded in: the Setup Wizard, Seed Wizard, Deploy/Settings,
 * per-user detail, and Help. Those are configuration and one-time-setup
 * flows, not things you watch while a migration runs — flattening them in
 * would make this page worse at the one job it has.
 *
 * Two real data sources, shown side by side rather than faked into one:
 * the control plane (:8090, WebSocket-pushed — fleet/users/benchmarks/
 * provisioning) and the existing ledger reads (:8080, via useMigrationStore
 * — service-level breakdown, verification, the raw activity log). Both are
 * genuine reads off migration.db, just through two different servers built
 * at different points in this project; merging the *display* is honest,
 * pretending they were always one system would not be.
 */
type ServiceKey = 'drive' | 'mailbox' | 'calendar' | 'contacts' | 'chat' | 'permissions'
const SERVICES: { key: ServiceKey; label: string; icon: React.ReactElement }[] = [
  { key: 'drive', label: 'Drive', icon: <DriveIcon /> },
  { key: 'mailbox', label: 'Gmail', icon: <MailIcon /> },
  { key: 'calendar', label: 'Calendar', icon: <CalendarIcon /> },
  { key: 'contacts', label: 'Contacts', icon: <ContactsIcon /> },
  { key: 'chat', label: 'Chat', icon: <ChatIcon /> },
  { key: 'permissions', label: 'Permissions', icon: <PermissionsIcon /> },
]
const statusChipSx: Record<MigrationStatus, { bg: string; fg: string }> = {
  completed: { bg: '#e6f4ea', fg: '#137333' }, verified: { bg: '#e6f4ea', fg: '#137333' },
  in_progress: { bg: '#e6f4ea', fg: '#137333' }, retrying: { bg: '#fef7e0', fg: '#b06000' },
  waiting: { bg: '#fef7e0', fg: '#b06000' }, mismatch: { bg: '#fef7e0', fg: '#b06000' },
  failed: { bg: '#fce8e6', fg: '#c5221f' }, needs_attention: { bg: '#fce8e6', fg: '#c5221f' },
  not_started: { bg: '#f1f0f4', fg: '#414754' }, pending: { bg: '#f1f0f4', fg: '#414754' },
  paused: { bg: '#f1f0f4', fg: '#414754' },
}
function aggregateService(services: (ServiceProgress | undefined)[]) {
  const present = services.filter((s): s is ServiceProgress => !!s)
  const done = present.reduce((sum, s) => sum + s.itemsCompleted, 0)
  const total = present.reduce((sum, s) => sum + s.itemsTotal, 0)
  const anyFailed = present.some((s) => s.status === 'failed' || s.status === 'needs_attention')
  const anyRetrying = present.some((s) => s.status === 'retrying' || s.status === 'mismatch')
  const allDone = present.length > 0 && present.every((s) => s.status === 'completed' || s.status === 'verified')
  const anyStarted = present.some((s) => s.status !== 'not_started' && s.status !== 'pending')
  const status: MigrationStatus = anyFailed ? 'needs_attention' : allDone ? 'completed'
    : anyRetrying ? 'retrying' : anyStarted ? 'in_progress' : 'not_started'
  return { done, total, pct: total > 0 ? Math.round((done / total) * 100) : 0, status }
}

const pct = (v: number | null) => (v == null ? '—' : `${Math.round(v)}%`)
const barColor = (v: number | null) => v == null ? 'inherit' : v >= 90 ? 'error' : v >= 70 ? 'warning' : 'primary'

const NodeCard: React.FC<{ node: FleetNode }> = ({ node }) => (
  <Paper variant="outlined" sx={{
    p: 2, borderRadius: 2, minWidth: 240, flex: '1 1 240px',
    borderColor: node.healthy ? 'divider' : 'error.main', borderWidth: node.healthy ? 1 : 2,
  }}>
    <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1 }}>
      <NodeIcon fontSize="small" color={node.healthy ? 'action' : 'error'} />
      <Typography variant="subtitle1" sx={{ fontWeight: 700, flexGrow: 1 }}>{node.node_id}</Typography>
      <Tooltip title={node.healthy ? `heartbeat ${node.secondsSinceHeartbeat}s ago` : 'no recent heartbeat'}>
        <DotIcon sx={{ fontSize: 12 }} color={node.healthy ? 'success' : 'error'} />
      </Tooltip>
    </Stack>
    <Typography variant="caption" color="text.secondary" display="block" noWrap>
      {node.hostname} · {node.location ?? 'unknown'}
    </Typography>
    <Stack spacing={0.75} sx={{ mt: 1.5 }}>
      {([['CPU', node.cpu_pct], ['RAM', node.ram_pct], ['Disk', node.disk_pct]] as const).map(([label, v]) => (
        <Box key={label}>
          <Stack direction="row" justifyContent="space-between">
            <Typography variant="caption" color="text.secondary">{label}</Typography>
            <Typography variant="caption" sx={{ fontVariantNumeric: 'tabular-nums' }}>{pct(v)}</Typography>
          </Stack>
          <LinearProgress variant="determinate" value={v ?? 0} color={barColor(v) as any} sx={{ height: 4 }} />
        </Box>
      ))}
    </Stack>
    <Stack direction="row" spacing={1} sx={{ mt: 1.5 }} flexWrap="wrap">
      {node.active_job
        ? <Chip size="small" color="primary" icon={<LiveIcon />} label={`${node.active_job}${node.job_pid ? ` · ${node.job_pid}` : ''}`} />
        : <Chip size="small" variant="outlined" label="idle" />}
    </Stack>
  </Paper>
)

const MissionControl: React.FC = () => {
  const navigate = useNavigate()
  // -- control plane (:8090) ------------------------------------------------
  const [nodes, setNodes] = useState<FleetNode[]>([])
  const [cpUsers, setCpUsers] = useState<UserProgress[]>([])
  const [shares, setShares] = useState<PublicShare[]>([])
  const [shareCount, setShareCount] = useState(0)
  const [failures, setFailures] = useState<FailureRow[]>([])
  const [connected, setConnected] = useState(false)
  const [alert, setAlert] = useState<string | null>(null)
  const [forensic, setForensic] = useState<{ user: string; item: string } | null>(null)

  const refreshLists = useCallback(() => {
    fetchPublicShares().then(setShares).catch(() => {})
    fetchFailures().then(setFailures).catch(() => {})
    fetchFleet().then(setNodes).catch(() => {})
    fetchUsers().then(setCpUsers).catch(() => {})
  }, [])

  useEffect(() => {
    refreshLists()
    return connectCP((e: CPEvent) => {
      if (e.type === 'SNAPSHOT' || e.type === 'JOB_PROGRESS') {
        setCpUsers(e.data.users ?? []); setNodes(e.data.nodes ?? [])
        setShareCount(e.data.publicShares ?? 0)
      } else if (e.type === 'NODE_HEARTBEAT') {
        fetchFleet().then(setNodes).catch(() => {})
      } else if (e.type === 'CRITICAL_ALERT') {
        setAlert(e.data.message ?? 'Critical alert')
        fetchPublicShares().then(setShares).catch(() => {})
      } else if (e.type === 'ACTION_COMPLETE') {
        setAlert(`${e.data.actor}: ${e.data.action} → ${e.data.outcome}`)
        refreshLists()
      }
    }, setConnected)
  }, [refreshLists])

  // -- ledger reads (:8080, via the existing store/poller) ------------------
  const { users, verification, activities, metrics } = useMigrationStore()
  const [userFilter, setUserFilter] = useState('')

  // Floored, and never 100 unless every user actually is. Math.round turned
  // a mean of 99.99 into "overall 100%" while the Drive card beside it read
  // "501,661 / 501,662 in progress" and Permissions "69,520 / 69,549" -- a
  // header claiming done, thirty items short, next to two panels saying
  // otherwise. Rounding up to a completion figure is the one direction this
  // number must never be wrong in.
  const overallProgress = users.length === 0
    ? 0
    : users.every((u) => u.progress >= 100)
      ? 100
      : Math.min(99, Math.floor(
          users.reduce((sum, u) => sum + u.progress, 0) / users.length))
  const filteredUsers = useMemo(
    () => users.filter((u) => u.email.toLowerCase().includes(userFilter.toLowerCase())),
    [users, userFilter],
  )

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
      <Box sx={{ display: 'flex', alignItems: 'flex-end', gap: 2, flexWrap: 'wrap' }}>
        <Box sx={{ flexGrow: 1 }}>
          <Typography variant="h2">Mission Control</Typography>
          <Stack direction="row" spacing={1} alignItems="center">
            <DotIcon sx={{ fontSize: 10 }} color={connected ? 'success' : 'error'} />
            <Typography variant="body2" color="text.secondary">
              {connected ? 'Live — pushed over WebSocket' : 'Disconnected — reconnecting…'}
              {' · '}{users.length} users tracked · overall {overallProgress}%
            </Typography>
          </Stack>
        </Box>

      <NextActions />
      </Box>

      <Alert severity="info" action={
        <Button color="inherit" size="small" onClick={() => navigate('/wizard?mode=seed')}>
          Open Setup Wizard
        </Button>
      }>
        Need a test tenant seeded first?
      </Alert>

      {alert && <Alert severity="warning" onClose={() => setAlert(null)}>{alert}</Alert>}

      {/* First, because every panel below fails with unauthorized_client
          if this is not done -- there is nothing more useful to show an
          operator ahead of a broken setup than the reason it is broken. */}
      {/* DwdSetup stays here as a live health gate -- it auto-collapses
          when both tenants are green, and a broken delegation is something
          you want to see during a run, not only during setup. CloudSetup
          lives in the Seed Wizard instead: creating projects is a one-time
          setup act, not something to watch mid-migration. */}
      <DwdSetup />

      <EmergencyBrake shares={shares} liveCount={shareCount} onReverted={refreshLists} />

      {/* Fleet */}
      <Box>
        <Typography variant="overline" color="text.secondary">
          Fleet ({nodes.length} node{nodes.length === 1 ? '' : 's'})
          {/* Not CPU/RAM here too -- each NodeCard below already shows its
             own real reading (fleet_agent.py's ps scan). A second, separately
             -timed CPU% computed here from the ledger poller (webui.py's own
             load-average snapshot) drifts from the node card's number just
             from being sampled at a different moment, which reads as a bug
             ("System Health disagrees with Mission Control") rather than
             what it actually is -- two honest but redundant measurements.
             workers has no fleet equivalent, so it's the one number worth
             keeping here. */}
          {metrics && ` · workers ${metrics.workers.current}/${metrics.workers.max}`}
        </Typography>
        <Stack direction="row" spacing={2} sx={{ mt: 1, flexWrap: 'wrap', gap: 2 }}>
          {nodes.map((n) => <NodeCard key={n.node_id} node={n} />)}
          {!nodes.length && (
            <Alert severity="info" sx={{ flex: 1 }}>
              No nodes registered. Run <code>fleet_agent.py</code> on each host.
            </Alert>
          )}
        </Stack>
      </Box>

      {/* Service status, from the ledger's per-service breakdown */}
      <Box>
        <Typography variant="overline" color="text.secondary">Service status</Typography>
        <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', sm: '1fr 1fr', md: '1fr 1fr 1fr' }, gap: 2, mt: 1 }}>
          {SERVICES.map(({ key, label, icon }) => {
            const agg = aggregateService(users.map((u) => u.details?.[key]))
            const sx = statusChipSx[agg.status]
            return (
              <Paper key={key} variant="outlined" sx={{ borderRadius: 2, p: 2 }}>
                <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ mb: 1 }}>
                  <Stack direction="row" alignItems="center" spacing={1}>
                    <Box sx={{ color: 'text.secondary' }}>{icon}</Box>
                    <Typography variant="subtitle2" fontWeight={700}>{label}</Typography>
                  </Stack>
                  <Box sx={{ px: 1, py: 0.25, borderRadius: 999, bgcolor: sx.bg, color: sx.fg,
                             fontSize: 11, fontWeight: 700 }}>
                    {agg.status.replace('_', ' ')}
                  </Box>
                </Stack>
                <LinearProgress variant="determinate" value={agg.pct} sx={{ height: 6 }} />
                <Typography variant="caption" color="text.secondary">
                  {agg.done.toLocaleString()} / {agg.total.toLocaleString()}
                </Typography>
              </Paper>
            )
          })}
        </Box>
      </Box>

      {/* Verification / fidelity */}
      {verification.length > 0 && (
        <Box>
          <Typography variant="overline" color="text.secondary">Verification</Typography>
          <Stack direction="row" spacing={2} sx={{ mt: 1, flexWrap: 'wrap', gap: 2 }}>
            {verification.map((v) => (
              <Paper key={v.type} variant="outlined" sx={{ p: 1.5, borderRadius: 2, minWidth: 180 }}>
                <Typography variant="caption" color="text.secondary">{v.type}</Typography>
                <Typography variant="h6" sx={{ fontWeight: 700 }}>{v.confidence}%</Typography>
                <Chip size="small" label={v.status}
                      color={v.status === 'verified' ? 'success' : v.status === 'mismatch' ? 'warning' : 'default'}
                      variant="outlined" />
              </Paper>
            ))}
          </Stack>
        </Box>
      )}

      {/* High on the page on purpose: when something is wrong, "what is
          going on" is the first question, and it reads the same ledger the
          panels below render one facet of each. */}
      <AiDiagnostics />

      <CoverageAudit />

      <ProvisionUsers />

      <JobController users={cpUsers} nodes={nodes} onChanged={refreshLists} />

      <BenchmarkRunner />

      {/* Roster with search — Users.tsx, folded in */}
      <Paper variant="outlined" sx={{ borderRadius: 2, p: 2 }}>
        <Stack direction="row" alignItems="center" spacing={2} sx={{ mb: 1.5, flexWrap: 'wrap', gap: 1.5 }}>
          <Typography variant="h6" sx={{ flexGrow: 1 }}>Users ({filteredUsers.length})</Typography>
          <TextField size="small" placeholder="Filter by email…" value={userFilter}
                     onChange={(e) => setUserFilter(e.target.value)}
                     sx={{ width: { xs: '100%', sm: 240 } }} />
        </Stack>
        <Stack spacing={0.5} sx={{ maxHeight: 320, overflow: 'auto' }}>
          {filteredUsers.map((u) => (
            <Box key={u.email} sx={{
              display: 'flex', flexDirection: { xs: 'column', sm: 'row' },
              alignItems: { xs: 'flex-start', sm: 'center' }, gap: { xs: 0.5, sm: 1.5 }, p: 1,
              borderBottom: '1px solid', borderColor: 'divider',
            }}>
              <Typography variant="body2" sx={{ fontWeight: 600, width: { xs: '100%', sm: 220 } }} noWrap>
                {u.email}
              </Typography>
              <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, width: '100%', flex: { sm: 1 } }}>
                <LinearProgress variant="determinate" value={u.progress} sx={{ flex: 1, height: 6 }} />
                <Typography variant="caption" sx={{ width: 40, textAlign: 'right' }}>{u.progress}%</Typography>
                <Chip size="small" label={u.status} variant="outlined" />
              </Box>
            </Box>
          ))}
          {!filteredUsers.length && (
            <Typography variant="body2" color="text.secondary" sx={{ py: 2, textAlign: 'center' }}>
              No users match "{userFilter}".
            </Typography>
          )}
        </Stack>
      </Paper>

      {/* Failures -> forensics */}
      <Paper variant="outlined" sx={{ borderRadius: 2, p: 2 }}>
        <Typography variant="h6" sx={{ mb: 1 }}>Recent failures ({failures.length})</Typography>
        <Stack spacing={0.5} sx={{ mt: 1, maxHeight: 260, overflow: 'auto' }}>
          {failures.map((f) => (
            <Box key={f.id} onClick={() => setForensic({ user: f.source_user, item: f.item_id })}
                 sx={{ p: 1, borderRadius: 1, cursor: 'pointer', border: '1px solid', borderColor: 'divider',
                       '&:hover': { bgcolor: 'action.hover' } }}>
              <Stack direction="row" spacing={1} alignItems="center">
                <Chip size="small" color="error" variant="outlined" label={f.item_type} />
                <Typography variant="body2" sx={{ fontWeight: 600 }}>{f.source_user.split('@')[0]}</Typography>
                <Typography variant="caption" color="text.secondary" noWrap sx={{ flexGrow: 1 }}>
                  {f.error_message?.slice(0, 120) ?? 'no message'}
                </Typography>
              </Stack>
            </Box>
          ))}
          {!failures.length && (
            <Typography variant="body2" color="text.secondary" sx={{ py: 2, textAlign: 'center' }}>
              No failures recorded.
            </Typography>
          )}
        </Stack>
      </Paper>

      {/* Live activity tail -- ActivityFeed, folded in */}
      <Paper variant="outlined" sx={{ borderRadius: 2, p: 2 }}>
        <Typography variant="h6" sx={{ mb: 1 }}>Activity</Typography>
        <Stack spacing={0.5} sx={{ maxHeight: 240, overflow: 'auto' }}>
          {activities.slice(0, 30).map((a) => (
            <Box key={a.id} sx={{ display: 'flex', gap: 1, py: 0.5, borderBottom: '1px solid', borderColor: 'divider' }}>
              <Typography variant="caption" color="text.secondary" sx={{ whiteSpace: 'nowrap' }}>
                {new Date(a.timestamp).toLocaleTimeString()}
              </Typography>
              <Typography variant="body2" sx={{ fontWeight: 600 }}>{a.user}</Typography>
              <Typography variant="body2" color="text.secondary" noWrap>{a.action}</Typography>
            </Box>
          ))}
          {!activities.length && (
            <Typography variant="body2" color="text.secondary" sx={{ py: 2, textAlign: 'center' }}>
              No activity yet.
            </Typography>
          )}
        </Stack>
      </Paper>

      <Divider />
      <Typography variant="caption" color="text.secondary" sx={{ textAlign: 'center', pb: 2 }}>
        Setup, Seed, Deploy and per-user detail live in their own pages — this
        screen is for watching and acting during a run, not one-time config.
      </Typography>

      <ForensicModal
        open={!!forensic} sourceUser={forensic?.user ?? null} itemId={forensic?.item ?? null}
        onClose={() => setForensic(null)} onRetried={refreshLists}
      />
    </Box>
  )
}

export default MissionControl
