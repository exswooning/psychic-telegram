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
import React, { useEffect, useState } from 'react'
import {
  Alert, Box, Button, Checkbox, FormControlLabel, Grid, MenuItem, TextField,
  Typography,
} from '@mui/material'
import { runSeed, fetchStorageSummary, StorageSku } from '@/api/client'
import { SERVICES as SEEDABLE } from '@/components/SeedOneService'
import JobProgress from '@/components/JobProgress'
import DomainSandboxToggle from '@/components/DomainSandboxToggle'

export const SeedTopUp: React.FC<{ domain?: string; accountId?: number }> =
    ({ domain, accountId }) => {
  const [confirmDomain, setConfirmDomain] = useState('')
  const [only, setOnly] = useState('')
  const [scale, setScale] = useState('small')
  const [sharedDrives, setSharedDrives] = useState('')
  const [users, setUsers] = useState('')
  // Storage, not content: fills each account toward its OWN Workspace
  // limit (read fresh per user, never a guessed number) rather than
  // creating more files/mail/events. Mutually exclusive with the controls
  // above in practice, not just in the request -- seed_sandbox.py's
  // --top-up-only skips every other seeding step entirely, so "what to
  // add"/"how much more" would silently do nothing while this is checked.
  const [fillUntilFull, setFillUntilFull] = useState(false)
  // Not 100: a real licence pools terabytes per account (a live tenant
  // reported 9 TB), and "full" against that is petabytes across a tenant.
  const [fillPercent, setFillPercent] = useState('10')
  const [skus, setSkus] = useState<StorageSku[] | null>(null)
  const [skuErr, setSkuErr] = useState('')
  useEffect(() => {
    if (!fillUntilFull || skus) return
    fetchStorageSummary(accountId)
      .then((r) => { setSkus(r.skus); setSkuErr(r.error) })
      .catch((e) => setSkuErr(String(e)))
  }, [fillUntilFull, skus, accountId])
  const pct = Number(fillPercent)
  const pctOk = pct > 0 && pct <= 100
  // Google accepts about this much into one account's Drive per day, whatever
  // the link speed -- a floor on how long a fill can take, not an estimate.
  const DAILY_UPLOAD_CAP = 750e9
  const size = (b: number) => b >= 1e15 ? `${(b / 1e15).toFixed(1)} PB`
    : b >= 1e12 ? `${(b / 1e12).toFixed(1)} TB` : `${Math.round(b / 1e9).toLocaleString()} GB`
  const gb = (b: number) => `${(b / 1e9).toLocaleString(undefined, { maximumFractionDigits: 1 })} GB`
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
      sharedDrives, users, only: only || undefined, accountId,
      topUpOnly: fillUntilFull || undefined,
      fillUntilFull: fillUntilFull || undefined,
      fillPercent: fillUntilFull ? pct : undefined,
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
            disabled={fillUntilFull}
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
            disabled={fillUntilFull}
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
            value={sharedDrives} disabled={fillUntilFull}
            inputProps={{ 'data-testid': 'topup-shared-drives' }}
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
        <Grid item xs={12}>
          <FormControlLabel
            control={<Checkbox checked={fillUntilFull}
                              inputProps={{ 'data-testid': 'topup-fill-until-full' } as never}
                              onChange={(e) => setFillUntilFull(e.target.checked)} />}
            label={
              <Box>
                <Typography variant="body2">
                  Fill storage until full, instead
                </Typography>
                <Typography variant="caption" color="text.secondary">
                  Adds large filler files toward each account&apos;s OWN Workspace
                  storage limit (read fresh per user, never guessed) rather than
                  more mail, Drive documents, events, contacts or tasks. Skips
                  every other seeding step above while checked — safe to run
                  repeatedly, it only ever tops up, never re-adds what is
                  already there.
                </Typography>
              </Box>
            } />
        </Grid>
        {fillUntilFull && (
          <Grid item xs={12} data-testid="topup-licences">
            <TextField size="small" type="number" label="Fill to % of each account's limit"
              value={fillPercent} onChange={(e) => setFillPercent(e.target.value)}
              inputProps={{ min: 1, max: 100, 'data-testid': 'topup-fill-percent' }}
              error={!pctOk} helperText={pctOk ? ' ' : 'between 1 and 100'} sx={{ mb: 1, minWidth: 240 }} />
            {skuErr && <Alert severity="warning">{skuErr}</Alert>}
            {!skus && !skuErr && <Typography variant="caption">Reading licences…</Typography>}
            {pctOk && (() => {
              // Upper bound: every account filled from empty. Real usage only
              // lowers it, and nothing here can know that before the run.
              const rows = (skus ?? []).filter((x) => x.limitBytes)
              const total = rows.reduce((n, x) => n + x.accounts * x.limitBytes! * pct / 100, 0)
              const worst = Math.max(0, ...rows.map((x) => x.limitBytes! * pct / 100))
              const days = Math.ceil(worst / DAILY_UPLOAD_CAP)
              const dayPct = rows.length ? Math.floor((DAILY_UPLOAD_CAP / Math.max(...rows.map((x) => x.limitBytes!))) * 100) : 0
              return days > 1 ? (
                <Alert severity="warning" sx={{ mb: 1 }} data-testid="topup-volume">
                  Up to <strong>{size(total)}</strong> in total. Google accepts about 750 GB
                  per account per day, so this takes <strong>at least {days} days</strong>,
                  however fast the link is. {dayPct >= 1
                    ? `${dayPct}% or less fits in a day.` : ''}
                </Alert>
              ) : null
            })()}
            {skus?.map((s) => (
              <Typography key={s.skuId} variant="body2" data-testid={`topup-sku-${s.skuId}`}>
                <strong>{s.name}</strong> · {s.accounts} account(s) ·{' '}
                {s.error ? `limit unreadable (${s.error})`
                  : s.limitBytes == null ? 'no storage limit — nothing to fill'
                  : <>{gb(s.limitBytes)} each → fills to <strong>{pctOk ? gb(s.limitBytes * pct / 100) : '—'}</strong> ({fillPercent}%)</>}
              </Typography>
            ))}
          </Grid>
        )}
      </Grid>
      <Button sx={{ mt: 1 }} size="small" variant="contained" onClick={start}
              disabled={jobRunning || !confirmDomain.trim() || (fillUntilFull && !pctOk)}>
        {fillUntilFull ? 'Fill until full' : 'Add more'}
      </Button>
      {err && <Alert severity="error" sx={{ mt: 1 }}>{err}</Alert>}
      {queued && <Alert severity="info" sx={{ mt: 1 }}>{queued}</Alert>}
      <JobProgress active={jobActive} expectedName="seed" onRunningChange={setJobRunning} />
    </Box>
  )
}

export default SeedTopUp
