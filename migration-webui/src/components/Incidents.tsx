/**
 * What the run watcher found wrong, with the hand-off for fixing it.
 *
 * The watcher (run_watch.py) opens an incident when a run crashes, exits clean
 * but fails its benchmarks, starts failing in bulk, or stalls. Each carries a
 * brief -- the failing checks, the error families and the log tail -- written
 * to be pasted straight into Claude Code. Nothing here fixes anything by
 * itself: a fix is a deploy, and a deploy can kill a running seed or
 * migration, so the person decides when.
 *
 * Reachable with nothing running: incidents are stored, not live.
 */
import React, { useCallback, useEffect, useState } from 'react'
import {
  Alert, Box, Button, Chip, CircularProgress, FormControlLabel, Paper, Stack, Switch, Typography,
} from '@mui/material'
import { fetchIncidentBrief, fetchIncidents, setIncidentStatus } from '@/api/controlPlane'
import type { Incident, IncidentStatus } from '@/api/controlPlane'

const SEVERITY: Record<Incident['severity'], 'error' | 'warning' | 'info'> = {
  error: 'error', warn: 'warning', info: 'info',
}
const POLL_MS = 30_000

const when = (iso: string) => {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString()
}

export const Incidents: React.FC = () => {
  const [items, setItems] = useState<Incident[] | null>(null)
  const [error, setError] = useState('')
  const [showResolved, setShowResolved] = useState(false)
  const [note, setNote] = useState('')
  // A brief that could not be copied is shown instead of lost.
  const [brief, setBrief] = useState<{ id: number; text: string } | null>(null)

  const load = useCallback(() => {
    fetchIncidents()
      .then((r) => { setItems(r.incidents); setError('') })
      .catch((e) => { setItems((v) => v ?? []); setError(e instanceof Error ? e.message : String(e)) })
  }, [])
  useEffect(() => {
    load()
    const t = setInterval(load, POLL_MS)
    return () => clearInterval(t)
  }, [load])

  const move = async (id: number, status: IncidentStatus) => {
    setError('')
    try { await setIncidentStatus(id, status); load() }
    catch (e) { setError(e instanceof Error ? e.message : String(e)) }
  }

  const copy = async (id: number) => {
    setError(''); setNote(''); setBrief(null)
    try {
      const text = await fetchIncidentBrief(id)
      try {
        await navigator.clipboard.writeText(text)
        setNote('Brief copied — paste it into Claude Code.')
      } catch {
        setBrief({ id, text })
      }
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
  }

  const shown = (items ?? []).filter((i) => showResolved || i.status !== 'resolved')
  const open = (items ?? []).filter((i) => i.status === 'open').length

  return (
    <Paper variant="outlined" sx={{ p: 2, mb: 3 }} data-testid="incidents">
      <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 0.5 }}>
        <Typography variant="h6" sx={{ fontWeight: 700, flexGrow: 1 }}>
          Incidents{open > 0 && <Chip size="small" color="error" label={`${open} open`} sx={{ ml: 1 }}
                                      data-testid="incidents-open" />}
        </Typography>
        <FormControlLabel label="Show resolved" control={
          <Switch size="small" checked={showResolved} onChange={(e) => setShowResolved(e.target.checked)}
                  inputProps={{ 'aria-label': 'Show resolved' }} />} />
      </Stack>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5, maxWidth: 780 }}>
        Recorded when a run crashes, finishes but fails its benchmarks, starts failing in bulk, or
        stalls. <strong>Copy brief</strong> puts everything Claude Code needs on your clipboard.
        Nothing is fixed or deployed on its own — a deploy can kill a running seed or migration.
      </Typography>

      {error && <Alert severity="warning" sx={{ mb: 1.5 }}>{error}</Alert>}
      {note && <Alert severity="success" sx={{ mb: 1.5 }} onClose={() => setNote('')}>{note}</Alert>}
      {brief && (
        <Alert severity="info" sx={{ mb: 1.5 }} onClose={() => setBrief(null)} data-testid="brief-text">
          The browser would not copy it — select and copy this:
          <Box component="pre" sx={{ m: 0, mt: 1, maxHeight: 260, overflow: 'auto', fontSize: 12, whiteSpace: 'pre-wrap' }}>
            {brief.text}
          </Box>
        </Alert>
      )}

      {items === null && <CircularProgress size={18} />}
      {items !== null && shown.length === 0 && !error && (
        <Typography variant="body2" color="text.secondary" data-testid="no-incidents">
          No problems recorded. The watcher opens one here the moment a run crashes or fails its checks.
        </Typography>
      )}

      <Stack spacing={1}>
        {shown.map((i) => (
          <Stack key={i.id} direction={{ xs: 'column', md: 'row' }} spacing={1.5} alignItems={{ md: 'center' }}
                 data-testid={`incident-${i.id}`}
                 sx={{ p: 1.25, border: '1px solid', borderColor: 'divider', borderRadius: 1,
                       opacity: i.status === 'resolved' ? 0.6 : 1 }}>
            <Chip label={i.status === 'open' ? i.severity : i.status} size="small" color={i.status === 'open' ? SEVERITY[i.severity] : 'default'}
                  sx={{ fontWeight: 700, minWidth: 96 }} data-testid={`incident-status-${i.id}`} />
            <Box sx={{ flexGrow: 1, minWidth: 0 }}>
              <Typography variant="body2" sx={{ fontWeight: 600 }}>{i.title}</Typography>
              <Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>
                {i.job_name ?? 'job'}{i.account_id ? ` · account #${i.account_id}` : ''} · {when(i.last_seen_at)}
                {i.occurrences > 1 && ` · seen ${i.occurrences} times`}
              </Typography>
              {i.summary && (
                <Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>{i.summary}</Typography>
              )}
            </Box>
            <Stack direction="row" spacing={0.75}>
              <Button size="small" variant="outlined" onClick={() => copy(i.id)}>Copy brief</Button>
              {i.status === 'open' && (
                <Button size="small" onClick={() => move(i.id, 'acknowledged')}>Acknowledge</Button>)}
              {i.status !== 'resolved' && (
                <Button size="small" onClick={() => move(i.id, 'resolved')}>Resolve</Button>)}
              {i.status === 'resolved' && (
                <Button size="small" onClick={() => move(i.id, 'open')}>Reopen</Button>)}
            </Stack>
          </Stack>
        ))}
      </Stack>
    </Paper>
  )
}

export default Incidents
