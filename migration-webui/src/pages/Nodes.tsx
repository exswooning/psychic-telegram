import React, { useCallback, useEffect, useState } from 'react'
import {
  Alert, Box, Button, Chip, CircularProgress, IconButton, Paper, Stack,
  Table, TableBody, TableCell, TableHead, TableRow, TextField, Typography,
} from '@mui/material'
import {
  Refresh as RefreshIcon, ContentCopy as CopyIcon,
  Visibility as ShowIcon, VisibilityOff as HideIcon,
} from '@mui/icons-material'
import {
  fetchClaims, fetchNodeJoin, UserClaim, ClaimSummary, NodeJoinDetails,
} from '@/api/controlPlane'

/**
 * Nodes — who is migrating what, across machines.
 *
 * The direction matters and is the reason this page looks the way it does.
 * Nodes connect OUTWARD to this coordinator and claim work; nothing here
 * reaches into a node. fleet_agent.py's own docstring gives the reason:
 * a control plane that could reach into its nodes would need SSH
 * credentials for every machine holding service-account keys for both
 * tenants, which turns a dashboard into a lateral-movement path across the
 * whole migration. So this page shows state and hands out a join command;
 * it never drives anything remotely.
 *
 * The one genuinely surprising rule it has to communicate: a lease that has
 * expired does NOT free the user for another node. Resume is driven by the
 * dead node's own local item ledger, so a different machine restarting that
 * user would re-insert everything already delivered. Stale claims are shown
 * as needing attention rather than as available work.
 */

