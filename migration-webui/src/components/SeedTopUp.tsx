/**
 * Add more seeded content to a tenant that already has some -- without
 * touching the users or content already there.
 *
 * seed_sandbox.py has no "already seeded" check of its own: every run
 * creates a fresh batch of files/messages/events sized by --scale, on top
 * of whatever a user already has. Running it again on the same tenant
 * without --reset was always additive -- there was just no UI that said
 * so, or that made it safe by construction. The full Seed form's own
 * warning ("Writes fabricated data into the SOURCE tenant") reads like
 * starting over, and its create-users/reset controls are exactly the two
 * things a top-up must never touch: this domain already has the users it
 * needs, and reset is the one control that deletes what a top-up exists
 * to add to.
 *
 * So this is the same primitive, deliberately narrowed: no create-users,
 * no reset, no all-users/create-until-full (both about the user roster,
 * not content volume) -- just which service(s) and how much more.
 */
import React, { useState } from 'react'
import {
  Alert, Box, Button, Grid, MenuItem, TextField,
} from '@mui/material'
import { runSeed } from '@/api/client'
import { SERVICES as SEEDABLE } from '@/components/SeedOneService'
import JobProgress from '@/components/JobProgress'
import DomainSandboxToggle from '@/components/DomainSandboxToggle'

export const SeedTopUp: React.FC<{ domain?: string }> = ({ domain }) => {
  const [confirmDomain, setConfirmDomain] = useState('')
  const [only, setOnly] = useState('')
  const [scale, setScale] = useState('small')
  const [sharedDrives, setSharedDrives] = useState('')
  const [users, setUsers] = useState('')
  const [err, setErr] = useState<string | null>(null)
  const [queued, setQueued] = useState<string | null>(null)
  const [jobActive, setJobActive] = useState(false)
  const [jobRunning, setJobRunning] = useState(false)

  const start = async () => {
    setErr(null)
    setJobActive(false)
    // createUsers and reset both omitted (false/undefined): a top-up never
    // creates an account and never deletes anything. allUsers/
    // createUntilFull are about the user ROSTER, not content volume, and
    // have no place in "add more to who is already here".
    const r = await runSeed(confirmDomain, scale, false, false, {
      sharedDrives, users, only: only || undefined,
    })
    if (r.ok && !r.queued) setJobActive(true)
    setQueued(r.ok && r.queued ? (r.msg || 'queued — it will start on its own') : null)
    if (!r.ok) setErr(r.error || 'could not start')
  }

  return (
    <Box>
      {domain && <DomainSandboxToggle domain={domain} />}
      <Alert severity="info" sx={{ mb: 2 }}>
        Adds more content on top of what {domain || 'this tenant'} already
        has. Existing users, files and messages are left alone — this only
        creates new ones. Type the domain back to confirm.
      </Alert>
      <Grid container spacing={2} alignItems="center">
        <Grid item xs={12} sm={4}>
          <TextField
            fullWidth size="small" label="Type the domain to confirm"
            value={confirmDomain} onChange={(e) => setConfirmDomain(e.target.value)}
            inputProps={{ 'data-testid': 'topup-domain' }}
            helperText={domain || undefined}
          />
        </Grid>
        <Grid item xs={12} sm={3}>
          <TextField
            fullWidth size="small" select label="What to add" value={only}
            inputProps={{ 'data-testid': 'topup-only' }}
            onChange={(e) => setOnly(e.target.value)}
            helperText={only ? `just ${only}` : 'every service'}
          >
            <MenuItem value="">Everything</MenuItem>
            {SEEDABLE.map((sv) => (
              <MenuItem key={sv} value={sv}>{sv}</MenuItem>
            ))}
          </TextField>
        </Grid>
        <Grid item xs={12} sm={3}>
          <TextField
            fullWidth size="small" select label="How much more" value={scale}
            inputProps={{ 'data-testid': 'topup-scale' }}
            onChange={(e) => setScale(e.target.value)}
          >
            {['tiny', 'small', 'medium', 'large', 'huge'].map((s) => (
              <MenuItem key={s} value={s}>{s}</MenuItem>
            ))}
          </TextField>
        </Grid>
        <Grid item xs={12} sm={2}>
          <TextField
            fullWidth size="small" label="More shared drives" placeholder="0"
            value={sharedDrives} inputProps={{ 'data-testid': 'topup-shared-drives' }}
            onChange={(e) => setSharedDrives(e.target.value.replace(/[^0-9]/g, ''))}
            helperText="on top of what exists"
          />
        </Grid>
        <Grid item xs={12}>
          <TextField
            fullWidth size="small" label="Only these users"
            placeholder="george, ivan"
            value={users} inputProps={{ 'data-testid': 'topup-users' }}
            onChange={(e) => setUsers(e.target.value)}
            helperText="Comma-separated localparts, no @domain. Blank tops up every user the tenant already has."
          />
        </Grid>
      </Grid>
      <Button sx={{ mt: 1 }} size="small" variant="contained" onClick={start}
              disabled={jobRunning || !confirmDomain.trim()}>
        Add more
      </Button>
      {err && <Alert severity="error" sx={{ mt: 1 }}>{err}</Alert>}
      {queued && <Alert severity="info" sx={{ mt: 1 }}>{queued}</Alert>}
      <JobProgress active={jobActive} expectedName="seed" onRunningChange={setJobRunning} />
    </Box>
  )
}

export default SeedTopUp
