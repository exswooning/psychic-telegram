import React, { useCallback, useEffect, useState } from 'react'
import {
  Alert, Box, Chip, LinearProgress, Paper, Stack, TextField, Tooltip, Typography,
} from '@mui/material'
import {
  Dns as NodeIcon, Circle as DotIcon, Bolt as LiveIcon,
} from '@mui/icons-material'
import {
  CPEvent, FleetNode, UserProgress, PublicShare, FailureRow,
  connectCP, fetchFleet, fetchUsers, fetchPublicShares, fetchFailures,
  getOperator, setOperator,
} from '@/api/controlPlane'
import JobController from '@/components/JobController'
import EmergencyBrake from '@/components/EmergencyBrake'
import ForensicModal from '@/components/ForensicModal'
import BenchmarkRunner from '@/components/BenchmarkRunner'

/**
 * The Command Center screen: fleet health, the security brake, job control,
 * and a failure feed that opens forensics.
 *
 * State arrives over one WebSocket, not per-component polling. The server
 * runs a single ledger tailer and pushes diffs, so N open dashboards cost one
 * DB read per tick rather than N — see api_server.py's module docstring.
 * The REST calls here run exactly once, to fill the lists the socket only
 * sends counts for.
 */
const pct = (v: number | null) => (v == null ? '—' : `${Math.round(v)}%`)

const barColor = (v: number | null) =>
  v == null ? 'inherit' : v >= 90 ? 'error' : v >= 70 ? 'warning' : 'primary'

const NodeCard: React.FC<{ node: FleetNode }> = ({ node }) => (
  <Paper variant="outlined" sx={{
    p: 2, borderRadius: 2, minWidth: 260, flex: '1 1 260px',
    borderColor: node.healthy ? 'divider' : 'error.main',
    borderWidth: node.healthy ? 1 : 2,
  }}>
    <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1 }}>
      <NodeIcon fontSize="small" color={node.healthy ? 'action' : 'error'} />
      <Typography variant="subtitle1" sx={{ fontWeight: 700, flexGrow: 1 }}>
        {node.node_id}
      </Typography>
      <Tooltip title={
        node.healthy
          ? `heartbeat ${node.secondsSinceHeartbeat}s ago`
          : 'no recent heartbeat — node may be down'
      }>
        <DotIcon sx={{ fontSize: 12 }} color={node.healthy ? 'success' : 'error'} />
      </Tooltip>
    </Stack>

    <Typography variant="caption" color="text.secondary" display="block" noWrap>
      {node.hostname} · {node.location ?? 'unknown'}
    </Typography>
    <Typography variant="caption" color="text.secondary" display="block"
                sx={{ fontFamily: 'ui-monospace, monospace' }}>
      {node.code_commit ? `commit ${node.code_commit}` : 'no git history'}
      {node.transfer_mode ? ` · ${node.transfer_mode}` : ''}
    </Typography>

    <Stack spacing={0.75} sx={{ mt: 1.5 }}>
      {([['CPU', node.cpu_pct], ['RAM', node.ram_pct], ['Disk', node.disk_pct]] as const)
        .map(([label, v]) => (
          <Box key={label}>
            <Stack direction="row" justifyContent="space-between">
              <Typography variant="caption" color="text.secondary">{label}</Typography>
              <Typography variant="caption" sx={{ fontVariantNumeric: 'tabular-nums' }}>
                {pct(v)}
              </Typography>
            </Stack>
            <LinearProgress variant="determinate" value={v ?? 0}
                            color={barColor(v) as any} sx={{ height: 4 }} />
          </Box>
        ))}
    </Stack>

    <Stack direction="row" spacing={1} sx={{ mt: 1.5 }} flexWrap="wrap">
      {node.active_job
        ? <Chip size="small" color="primary" icon={<LiveIcon />}
                label={`${node.active_job}${node.job_pid ? ` · pid ${node.job_pid}` : ''}`} />
        : <Chip size="small" variant="outlined" label="idle" />}
      {node.users_failed > 0 && (
        <Chip size="small" color="error" label={`${node.users_failed} failed`} />
      )}
    </Stack>
  </Paper>
)

