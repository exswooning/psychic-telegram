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

/** A percentage bar is meaningless without its denominator: "78%" of an
 *  unknown amount of RAM does not help anyone decide where to put work. */
const spec = (n: FleetNode): string => {
  const bits: string[] = []
  if (n.cpu_cores) bits.push(`${n.cpu_cores} core${n.cpu_cores === 1 ? '' : 's'}`)
  if (n.ram_gb) bits.push(`${n.ram_gb} GB RAM`)
  if (n.disk_gb) bits.push(`${Math.round(n.disk_gb)} GB disk`)
  return bits.join(' · ')
}

const OS_NAME: Record<string, string> = {
  darwin: 'macOS', win32: 'Windows', linux: 'Linux',
}

const pct = (v: number | null | undefined): string =>
  v === null || v === undefined ? '—' : `${Math.round(v)}%`

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
  // Which offline machine's checklist is open. "offline" on its own tells
  // you a fact and leaves you with a question; the coordinator cannot
  // restart a node -- by design, nothing here reaches into a machine -- so
  // the most it can honestly do is say where to look, in order.
  const [why, setWhy] = useState('')

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
                <TableCell>specs</TableCell>
                <TableCell align="right">cpu</TableCell>
                <TableCell align="right">ram</TableCell>
                <TableCell align="right">disk</TableCell>
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
                    {n.platform && (
                      <Typography variant="caption" color="text.secondary"
                                  sx={{ display: 'block', fontWeight: 400 }}>
                        {OS_NAME[n.platform] || n.platform}
                      </Typography>
                    )}
                  </TableCell>
                  <TableCell sx={{ fontSize: 12 }}
                             data-testid={`spec-${n.node_id}`}>
                    {spec(n) || <span style={{ opacity: 0.6 }}>not reported</span>}
                  </TableCell>
                  {/* Live load, next to the capacity it is a fraction of.
                      A dash where a node could not measure one -- 0% would
                      read as idle on a machine that is flat out. */}
                  <TableCell align="right" sx={{ fontSize: 12,
                                                 fontVariantNumeric: 'tabular-nums' }}
                             data-testid={`cpu-${n.node_id}`}>{pct(n.cpu_pct)}</TableCell>
                  <TableCell align="right" sx={{ fontSize: 12,
                                                 fontVariantNumeric: 'tabular-nums',
                                                 color: (n.ram_pct ?? 0) > 90
                                                   ? 'warning.main' : undefined }}
                             data-testid={`ram-${n.node_id}`}>{pct(n.ram_pct)}</TableCell>
                  <TableCell align="right" sx={{ fontSize: 12,
                                                 fontVariantNumeric: 'tabular-nums',
                                                 color: (n.disk_pct ?? 0) > 90
                                                   ? 'warning.main' : undefined }}
                             data-testid={`disk-${n.node_id}`}>{pct(n.disk_pct)}</TableCell>
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
                          label={n.healthy ? 'online' : 'offline — why?'}
                          onClick={n.healthy ? undefined
                            : () => setWhy((w) => w === n.node_id ? '' : n.node_id)}
                          sx={n.healthy ? undefined : { cursor: 'pointer' }}
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
              {nodes.filter((n) => n.node_id === why).map((n) => (
                <TableRow key={`${n.node_id}-why`}>
                  <TableCell colSpan={11} sx={{ bgcolor: 'action.hover' }}>
                    <Box data-testid={`why-${n.node_id}`} sx={{ py: 1 }}>
                      <Typography variant="body2" sx={{ fontWeight: 600, mb: 1 }}>
                        {n.node_id} has not checked in for {ago(n.last_seen)
                          .replace(' ago', '')}. Check in this order:
                      </Typography>
                      <Typography variant="body2" component="div" sx={{ mb: 1 }}>
                        <strong>1. Is it on and awake?</strong> A sleeping
                        machine freezes its agent, so heartbeats stop. It
                        reconnects by itself within about 20 seconds of
                        waking — there is nothing to click here, and no
                        button could do it: this page never opens a
                        connection to a machine.
                        <br />
                        <strong>2. Is the agent running there?</strong> Most
                        likely if it has never been seen more than once. On
                        that machine:
                        <Box component="pre" sx={{ fontSize: 11, my: 0.5,
                                                   whiteSpace: 'pre-wrap' }}>
{`# Windows
cd $env:USERPROFILE\\bitport; .\\.venv\\Scripts\\python.exe node_agent.py

# Linux / macOS
systemctl --user status bitport-node   # or: ./.venv/bin/python node_agent.py`}
                        </Box>
                        <strong>3. Can it still reach here?</strong> The
                        agent logs <code>coordinator unreachable</code> each
                        poll when it cannot — check{' '}
                        <code>node_agent_run.log</code> beside its install.
                      </Typography>
                      <Typography variant="caption" color="text.secondary">
                        If none of that explains it, re-running the joiner
                        with a fresh code is safe: it keeps the keys and the
                        ledger, and registers the agent to start on its own
                        from then on.
                      </Typography>
                    </Box>
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
