/**
 * The machines that have joined, and whether they are actually there.
 *
 * The page could hand out join codes and start work, but never showed what
 * had joined -- so a node that installed cleanly and a node that never
 * checked in looked identical from here.
 *
 * Liveness is derived from last_seen server-side rather than stored, for
 * the reason cpdb.fleet() gives: a node that dies cannot mark itself down.
 * So "offline" here means "has not been heard from", which is the only
 * thing anyone can honestly know from this end.
 */
import React, { useCallback, useEffect, useState } from 'react'
import {
  Box, Chip, Paper, Stack, Switch, Table, TableBody, TableCell, TableHead,
  TableRow, Tooltip, Typography,
} from '@mui/material'
import { fetchFleet, setNodeTakesWork } from '@/api/controlPlane'
import type { FleetNode } from '@/api/controlPlane'

const ago = (iso: string): string => {
  const then = Date.parse(iso)
  if (Number.isNaN(then)) return '—'
  const s = Math.round((Date.now() - then) / 1000)
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.round(s / 60)}m ago`
  if (s < 86400) return `${Math.round(s / 3600)}h ago`
  return `${Math.round(s / 86400)}d ago`
}

export const NodeList: React.FC = () => {
  const [nodes, setNodes] = useState<FleetNode[]>([])
  const [loaded, setLoaded] = useState(false)
  const [busy, setBusy] = useState('')

  const refresh = useCallback(() => {
    fetchFleet()
      .then((n) => { setNodes(n); setLoaded(true) })
      .catch(() => setLoaded(true))
  }, [])

  useEffect(() => {
    refresh()
    // Faster than the claims poll: this is the panel someone watches while
    // waiting for a machine they just set up to appear.
    const t = window.setInterval(refresh, 10000)
    return () => window.clearInterval(t)
  }, [refresh])

  return (
    <Paper variant="outlined" sx={{ p: 2, mt: 3 }} data-testid="node-list">
      <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1 }}>
        Machines
      </Typography>

      {loaded && nodes.length === 0 && (
        <Typography variant="body2" color="text.secondary"
                    data-testid="no-nodes">
          None yet. A machine appears here once it has checked in — the
          installer sends one on the way out, and the agent sends one each
          time it polls.
        </Typography>
      )}

      {nodes.length > 0 && (
        <Box sx={{ overflowX: 'auto' }}>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>machine</TableCell>
                <TableCell>takes work</TableCell>
                <TableCell>state</TableCell>
                <TableCell>doing</TableCell>
                <TableCell>last heard from</TableCell>
                <TableCell align="right">done</TableCell>
                <TableCell align="right">failed</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {nodes.map((n) => (
                <TableRow key={n.node_id} data-testid={`node-${n.node_id}`}>
                  <TableCell sx={{ fontSize: 12, fontWeight: 600 }}>
                    {n.node_id}
                  </TableCell>
                  <TableCell>
                    {/* Excluding a machine does not stop the run: the node
                        ANDs this with the tenant's directive on its next
                        poll, and finishes the user it is on first. */}
                    <Tooltip title={n.takes_work === 0
                      ? 'Sitting out — the tenant can still be running'
                      : 'Picks up users when this tenant is running'}>
                      <Switch size="small" disabled={busy === n.node_id}
                              checked={n.takes_work !== 0}
                              data-testid={`takes-${n.node_id}`}
                              onChange={(e) => {
                                const want = e.target.checked
                                setBusy(n.node_id)
                                setNodes((cur) => cur.map((x) => x.node_id === n.node_id
                                  ? { ...x, takes_work: want ? 1 : 0 } : x))
                                setNodeTakesWork(n.node_id, want)
                                  .catch(() => refresh())
                                  .finally(() => { setBusy(''); refresh() })
                              }} />
                    </Tooltip>
                  </TableCell>
                  <TableCell>
                    {/* `healthy`, computed from last_seen at read time --
                        see cpdb.fleet(). A node that dies cannot mark
                        itself down, which is the whole failure mode. */}
                    <Chip size="small"
                          color={n.healthy ? 'success' : 'default'}
                          variant={n.healthy ? 'filled' : 'outlined'}
                          label={n.healthy ? 'online' : 'offline'}
                          data-testid={`state-${n.node_id}`} />
                  </TableCell>
                  <TableCell sx={{ fontSize: 12 }}>
                    {n.active_job || <span style={{ opacity: 0.6 }}>idle</span>}
                  </TableCell>
                  <TableCell sx={{ fontSize: 12 }}>{ago(n.last_seen)}</TableCell>
                  <TableCell align="right" sx={{ fontSize: 12,
                                                 fontVariantNumeric: 'tabular-nums' }}>
                    {n.users_done}
                  </TableCell>
                  <TableCell align="right"
                             sx={{ fontSize: 12, fontVariantNumeric: 'tabular-nums',
                                   color: n.users_failed > 0 ? 'warning.main' : undefined }}>
                    {n.users_failed}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <Stack direction="row" spacing={1} sx={{ mt: 1 }}>
            <Typography variant="caption" color="text.secondary">
              Offline means nothing has been heard from it recently — a node
              that dies cannot report that it died, so this is measured from
              the last check-in rather than stored. Turning off “takes work”
              excludes one machine without stopping the tenant&apos;s run; it
              applies on that node&apos;s next poll, and it finishes the user
              it is on first.
            </Typography>
          </Stack>
        </Box>
      )}
    </Paper>
  )
}

export default NodeList