const FleetDashboard: React.FC = () => {
  const [nodes, setNodes] = useState<FleetNode[]>([])
  const [users, setUsers] = useState<UserProgress[]>([])
  const [shares, setShares] = useState<PublicShare[]>([])
  const [shareCount, setShareCount] = useState(0)
  const [failures, setFailures] = useState<FailureRow[]>([])
  const [connected, setConnected] = useState(false)
  const [alert, setAlert] = useState<string | null>(null)
  const [operatorName, setOperatorName] = useState(getOperator())
  const [forensic, setForensic] = useState<{ user: string; item: string } | null>(null)

  const refreshLists = useCallback(() => {
    // Only the things the socket sends as counts rather than rows.
    fetchPublicShares().then(setShares).catch(() => {})
    fetchFailures().then(setFailures).catch(() => {})
    fetchFleet().then(setNodes).catch(() => {})
    fetchUsers().then(setUsers).catch(() => {})
  }, [])

  useEffect(() => {
    refreshLists()
    return connectCP((e: CPEvent) => {
      if (e.type === 'SNAPSHOT' || e.type === 'JOB_PROGRESS') {
        setUsers(e.data.users ?? [])
        setNodes(e.data.nodes ?? [])
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

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
      <Box sx={{ display: 'flex', alignItems: 'flex-end', gap: 2, flexWrap: 'wrap' }}>
        <Box sx={{ flexGrow: 1 }}>
          <Typography variant="h2">Command Center</Typography>
          <Stack direction="row" spacing={1} alignItems="center">
            <DotIcon sx={{ fontSize: 10 }} color={connected ? 'success' : 'error'} />
            <Typography variant="body2" color="text.secondary">
              {connected ? 'Live — pushed over WebSocket' : 'Disconnected — reconnecting…'}
            </Typography>
          </Stack>
        </Box>
        {/* Identity is a plain field, not auth: the real access control is the
            SSH tunnel. This exists so every action has a name attached in
            operator_actions_log. */}
        <TextField
          size="small" label="Operator" value={operatorName}
          onChange={(e) => { setOperatorName(e.target.value); setOperator(e.target.value) }}
          helperText="Recorded against every action"
          sx={{ width: 220 }}
        />
      </Box>

      {alert && <Alert severity="warning" onClose={() => setAlert(null)}>{alert}</Alert>}

      <EmergencyBrake shares={shares} liveCount={shareCount} onReverted={refreshLists} />

      <Box>
        <Typography variant="overline" color="text.secondary">
          Fleet ({nodes.length} node{nodes.length === 1 ? '' : 's'})
        </Typography>
        <Stack direction="row" spacing={2} sx={{ mt: 1, flexWrap: 'wrap', gap: 2 }}>
          {nodes.map((n) => <NodeCard key={n.node_id} node={n} />)}
          {!nodes.length && (
            <Alert severity="info" sx={{ flex: 1 }}>
              No nodes have registered. Each host posts to
              <code> /api/v2/fleet/heartbeat</code>; run <code>fleet_agent.py</code> there.
            </Alert>
          )}
        </Stack>
      </Box>

      <JobController users={users} nodes={nodes} onChanged={refreshLists} />

      <BenchmarkRunner />

      <Paper variant="outlined" sx={{ borderRadius: 2, p: 2 }}>
        <Typography variant="h6" sx={{ mb: 1 }}>
          Recent failures ({failures.length})
        </Typography>
        <Typography variant="caption" color="text.secondary">
          Click a row for the exact API response, retry history, and Fix &amp; Retry.
        </Typography>
        <Stack spacing={0.5} sx={{ mt: 1.5, maxHeight: 300, overflow: 'auto' }}>
          {failures.map((f) => (
            <Box
              key={f.id}
              onClick={() => setForensic({ user: f.source_user, item: f.item_id })}
              sx={{
                p: 1, borderRadius: 1, cursor: 'pointer', border: '1px solid',
                borderColor: 'divider', '&:hover': { bgcolor: 'action.hover' },
              }}
            >
              <Stack direction="row" spacing={1} alignItems="center">
                <Chip size="small" color="error" variant="outlined" label={f.item_type} />
                <Typography variant="body2" sx={{ fontWeight: 600 }}>
                  {f.source_user.split('@')[0]}
                </Typography>
                <Typography variant="caption" color="text.secondary" noWrap sx={{ flexGrow: 1 }}>
                  {f.error_message?.slice(0, 120) ?? 'no message'}
                </Typography>
                <Typography variant="caption" color="text.secondary" sx={{ whiteSpace: 'nowrap' }}>
                  {new Date(f.timestamp).toLocaleTimeString()}
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

      <ForensicModal
        open={!!forensic}
        sourceUser={forensic?.user ?? null}
        itemId={forensic?.item ?? null}
        onClose={() => setForensic(null)}
        onRetried={refreshLists}
      />
    </Box>
  )
}

export default FleetDashboard
