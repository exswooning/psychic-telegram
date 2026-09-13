/**
 * The 2-Step code, on the page, so nobody has to find a phone.
 *
 * The setup wizard drives a real browser through Google's sign-in and a
 * 2-Step prompt stops it dead -- the installer's own note says the sign-in
 * cannot answer it. Until now the UI could only DISPLAY the challenge
 * ("check your phone, tap 47") and tell the operator to go and deal with
 * it, which is the whole reason unattended setup was not unattended.
 *
 * The seconds remaining are shown as loudly as the code. A code with two
 * seconds left is rejected by the time it is typed, and a UI that shows
 * only the digits invites exactly that mistake.
 */
import React, { useCallback, useEffect, useState } from 'react'
import {
  Alert, Box, Button, CircularProgress, Paper, Stack, TextField, Typography,
} from '@mui/material'
import { ContentCopy as CopyIcon, Check as CheckIcon } from '@mui/icons-material'
import { fetchMfaCode, storeMfaSecret } from '@/api/controlPlane'
import type { MfaCode } from '@/api/controlPlane'

export const AuthenticatorCode: React.FC<{ email?: string }> = ({ email = '' }) => {
  const [who, setWho] = useState(email)
  const [st, setSt] = useState<MfaCode | null>(null)
  const [err, setErr] = useState('')
  const [secret, setSecret] = useState('')
  const [copied, setCopied] = useState(false)
  const [left, setLeft] = useState(0)

  const refresh = useCallback((target: string) => {
    if (!target) return
    fetchMfaCode(target)
      .then((r) => { setSt(r); setLeft(r.secondsRemaining); setErr(r.error || '') })
      .catch((e) => setErr(e instanceof Error ? e.message : String(e)))
  }, [])

  useEffect(() => { refresh(who) }, [who, refresh])

  useEffect(() => {
    // Tick locally, and re-fetch when the window rolls. Polling every second
    // would ask the server for the same six digits thirty times over.
    const t = window.setInterval(() => {
      setLeft((v) => {
        if (v <= 1) { refresh(who); return st?.period || 30 }
        return v - 1
      })
    }, 1000)
    return () => window.clearInterval(t)
  }, [who, refresh, st?.period])

  const period = st?.period || 30
  const frac = Math.max(0, Math.min(1, left / period))

  return (
    <Paper variant="outlined" sx={{ p: 2 }} data-testid="authenticator">
      <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1 }}>
        Authenticator code
      </Typography>

      <TextField select size="small" label="Account" sx={{ minWidth: 300, mb: 2 }}
                 value={who} onChange={(e) => setWho(e.target.value)}
                 SelectProps={{ native: true }}
                 inputProps={{ 'data-testid': 'mfa-account' }}>
        <option value="">select an account…</option>
        {(st?.accounts || []).map((a) => <option key={a} value={a}>{a}</option>)}
      </TextField>

      {st?.code ? (
        <Stack direction="row" spacing={2} alignItems="center" sx={{ mb: 1 }}>
          <Typography data-testid="mfa-code"
                      sx={{ fontSize: 40, fontWeight: 700, letterSpacing: 6,
                            fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
                            color: left <= 5 ? 'warning.main' : 'text.primary' }}>
            {st.code.slice(0, 3)} {st.code.slice(3)}
          </Typography>
          <Box sx={{ position: 'relative', display: 'inline-flex' }}>
            <CircularProgress variant="determinate" value={frac * 100} size={44}
                              color={left <= 5 ? 'warning' : 'primary'} />
            <Box sx={{ position: 'absolute', inset: 0, display: 'grid',
                       placeItems: 'center' }}>
              <Typography variant="caption" data-testid="mfa-seconds"
                          sx={{ fontVariantNumeric: 'tabular-nums' }}>
                {left}
              </Typography>
            </Box>
          </Box>
          <Button size="small" startIcon={copied ? <CheckIcon /> : <CopyIcon />}
                  data-testid="mfa-copy"
                  onClick={() => {
                    navigator.clipboard?.writeText(st.code)
                    setCopied(true); window.setTimeout(() => setCopied(false), 1500)
                  }}>
            {copied ? 'Copied' : 'Copy'}
          </Button>
        </Stack>
      ) : null}

      {left <= 5 && st?.code ? (
        <Alert severity="warning" sx={{ mb: 1 }} data-testid="mfa-expiring">
          Expires in {left}s — wait for the next one rather than typing this.
        </Alert>
      ) : null}

      {err && <Alert severity="info" sx={{ mb: 1 }} data-testid="mfa-error">{err}</Alert>}

      {(!st?.accounts?.length || err) && (
        <Box sx={{ mt: 1 }}>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
            Paste the setup key from Google&apos;s 2-Step page — the spaced
            lowercase form, or the whole <code>otpauth://</code> URI. Stored
            in a root-only file, never in the database, because that gets
            copied to worker nodes and taken in backups.
          </Typography>
          <Stack direction="row" spacing={1}>
            <TextField size="small" label="Account email" value={who}
                       onChange={(e) => setWho(e.target.value)} sx={{ flex: 1 }}
                       inputProps={{ 'data-testid': 'mfa-new-email' }} />
            <TextField size="small" label="Setup key" value={secret}
                       onChange={(e) => setSecret(e.target.value)} sx={{ flex: 1 }}
                       inputProps={{ 'data-testid': 'mfa-new-secret' }} />
            <Button size="small" variant="contained" data-testid="mfa-save"
                    disabled={!who.trim() || !secret.trim()}
                    onClick={() => storeMfaSecret(who, secret)
                      .then(() => { setSecret(''); setErr(''); refresh(who) })
                      .catch((e) => setErr(e instanceof Error ? e.message : String(e)))}>
              Save
            </Button>
          </Stack>
          <Alert severity="warning" sx={{ mt: 1.5 }} data-testid="mfa-tradeoff">
            A seed stored here, beside the password, is <strong>one factor,
            not two</strong> — anything that can read both is a single point
            of total compromise. Worth it for an admin account that exists to
            be automated; not for anyone&apos;s everyday account.
          </Alert>
        </Box>
      )}
    </Paper>
  )
}

export default AuthenticatorCode
