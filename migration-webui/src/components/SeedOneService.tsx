/**
 * Top up one service on a corpus that already exists.
 *
 * A twelve-hour seed produced 126 identical warnings -- one per user --
 * because the Chat app was not configured, so every chat call 404'd and
 * every user finished with "0 chat messages in 0 spaces". Everything else
 * in that corpus is good. The only way to recover chat was to seed all of
 * it again, which is twelve hours to fix one service.
 *
 * So: pick a service, type the domain, and the seeder runs just that one
 * across the users already there. Same typed-domain gate as every other
 * thing that writes into a tenant -- this writes real (fabricated) data,
 * and the mistake worth catching is aiming it at the wrong one of two
 * configured domains.
 */
import React, { useState } from 'react'
import {
  Alert, Box, Button, Dialog, DialogActions, DialogContent, DialogTitle,
  MenuItem, Stack, TextField, Typography,
} from '@mui/material'
import { Grass as SeedIcon } from '@mui/icons-material'
import { runSeed } from '@/api/client'

/** seed_sandbox.SEEDABLE. The server validates against the seeder's own
 *  list and refuses anything else by name, so a drift here is a rejected
 *  request rather than a job that starts and dies. */
export const SERVICES = ['drive', 'gmail', 'calendar', 'chat',
                         'contacts', 'tasks'] as const

export const SeedOneService: React.FC<{
  domain: string
  onStarted?: () => void
}> = ({ domain, onStarted }) => {
  const [open, setOpen] = useState(false)
  const [service, setService] = useState<string>('chat')
  const [typed, setTyped] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [done, setDone] = useState('')

  const matches = typed.trim().toLowerCase() === domain.toLowerCase()

  const go = async () => {
    setBusy(true); setError('')
    try {
      const r = await runSeed(domain, 'small', false, false, { only: service })
      if (!r.ok) throw new Error(r.error || 'could not start')
      setDone(r.queued
        ? (r.msg || 'the box is busy — queued, it will start on its own')
        : `seeding ${service} across the existing users`)
      setOpen(false)
      onStarted?.()
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <Button size="small" variant="outlined" startIcon={<SeedIcon />}
              data-testid={`seed-one-${domain}`}
              onClick={() => { setOpen(true); setTyped(''); setError(''); setDone('') }}>
        Seed one service
      </Button>
      {done && (
        <Typography variant="caption" color="success.main"
                    sx={{ display: 'block', mt: 0.5 }}>
          {done}
        </Typography>
      )}

      <Dialog open={open} onClose={() => !busy && setOpen(false)}
              maxWidth="sm" fullWidth>
        <DialogTitle>Seed one service — {domain}</DialogTitle>
        <DialogContent>
          <Alert severity="info" sx={{ mb: 2 }}>
            Runs the seeder across the users already in this tenant, writing
            only the service you pick. Everything else is skipped and reported
            as such, so a partial pass cannot be mistaken for a full one.
          </Alert>
          <Stack spacing={2}>
            <TextField select fullWidth size="small" label="Service"
                       value={service}
                       onChange={(e) => setService(e.target.value)}
                       inputProps={{ 'data-testid': 'seed-one-service' }}>
              {SERVICES.map((s) => (
                <MenuItem key={s} value={s}>{s}</MenuItem>
              ))}
            </TextField>
            {service === 'drive' && (
              <Alert severity="warning">
                Drive is the one service that builds rather than tops up.
                Running it on a tenant that already has a corpus adds a second
                one — the counts afterwards will not match the manifest.
              </Alert>
            )}
            <TextField fullWidth size="small"
                       label={`Type ${domain} to confirm`}
                       value={typed} onChange={(e) => setTyped(e.target.value)}
                       inputProps={{ 'data-testid': 'seed-one-confirm' }} />
            {error && <Alert severity="error">{error}</Alert>}
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setOpen(false)} disabled={busy}>Cancel</Button>
          <Button variant="contained" data-testid="seed-one-go"
                  disabled={!matches || busy} onClick={go}>
            {busy ? 'Starting…' : `Seed ${service}`}
          </Button>
        </DialogActions>
      </Dialog>
    </>
  )
}

export default SeedOneService
