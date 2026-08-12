import React, { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Box, Paper, Typography, TextField, Button, Stack, Alert,
} from '@mui/material'
import { RocketLaunch as BrandIcon, CheckCircle as CheckIcon } from '@mui/icons-material'
import { setOperator } from '@/api/controlPlane'

// There is no password here because there is no password backend: every
// write action is gated on the operator name in this field (sent as
// X-Operator, recorded against operator_actions_log) plus a per-action
// reason code, not a credential. This screen exists so that name gets set
// deliberately once, on entry, instead of silently defaulting to '' the
// first time Settings.tsx notices writes are being refused.
const Login: React.FC = () => {
  const navigate = useNavigate()
  const [name, setName] = useState('')
  const [error, setError] = useState('')

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    const trimmed = name.trim()
    if (trimmed.length < 2) {
      setError('Enter a name at least 2 characters long — it is what shows up next to every action you take.')
      return
    }
    setOperator(trimmed)
    navigate('/mission-control', { replace: true })
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
              Enter your name to open the console.
            </Typography>
          </Box>

          {error && <Alert severity="error" onClose={() => setError('')}>{error}</Alert>}

          <Box component="form" onSubmit={handleSubmit} sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
            <TextField
              label="Your name" placeholder="e.g. Aryan Paul" value={name}
              onChange={(e) => setName(e.target.value)} autoFocus fullWidth
            />
            <Button type="submit" variant="contained" size="large" sx={{ py: 1.25 }}>
              Continue
            </Button>
          </Box>

          <Typography variant="caption" color="text.secondary">
            There's no password here — access is attributed by name, and every action you
            take is logged against it with a reason code. This is not a security boundary;
            it's an audit trail. Put real infrastructure (VPN, network ACLs, a reverse
            proxy with real auth) in front of this if it's reachable by anyone you don't trust.
          </Typography>
        </Box>
      </Paper>
    </Box>
  )
}

export default Login