const relative = (iso: string): string => {
  const then = Date.parse(iso)
  if (Number.isNaN(then)) return iso
  const secs = Math.round((Date.now() - then) / 1000)
  if (secs < 60) return `${secs}s ago`
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`
  return `${Math.round(secs / 3600)}h ago`
}

const StateChip: React.FC<{ claim: UserClaim }> = ({ claim }) => {
  if (claim.status === 'DONE') {
    return <Chip size="small" color="success" variant="outlined" label="done" />
  }
  if (claim.status === 'FAILED') {
    return <Chip size="small" color="error" variant="outlined" label="failed" />
  }
  if (claim.stale) {
    return <Chip size="small" color="warning" label="lease expired" />
  }
  return <Chip size="small" color="primary" variant="outlined" label="running" />
}

const Stat: React.FC<{ label: string; value: number; testid: string }> =
  ({ label, value, testid }) => (
    <Box data-testid={testid}>
      <Typography sx={{ fontWeight: 700, fontSize: 22, lineHeight: 1.2,
                        fontVariantNumeric: 'tabular-nums' }}>
        {value.toLocaleString()}
      </Typography>
      <Typography variant="caption" color="text.secondary">{label}</Typography>
    </Box>
  )

export const Nodes: React.FC = () => {
  const [claims, setClaims] = useState<UserClaim[]>([])
  const [summary, setSummary] = useState<ClaimSummary | null>(null)
  const [join, setJoin] = useState<NodeJoinDetails | null>(null)
  const [joinError, setJoinError] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [revealed, setRevealed] = useState(false)
  const [nodeTarget, setNodeTarget] = useState('')

  const refresh = useCallback(() => {
    setLoading(true)
    fetchClaims()
      .then((r) => { setClaims(r.claims); setSummary(r.summary); setError('') })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    refresh()
    // Claims change only when a node starts or finishes a user, which is
    // minutes apart -- polling faster would cost more than it shows.
    const t = window.setInterval(refresh, 15000)
    return () => window.clearInterval(t)
  }, [refresh])

  useEffect(() => {
    fetchNodeJoin(false)
      .then(setJoin)
      .catch((e) => setJoinError(e instanceof Error ? e.message : String(e)))
  }, [])

  const reveal = () => {
    if (revealed) { setRevealed(false); fetchNodeJoin(false).then(setJoin).catch(() => {}); return }
    fetchNodeJoin(true).then((j) => { setJoin(j); setRevealed(true) }).catch(() => {})
  }

  const coordinator = join?.coordinatorUrl || window.location.origin
  const command = [
    './node_setup.sh',
    nodeTarget.trim() || '<user@node-address>',
    'root@<coordinator-host>',
    `'${coordinator}'`,
    revealed && join?.token ? `'${join.token}'` : "'<node-token>'",
  ].join(' ')

  const stale = claims.filter((c) => c.stale)

  return (
    <Box sx={{ p: 3 }}>
      <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 0.5 }}>
        <Typography variant="h5" sx={{ fontWeight: 700 }}>Nodes</Typography>
        {loading && <CircularProgress size={16} />}
        <Box sx={{ flex: 1 }} />
        <IconButton size="small" onClick={refresh} aria-label="refresh">
          <RefreshIcon fontSize="small" />
        </IconButton>
      </Stack>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        One migration spread across several machines. Each node claims users
        from this coordinator before touching anything, so no two machines can
        start the same mailbox — which would insert every message twice.
      </Typography>

      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}

      {summary && (
        <Paper variant="outlined" sx={{ p: 2, mb: 3 }}>
          <Stack direction="row" spacing={4} sx={{ flexWrap: 'wrap', gap: 2 }}>
            <Stat testid="stat-total" label="users claimed" value={summary.total} />
            <Stat testid="stat-done" label="done" value={summary.done} />
            <Stat testid="stat-failed" label="failed" value={summary.failed} />
            <Stat testid="stat-stale" label="lease expired" value={summary.stale} />
            <Stat testid="stat-nodes" label="nodes" value={summary.nodes.length} />
          </Stack>
        </Paper>
      )}

      {stale.length > 0 && (
        <Alert severity="warning" sx={{ mb: 3 }} data-testid="stale-warning">
          {stale.length} claim(s) have an expired lease — the node holding them
          stopped. <strong>These are not free for another node to pick up.</strong>{' '}
          Resume reads the item ledger on the machine that was doing the work,
          so a different node restarting one of these would re-deliver
          everything it already migrated. Restart that node to let it continue,
          or force the claim elsewhere only if you are prepared to clean up
          duplicates in the target.
        </Alert>
      )}

      {summary && summary.nodes.length > 0 && (
        <Paper variant="outlined" sx={{ p: 2, mb: 3 }}>
          <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1 }}>
            By node
          </Typography>
          <Stack spacing={1}>
            {summary.nodes.map((n) => (
              <Stack key={n.node} direction="row" spacing={1} alignItems="center"
                     data-testid={`node-${n.node}`}>
                <Typography variant="body2" sx={{ fontWeight: 600, minWidth: 160 }}>
                  {n.node}
                </Typography>
                <Chip size="small" variant="outlined" label={`${n.claimed} running`} />
                <Chip size="small" variant="outlined" color="success" label={`${n.done} done`} />
                {n.failed > 0 && (
                  <Chip size="small" variant="outlined" color="error" label={`${n.failed} failed`} />
                )}
                {n.stale > 0 && (
                  <Chip size="small" color="warning" label={`${n.stale} expired`} />
                )}
              </Stack>
            ))}
          </Stack>
        </Paper>
      )}

      <Paper variant="outlined" sx={{ mb: 3 }}>
        <Box sx={{ p: 2, pb: 1 }}>
          <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>
            Claims
          </Typography>
        </Box>
        {claims.length === 0 ? (
          <Box sx={{ p: 2, pt: 0 }}>
            <Typography variant="body2" color="text.secondary"
                        data-testid="no-claims">
              No claims yet. A single-machine migration never creates any —
              claiming only happens when BITPORT_COORDINATOR is set, so nothing
              changes until you actually add a node.
            </Typography>
          </Box>
        ) : (
          <Box sx={{ overflowX: 'auto' }}>
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell>user</TableCell>
                  <TableCell>node</TableCell>
                  <TableCell>state</TableCell>
                  <TableCell>services</TableCell>
                  <TableCell>lease renewed</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {claims.map((c) => (
                  <TableRow key={`${c.account_id}:${c.source_user}`}
                            data-testid={`claim-${c.source_user}`}>
                    <TableCell sx={{ fontSize: 12 }}>{c.source_user}</TableCell>
                    <TableCell sx={{ fontSize: 12 }}>
                      {c.node_id}
                      {c.forced_from && (
                        <Typography variant="caption" color="warning.main"
                                    sx={{ display: 'block' }}>
                          forced from {c.forced_from}
                        </Typography>
                      )}
                    </TableCell>
                    <TableCell><StateChip claim={c} /></TableCell>
                    <TableCell sx={{ fontSize: 12 }}>{c.services || '—'}</TableCell>
                    <TableCell sx={{ fontSize: 12 }}>{relative(c.renewed_at)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Box>
        )}
      </Paper>

      <Paper variant="outlined" sx={{ p: 2 }}>
        <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1 }}>
          Add a node
        </Typography>

        {joinError && (
          <Alert severity="info" sx={{ mb: 2 }} data-testid="join-restricted">
            {joinError}
          </Alert>
        )}

        {join && !join.enabled && (
          <Alert severity="warning" sx={{ mb: 2 }} data-testid="node-auth-off">
            This control plane is not accepting worker nodes. Set
            BITPORT_NODE_TOKEN in the API service and restart it — without a
            token every claim request is refused, which is the intended
            default rather than an oversight.
          </Alert>
        )}

        {join?.enabled && (
          <>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
              The node connects outward to this coordinator — nothing here
              reaches into it. Over the public address the token is what
              authenticates it; on a tailnet, put the tailnet address below
              instead and the traffic never leaves your private network.
            </Typography>

            <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 2 }}>
              <TextField size="small" label="Node SSH target" sx={{ flex: 1 }}
                         placeholder="ubuntu@100.x.y.z"
                         value={nodeTarget}
                         onChange={(e) => setNodeTarget(e.target.value)}
                         inputProps={{ 'data-testid': 'node-target' }} />
              <Button size="small" startIcon={revealed ? <HideIcon /> : <ShowIcon />}
                      onClick={reveal} data-testid="reveal-token">
                {revealed ? 'Hide token' : 'Show token'}
              </Button>
            </Stack>

            <Box component="pre" data-testid="join-command"
                 sx={{ fontSize: 11, p: 1.5, bgcolor: 'action.hover',
                       borderRadius: 1, overflowX: 'auto', m: 0,
                       whiteSpace: 'pre-wrap' }}>
              {command}
            </Box>
            <Stack direction="row" spacing={1} sx={{ mt: 1 }} alignItems="center">
              <Button size="small" startIcon={<CopyIcon />}
                      onClick={() => navigator.clipboard?.writeText(command)}>
                Copy
              </Button>
              <Typography variant="caption" color="text.secondary">
                Run it from a machine that can reach both. Keys and the identity
                map are copied through you, not served by this API — an endpoint
                handing service-account keys to anything holding a node token
                would make that token equivalent to the keys for the whole
                tenant.
              </Typography>
            </Stack>
          </>
        )}
      </Paper>
    </Box>
  )
}

export default Nodes
