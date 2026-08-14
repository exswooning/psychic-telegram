import React, { useState } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import {
  Box, Paper, Typography, TextField, Button, Stack, Alert, Link,
} from '@mui/material'
import { RocketLaunch as BrandIcon, CheckCircle as CheckIcon } from '@mui/icons-material'
import { login } from '@/api/controlPlane'

const Login: React.FC = () => {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setBusy(true)
    try {
      await login(email.trim(), password)
      // A hard navigation, not react-router's navigate(): App.tsx's own
      // `account` state is only fetched once, on mount -- a client-side
      // route change to /mission-control leaves it still null, so its own
      // route guard (`if (!account && !isPublic)`) immediately bounces
      // straight back to /login before the new session is ever seen. A
      // full page load remounts App.tsx and re-runs fetchMe() against the
      // now-valid cookie instead.
      //
      // /app prefix required: window.location.href is a raw browser
      // navigation, not routed through react-router's basename="/app"
      // (main.tsx) the way <Navigate>/navigate() are -- webui.py's own
      // top-level routing only serves the SPA under /app and /app/*, so
      // the bare path 404s at the server before React ever sees it.
      window.location.href = '/app/mission-control'
    } catch (err: any) {
      setError(err.message || 'sign in failed')
    } finally {
      // Cleared on every path, success or failure -- the field never holds
      // a password that has already been sent.
      setPassword('')
      setBusy(false)
    }
  }

  return (
    <Box sx={{
      minHeight: '100vh', width: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center',
      p: 3,
      background: (t) => t.palette.mode === 'dark'
        ? t.palette.background.default
        : `radial-gradient(760px 420px at 15% 12%, ${t.palette.primary.light}33 0%, transparent 60%),
           radial-gradient(640px 420px at 85% 88%, ${t.palette.secondary.light}55 0%, transparent 55%),
           ${t.palette.background.default}`,
    }}>
      <Paper variant="outlined" sx={{
        display: 'flex', width: '100%', maxWidth: 880, minHeight: 460,
        borderRadius: 4, overflow: 'hidden',
      }}>
        <Box sx={{
          flex: 1, p: 5, display: { xs: 'none', sm: 'flex' }, flexDirection: 'column', justifyContent: 'space-between',
          bgcolor: 'primary.main', color: 'primary.contrastText', position: 'relative', overflow: 'hidden',
        }}>
          <Box sx={{
            position: 'absolute', right: -60, bottom: -60, width: 260, height: 260, borderRadius: '50%',
            bgcolor: 'rgba(255,255,255,.08)',
          }} />
          <Stack direction="row" spacing={1.5} alignItems="center" sx={{ position: 'relative' }}>
            <Box sx={{
              width: 36, height: 36, borderRadius: '50%', bgcolor: 'rgba(255,255,255,.16)',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
            }}>
              <BrandIcon sx={{ fontSize: 20 }} />
            </Box>
            <Typography sx={{ fontFamily: '"Plus Jakarta Sans", sans-serif', fontWeight: 700, fontSize: 18 }}>
              Bitport
            </Typography>
          </Stack>
          <Box sx={{ position: 'relative' }}>
            <Typography variant="h4" sx={{ fontWeight: 700, maxWidth: '22ch', color: 'inherit', mb: 1.5 }}>
              Move a Workspace tenant with proof, not promises.
            </Typography>
            <Typography variant="body2" sx={{ color: 'rgba(255,255,255,.85)', maxWidth: '32ch' }}>
              Provision tenants, run migrations, and verify every file and permission that lands — before you call it done.
            </Typography>
          </Box>
          <Stack spacing={1.25} sx={{ position: 'relative' }}>
            {[
              'Functional OAuth scope verification, not a checkbox',
              'Every write action attributed and logged with a reason',
              'Resumable, per-service migration ledger',
            ].map((line) => (
              <Stack key={line} direction="row" spacing={1} alignItems="flex-start">
                <CheckIcon sx={{ fontSize: 16, mt: 0.25, color: 'rgba(255,255,255,.85)' }} />
                <Typography variant="caption" sx={{ color: 'rgba(255,255,255,.85)' }}>{line}</Typography>
              </Stack>
            ))}
          </Stack>
        </Box>

        <Box sx={{ flex: 1, p: 5, display: 'flex', flexDirection: 'column', justifyContent: 'center', gap: 2.5 }}>
          <Box>
            <Typography variant="h4" sx={{ fontWeight: 700 }}>Sign in</Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
              Enter your Bitport account to open the console.
            </Typography>
          </Box>

          {error && <Alert severity="error" onClose={() => setError('')}>{error}</Alert>}

          <Box component="form" onSubmit={handleSubmit} sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
            <TextField
              label="Email" type="email" placeholder="you@company.com" value={email}
              onChange={(e) => setEmail(e.target.value)} autoFocus fullWidth
              autoComplete="username"
            />
            <TextField
              label="Password" type="password" value={password}
              onChange={(e) => setPassword(e.target.value)} fullWidth
              autoComplete="current-password"
            />
            <Button type="submit" variant="contained" size="large" sx={{ py: 1.25 }}
                    disabled={busy || !email.trim() || !password}>
              {busy ? 'Signing in…' : 'Sign in'}
            </Button>
          </Box>

          <Typography variant="body2" color="text.secondary">
            New to Bitport?{' '}
            <Link component={RouterLink} to="/signup" underline="hover">Create an account</Link>
            {' · '}
            <Link component={RouterLink} to="/pricing" underline="hover">View pricing</Link>
          </Typography>
        </Box>
      </Paper>
    </Box>
  )
}

export default Login
