/**
 * Give storage back: delete the seeder's filler from accounts that hold more
 * than their own share.
 *
 * Workspace storage is POOLED -- 300 Business Starter accounts share one
 * 9,000 GB pool -- so an account left far above its 30 GB share starves every
 * account still to be filled. This removes the excess, and only the excess.
 *
 * It lives here and not in SeedTopUp on purpose: top-up's whole contract is
 * that it never deletes anything, and a delete control inside it would move
 * the one line that panel exists to hold.
 *
 * It is narrow by construction. The job can only remove whole files named
 * filler-NNNN.bin, owned by the account, inside a MIGRATION-TEST folder, and
 * stops once the account is back at its share. It previews first; deleting is
 * disabled until a preview has actually finished, and everything shown is read
 * back from the job's own log, not assumed.
 */
import React, { useCallback, useEffect, useState } from 'react'
import { Alert, Box, Button, Chip, LinearProgress, Stack, TextField, Typography } from '@mui/material'
import { fetchTrimStatus, trimFiller } from '@/api/controlPlane'
import type { TrimStatus } from '@/api/controlPlane'

const POLL_MS = 5000

export const SeedTrimFiller: React.FC<{ domain?: string; accountId?: number }> = ({ domain, accountId }) => {
  const [confirmDomain, setConfirmDomain] = useState('')
  const [status, setStatus] = useState<TrimStatus | null>(null)
  const [error, setError] = useState('')
  const [note, setNote] = useState('')
  const [starting, setStarting] = useState(false)

  const load = useCallback(() => {
    fetchTrimStatus(accountId).then(setStatus).catch((e) => setError(e instanceof Error ? e.message : String(e)))
  }, [accountId])
  useEffect(() => {
    load()
    const t = setInterval(load, POLL_MS)
    return () => clearInterval(t)
  }, [load])

  const start = async (apply: boolean) => {
    setError(''); setNote(''); setStarting(true)
    try {
      const r = await trimFiller(confirmDomain, { apply, accountId })
      if (r.ok) setNote(r.detail || 'Started.')
      else setError(r.detail || 'could not start')
      load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setStarting(false)
    }
  }

  const busy = starting || !!status?.running
  const typed = confirmDomain.trim() !== ''
  // Deleting is only offered once a preview has run to the end: what it will
  // remove has then been shown, from the tenant, not guessed.
  const previewed = !!status && status.mode === 'preview' && !status.running && !!status.summary
  const pct = status && status.total > 0 ? Math.round((status.done / status.total) * 100) : null

  return (
    <Box data-testid="trim-filler">
      <Alert severity="warning" sx={{ mb: 2 }}>
        Deletes filler from {domain || 'this tenant'}. Storage is pooled, so an account far above its own share
        starves the rest. This removes only files named <code>filler-NNNN.bin</code> inside a
        <code> MIGRATION-TEST</code> folder, whole files, until an account is back at its licence share.
        Documents, mail and everything else are left alone. <strong>Preview first.</strong>
      </Alert>
      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1.5} alignItems={{ sm: 'center' }}>
        <TextField size="small" label="Type the domain to confirm" value={confirmDomain}
                   onChange={(e) => setConfirmDomain(e.target.value)} helperText={domain || undefined}
                   inputProps={{ 'data-testid': 'trim-domain' }} sx={{ minWidth: 280 }} />
        <Button variant="outlined" size="small" onClick={() => start(false)} disabled={busy || !typed}>
          Preview
        </Button>
        <Button variant="contained" color="error" size="small" onClick={() => start(true)}
                disabled={busy || !typed || !previewed}>
          Delete filler
        </Button>
      </Stack>
      {!previewed && (
        <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5 }}
                    data-testid="trim-needs-preview">
          Delete unlocks once a preview has finished.
        </Typography>
      )}

      {error && <Alert severity="error" sx={{ mt: 1.5 }}>{error}</Alert>}
      {note && <Alert severity="info" sx={{ mt: 1.5 }}>{note}</Alert>}

      {status?.hasRun && (
        <Box sx={{ mt: 2 }} data-testid="trim-status">
          <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.5 }}>
            <Chip size="small" label={status.mode === 'apply' ? 'Deleting' : 'Preview'}
                  color={status.mode === 'apply' ? 'error' : 'default'} />
            <Typography variant="body2">
              {status.running ? 'Running' : 'Finished'}
              {status.total > 0 && ` · ${status.done} of ${status.total} accounts checked`}
            </Typography>
          </Stack>
          {/* typeof, not truthiness: 0% is a real reading, "no data yet" is not. */}
          {status.running && typeof pct === 'number' && <LinearProgress variant="determinate" value={pct} />}
          {status.summary && (
            <Typography variant="body2" sx={{ fontWeight: 600, mt: 1 }} data-testid="trim-summary">
              {status.summary}
            </Typography>
          )}
          {status.mode === 'preview' && status.summary && (
            <Typography variant="caption" color="text.secondary">Preview only — nothing was deleted.</Typography>
          )}
          {status.affected.length > 0 && (
            <Box component="ul" sx={{ m: 0, mt: 1, pl: 2.5, fontSize: 13 }} data-testid="trim-affected">
              {status.affected.map((l) => <li key={l}>{l}</li>)}
            </Box>
          )}
        </Box>
      )}
    </Box>
  )
}

export default SeedTrimFiller
