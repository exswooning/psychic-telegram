import React, { useState } from 'react'
import { useSearchParams, Link as RouterLink } from 'react-router-dom'
import {
  Box, Paper, Typography, TextField, Button, Stack, Alert, Link, Chip,
} from '@mui/material'
import { RocketLaunch as BrandIcon, CheckCircle as CheckIcon } from '@mui/icons-material'
import { signup } from '@/api/controlPlane'

const PLAN_LABELS: Record<string, string> = {
  trial: 'Free trial', starter: 'Starter', growth: 'Growth', scale: 'Scale',
}

const Signup: React.FC = () => {
  const [params] = useSearchParams()
  const plan = params.get('plan') || 'trial'
  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    if (password.length < 8) {
      setError('Password must be at least 8 characters.')
      return
    }
    setBusy(true)
    try {
      await signup(email.trim(), password, name.trim(), plan)
      // Hard navigation, not react-router's navigate() -- see Login.tsx's
      // identical fix for why: App.tsx's own account state is fetched only
      // once on mount, so a client-side route change here would bounce
      // straight back to /login before the fresh session is ever seen.
      window.location.href = '/mission-control'
    } catch (err: any) {
      setError(err.message || 'could not create account')
    } finally {
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
        display: 'flex', width: '100%', maxWidth: 880, minHeight: 500,
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
              Every account gets its own tenants, keys, and ledger.
            </Typography>
            <Typography variant="body2" sx={{ color: 'rgba(255,255,255,.85)', maxWidth: '32ch' }}>
              Nothing you set up here is visible to, or shared with, any other Bitport account.
            </Typography>
          </Box>
          <Stack spacing={1.25} sx={{ position: 'relative' }}>
            {[
              'Your own service-account keys, isolated by account',
              'Your own migration ledger and database',
              'Jobs run independently — no queueing behind other customers',
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
            <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.5 }}>
              <Typography variant="h4" sx={{ fontWeight: 700 }}>Create your account</Typography>
              <Chip size="small" label={PLAN_LABELS[plan] || plan} color="primary" variant="outlined" />
            </Stack>
            <Typography variant="body2" color="text.secondary">
              No card required to start.
            </Typography>
          </Box>

          {error && <Alert severity="error" onClose={() => setError('')}>{error}</Alert>}

          <Box component="form" onSubmit={handleSubmit} sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
            <TextField
              label="Your name" placeholder="e.g. Aryan Paul" value={name}
              onChange={(e) => setName(e.target.value)} autoFocus fullWidth
              autoComplete="name"
            />
            <TextField
              label="Email" type="email" placeholder="you@company.com" value={email}
              onChange={(e) => setEmail(e.target.value)} fullWidth
              autoComplete="username"
            />
            <TextField
              label="Password" type="password" value={password} helperText="At least 8 characters"
              onChange={(e) => setPassword(e.target.value)} fullWidth
              autoComplete="new-password"
            />
            <Button type="submit" variant="contained" size="large" sx={{ py: 1.25 }}
                    disabled={busy || !name.trim() || !email.trim() || password.length < 8}>
              {busy ? 'Creating account…' : 'Create account'}
            </Button>
          </Box>

          <Typography variant="body2" color="text.secondary">
            Already have an account?{' '}
            <Link component={RouterLink} to="/login" underline="hover">Sign in</Link>
          </Typography>
        </Box>
      </Paper>
    </Box>
  )
}

export default Signup
