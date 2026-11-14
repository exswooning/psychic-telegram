import React, { useCallback, useEffect, useRef, useState } from 'react'
import { Alert, Box, Button, Chip, CircularProgress, Stack, Typography } from '@mui/material'
import { fetchMoveProbe, startMoveProbe } from '@/api/controlPlane'
import type { MoveProbe, MoveProbeResult } from '@/api/controlPlane'

const ROUTE_LABEL: Record<string, string> = {
  direct: 'Straight into a target shared drive',
  'source-drive': 'Through a source shared drive, taken out by the target',
}

const state = (r: MoveProbeResult) =>
  r.ok === true ? { label: 'works', color: 'success' as const }
  : r.ok === false ? { label: 'refused', color: 'error' as const }
  : { label: 'not tried', color: 'default' as const }

/**
 * Tries to move a one-byte file between the two tenants through a shared drive, by each route a
 * move could use, and says where Google stopped it. "Move the files themselves" is offered only
 * after the direct route has passed (onAvailable), because that is the route the engine moves by.
 */
const MoveRouteProbe: React.FC<{ accountId?: number; onAvailable: (ok: boolean) => void }> = ({ accountId, onAvailable }) => {
  const [probe, setProbe] = useState<MoveProbe | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const startedAt = useRef(0)

  const load = useCallback(async () => {
    try {
      const p = await fetchMoveProbe(accountId)
      setProbe(p)
      onAvailable(p.moveAvailable)
      return p
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      return null
    }
  }, [accountId, onAvailable])

  useEffect(() => { void load() }, [load])

  // A probe takes about a minute (it makes and deletes shared drives): read it until it is newer
  // than the click, for at most three minutes.
  useEffect(() => {
    if (!busy) return
    const t = setInterval(async () => {
      const p = await load()
      const fresh = p?.results.some((r) => Date.parse(r.testedAt) >= startedAt.current - 2000)
      if (fresh || Date.now() - startedAt.current > 180_000) setBusy(false)
    }, 5000)
    return () => clearInterval(t)
  }, [busy, load])

  const run = async () => {
    setError('')
    try {
      const r = await startMoveProbe('test whether files can move between the two tenants', accountId)
      if (!r.ok) { setError(r.detail || 'could not start'); return }
      startedAt.current = Date.now()
      setBusy(true)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <Box sx={{ mt: 1.5 }} data-testid="move-probe">
      <Stack direction="row" spacing={1} alignItems="center">
        <Typography variant="subtitle2" sx={{ fontWeight: 600 }}>Can files move between these tenants?</Typography>
        <Button size="small" variant="outlined" onClick={run} disabled={busy} data-testid="move-probe-run">
          {busy ? 'Testing…' : probe?.results.length ? 'Test again' : 'Test the move route'}
        </Button>
        {busy && <CircularProgress size={16} />}
      </Stack>
      <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 0.5 }}>
        Moves a one-byte file through a shared drive on each route and deletes everything after. Needs the
        source to be a declared sandbox: a protected source is never written to.
      </Typography>
      {error && <Alert severity="error" sx={{ my: 1 }}>{error}</Alert>}
      {probe && !probe.results.length && !busy && (
        <Typography variant="body2" color="text.secondary" data-testid="move-probe-none">Not tested yet for {probe.pair}.</Typography>
      )}
      {probe?.results.map((r) => (
        <Box key={r.route} sx={{ mb: 0.75 }} data-testid={`move-probe-${r.route}`}>
          <Stack direction="row" spacing={1} alignItems="center">
            <Chip size="small" label={state(r).label} color={state(r).color} />
            <Typography variant="body2">{ROUTE_LABEL[r.route] ?? r.route}</Typography>
          </Stack>
          {r.ok !== true && (
            <Typography variant="caption" color="text.secondary" sx={{ display: 'block', pl: 1 }}>
              {r.step}: {r.detail}
            </Typography>
          )}
        </Box>
      ))}
      {probe?.moveAvailable && (
        <Alert severity="success" sx={{ mt: 1 }} data-testid="move-probe-available">
          A file moved. "Move the files themselves" is available below.
        </Alert>
      )}
      {probe?.otherRouteOnly && (
        <Alert severity="info" sx={{ mt: 1 }} data-testid="move-probe-other-only">
          Only the shared-drive route worked. A move run moves straight into a target shared drive, so it
          stays unavailable until the engine can use the other route.
        </Alert>
      )}
    </Box>
  )
}

export default MoveRouteProbe
