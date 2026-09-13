/**
 * The countdown, and the button that does not wait for it.
 *
 * The timer exists for when nobody CAN press the button. If the owner is
 * present and wants the material gone -- a stolen laptop, a co-administrator
 * who should no longer have root -- then waiting out a deadline is the
 * wrong behaviour, and so is making them find an SSH client.
 *
 * Every number here is shown with the signal it came from. A countdown that
 * says "18h left" without saying what reset it is asking to be trusted
 * about the most destructive automation on the machine.
 */
import React, { useCallback, useEffect, useState } from 'react'
import {
  Alert, Box, Button, Chip, Dialog, DialogActions, DialogContent,
  DialogTitle, LinearProgress, Paper, Stack, TextField, Typography,
} from '@mui/material'
import {
  Timer as TimerIcon, DeleteForever as WipeIcon,
  Favorite as AliveIcon,
} from '@mui/icons-material'
import { fetchDeadman, deadmanWipeNow, deadmanTouch } from '@/api/controlPlane'
import type { DeadmanStatus } from '@/api/controlPlane'

const hms = (secs: number): string => {
  const s = Math.max(0, secs)
  const d = Math.floor(s / 86400)
  const h = Math.floor((s % 86400) / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = Math.floor(s % 60)
  return d > 0 ? `${d}d ${h}h ${m}m` : `${h}h ${m}m ${String(sec).padStart(2, '0')}s`
}

const ago = (secs: number | null): string =>
  secs === null ? 'never' : hms(secs) + ' ago'

export const Deadman: React.FC = () => {
  const [st, setSt] = useState<DeadmanStatus | null>(null)
  const [err, setErr] = useState('')
  const [left, setLeft] = useState<number | null>(null)
  const [open, setOpen] = useState(false)
  const [typed, setTyped] = useState('')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [done, setDone] = useState<string[] | null>(null)

  const refresh = useCallback(() => {
    fetchDeadman()
      .then((s) => { setSt(s); setLeft(s.secondsRemaining); setErr('') })
      .catch((e) => setErr(e instanceof Error ? e.message : String(e)))
  }, [])

  useEffect(() => {
    refresh()
    // Re-read from the server every 30s; tick locally every second so the
    // countdown moves. A number that only changes every 30 seconds reads as
    // broken on exactly the screen where it must read as trustworthy.
    const poll = window.setInterval(refresh, 30000)
    const tick = window.setInterval(
      () => setLeft((v) => (v === null ? v : v - 1)), 1000)
    return () => { window.clearInterval(poll); window.clearInterval(tick) }
  }, [refresh])

  const fire = () => {
    setBusy(true)
    deadmanWipeNow(reason)
      .then((r) => { setDone(r.removed); setOpen(false); refresh() })
      .catch((e) => setErr(e instanceof Error ? e.message : String(e)))
      .finally(() => setBusy(false))
  }

  const frac = st && st.days && left !== null
    ? Math.min(1, Math.max(0, 1 - left / (st.days * 86400))) : 0

  return (
    <Box sx={{ p: 3 }}>
      <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.5 }}>
        <TimerIcon color="action" />
        <Typography variant="h5" sx={{ fontWeight: 700 }}>Dead man switch</Typography>
      </Stack>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        Destroys this machine&apos;s credentials and tenant data if nobody has
        been here for too long. Any sign of life resets it — signing in here
        counts.
      </Typography>

      {err && <Alert severity="error" sx={{ mb: 2 }} data-testid="deadman-error">{err}</Alert>}
      {done && (
        <Alert severity="warning" sx={{ mb: 2 }} data-testid="wipe-done">
          Wiped. {done.length} item(s) removed.
        </Alert>
      )}

      {st && (
        <Paper variant="outlined" sx={{ p: 2, mb: 3 }}>
          <Stack direction="row" spacing={2} alignItems="center" sx={{ mb: 1 }}>
            <Chip size="small" data-testid="deadman-state"
                  color={st.armed ? 'error' : 'default'}
                  variant={st.armed ? 'filled' : 'outlined'}
                  label={st.armed ? 'ARMED' : st.state} />
            {!st.emailConfigured && (
              <Chip size="small" color="warning" variant="outlined"
                    data-testid="no-email"
                    label="no email configured — warnings go to the log only" />
            )}
          </Stack>

          {st.armed && left !== null ? (
            <>
              <Typography data-testid="countdown"
                          sx={{ fontSize: 44, fontWeight: 700, letterSpacing: 1,
                                fontVariantNumeric: 'tabular-nums',
                                color: left < 3600 ? 'error.main'
                                  : left < 21600 ? 'warning.main' : 'text.primary' }}>
                {hms(left)}
              </Typography>
              <LinearProgress variant="determinate" value={frac * 100}
                              color={frac > 0.9 ? 'error' : frac > 0.5 ? 'warning' : 'primary'}
                              sx={{ height: 8, borderRadius: 1, my: 1 }} />
              <Typography variant="body2" color="text.secondary">
                until everything below is destroyed. Last sign of life:{' '}
                <strong>{st.newestSignal}</strong>, {ago(st.secondsSinceSeen)}.
              </Typography>
              {/* Explicit, because being signed in is not a signal: a
                  session is created at LOGIN, so someone already signed in
                  could watch this reach zero while looking at it. Not fired
                  on page load either -- a forgotten open tab must not hold
                  the switch open indefinitely. */}
              <Button size="small" variant="outlined" sx={{ mt: 1.5 }}
                      startIcon={<AliveIcon />} data-testid="deadman-touch"
                      onClick={() => deadmanTouch().then(refresh).catch(
                        (e) => setErr(e instanceof Error ? e.message : String(e)))}>
                I&apos;m here — reset the timer
              </Button>
            </>
          ) : (
            <Typography variant="body2" color="text.secondary"
                        data-testid="not-counting">
              Not counting down — {st.state}. Nothing will be destroyed.
            </Typography>
          )}
        </Paper>
      )}

      {st && (
        <Paper variant="outlined" sx={{ p: 2, mb: 3 }}>
          <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1 }}>
            What counts as a sign of life
          </Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
            The newest of these wins. Keyed on all of them because
            <code> last</code> alone misses every deploy — an SSH command with
            no terminal leaves no login record, and this host has a measured
            13-day gap in logins during weeks it was worked on daily.
          </Typography>
          <Stack spacing={0.5}>
            {Object.entries(st.signals)
              .sort((a, b) => (a[1] ?? 9e9) - (b[1] ?? 9e9))
              .map(([name, secs]) => (
                <Stack key={name} direction="row" spacing={2}
                       data-testid={`signal-${name.replace(/\s/g, '-')}`}>
                  <Typography variant="body2" sx={{ minWidth: 160 }}>{name}</Typography>
                  <Typography variant="body2" color="text.secondary"
                              sx={{ fontVariantNumeric: 'tabular-nums' }}>
                    {ago(secs)}
                  </Typography>
                </Stack>
              ))}
          </Stack>
        </Paper>
      )}

      <Paper variant="outlined" sx={{ p: 2, borderColor: 'error.main' }}>
        <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1 }}>
          Wipe now
        </Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
          Does not wait for the countdown. For when you are here and want the
          material gone — a stolen laptop, a co-administrator who should no
          longer have root.
        </Typography>
        {st?.targets?.length ? (
          <Box component="pre" data-testid="wipe-targets"
               sx={{ fontSize: 11, p: 1.5, bgcolor: 'action.hover', m: 0, mb: 1.5,
                     borderRadius: 1, whiteSpace: 'pre-wrap' }}>
            {st.targets.join('\n')}
          </Box>
        ) : null}
        <Button variant="outlined" color="error" startIcon={<WipeIcon />}
                onClick={() => setOpen(true)} data-testid="wipe-now">
          Wipe now
        </Button>
      </Paper>

      <Dialog open={open} onClose={() => setOpen(false)}>
        <DialogTitle>Destroy credentials and tenant data?</DialogTitle>
        <DialogContent>
          <Alert severity="error" sx={{ mb: 2 }}>
            Permanent. This removes the service-account keys for every tenant,
            the admin password, the migration ledgers and the backups. Nothing
            resumes afterwards.
          </Alert>
          <TextField fullWidth size="small" sx={{ mb: 2 }} label="Reason"
                     value={reason} onChange={(e) => setReason(e.target.value)}
                     inputProps={{ 'data-testid': 'wipe-reason' }} />
          <TextField fullWidth size="small" label="Type WIPE to confirm"
                     value={typed} onChange={(e) => setTyped(e.target.value)}
                     inputProps={{ 'data-testid': 'wipe-confirm' }} />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setOpen(false)}>Cancel</Button>
          <Button color="error" variant="contained" disabled={typed !== 'WIPE' || busy}
                  onClick={fire} data-testid="wipe-go">
            {busy ? 'Wiping…' : 'Wipe permanently'}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  )
}

export default Deadman
