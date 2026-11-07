import React, { useCallback, useEffect, useState } from 'react'
import { Alert, Box, Button, Card, CardContent, Chip, Stack, TextField, Typography } from '@mui/material'
import { experimentAction, fetchExperiments, type ExperimentRow } from '@/api/controlPlane'

/** An experiment's logs, harvested from the UI: bookmark every log and the ledger before a run,
 *  then fold what the run wrote into one page -- failures by kind, and what Bitport has not learned
 *  yet (run_digest.py). Reads only; it never touches a tenant. */
const ExperimentPanel: React.FC<{ accountId: number }> = ({ accountId }) => {
  const [rows, setRows] = useState<ExperimentRow[]>([])
  const [name, setName] = useState('full45-r1')
  const [open, setOpen] = useState<string | null>(null)
  const [digest, setDigest] = useState<string | null>(null)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const load = useCallback(() => {
    Promise.resolve().then(() => fetchExperiments(accountId, open ?? undefined))
      .then((r) => { setRows(r?.experiments ?? []); setDigest(r?.digest ?? null) }).catch(() => {})
  }, [accountId, open])
  useEffect(() => { load() }, [load])
  const building = rows.some((r) => r.building)
  useEffect(() => {
    if (!building) return
    const t = setInterval(load, 15_000)
    return () => clearInterval(t)
  }, [building, load])
  const act = (n: string, action: 'start' | 'digest') =>
    experimentAction(n, action, accountId, `experiment ${n}: ${action === 'start' ? 'bookmark logs' : 'build digest'}`)
      .then((r) => { setMsg({ ok: r.ok, text: r.detail }); if (action === 'digest') setOpen(n); load() })
      .catch((e: unknown) => setMsg({ ok: false, text: e instanceof Error ? e.message : String(e) }))

  return (
    <Card variant="outlined" sx={{ mb: 3 }} data-testid="experiment-panel">
      <CardContent>
        <Typography variant="h6" sx={{ fontWeight: 700 }}>Experiment log harvest</Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
          Bookmark before a run; build the digest after. It reads only what the run wrote since the bookmark.
        </Typography>
        <Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap', rowGap: 1, alignItems: 'center' }}>
          <TextField size="small" label="Experiment name" value={name} onChange={(e) => setName(e.target.value)}
                     inputProps={{ 'data-testid': 'experiment-name' }} />
          <Button variant="outlined" onClick={() => act(name, 'start')} data-testid="experiment-bookmark"
                  disabled={!/^[a-z0-9][a-z0-9-]{0,39}$/.test(name)}>Bookmark logs now</Button>
        </Stack>
        {msg && <Alert severity={msg.ok ? 'success' : 'error'} sx={{ mt: 1.5 }}>{msg.text}</Alert>}
        <Stack spacing={1} sx={{ mt: 2 }}>
          {rows.map((r) => (
            <Stack key={r.name} direction="row" spacing={1} sx={{ alignItems: 'center', flexWrap: 'wrap', rowGap: 1 }}
                   data-testid={`experiment-${r.name}`}>
              <Typography variant="body2" sx={{ fontWeight: 600, minWidth: 120 }}>{r.name}</Typography>
              <Typography variant="caption" color="text.secondary">
                bookmarked {r.startedAt ? new Date(r.startedAt).toLocaleString() : '-'}
              </Typography>
              {r.building ? <Chip size="small" color="info" label="building digest…" />
                : r.digestAt ? <Chip size="small" label={`digest ${new Date(r.digestAt).toLocaleString()}`} /> : null}
              <Button size="small" onClick={() => act(r.name, 'digest')} disabled={r.building}>Build digest</Button>
              {r.digestAt && <Button size="small" onClick={() => setOpen(open === r.name ? null : r.name)}>
                {open === r.name ? 'Hide' : 'View'}</Button>}
            </Stack>
          ))}
        </Stack>
        {open && digest && (
          <Box component="pre" data-testid="experiment-digest"
               sx={{ mt: 2, p: 1.5, maxHeight: 520, overflow: 'auto', fontSize: 12, bgcolor: 'action.hover',
                     borderRadius: 1, whiteSpace: 'pre-wrap' }}>{digest}</Box>
        )}
      </CardContent>
    </Card>
  )
}

export default ExperimentPanel
