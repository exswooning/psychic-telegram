/**
 * Which tenant the actions on this page act on.
 *
 * /api/run has always resolved a target through resolve_target_account --
 * an operator cleaning up somebody else's tenant is the normal case for
 * these tenant-wide steps -- but no caller ever sent one. So every button
 * ran against whichever account the session happened to resolve to, and
 * nothing on screen named it. Pressing "Shared drives: migrate" told you
 * neither which tenant it read nor which it was about to write.
 *
 * Naming the pair is most of the value even for someone with one account.
 * The chooser only appears where there is genuinely a choice.
 */
import React, { useEffect, useState } from 'react'
import {
  Alert, Box, Chip, MenuItem, Stack, TextField, Typography,
} from '@mui/material'
import { ArrowRightAlt as ToIcon } from '@mui/icons-material'
import {
  fetchAdminAccounts, fetchMe, fetchTenantConfigStatus,
} from '@/api/controlPlane'
import type { Account } from '@/api/controlPlane'

export interface TenantScope {
  accountId?: number
  source: string
  target: string
}

export const TenantScopePicker: React.FC<{
  /** The account currently selected, or undefined for the session's own. */
  accountId?: number
  onAccountChange: (accountId: number | undefined) => void
}> = ({ accountId, onAccountChange }) => {
  const [accounts, setAccounts] = useState<Account[]>([])
  const [mine, setMine] = useState<Account | null>(null)
  const [domains, setDomains] = useState<{ source: string; target: string }>(
    { source: '', target: '' })
  const [loaded, setLoaded] = useState(false)

  useEffect(() => {
    fetchMe().then((me) => {
      setMine(me as Account)
      if ((me as Account).is_superadmin) {
        fetchAdminAccounts().then(setAccounts).catch(() => {})
      }
    }).catch(() => {})
  }, [])

  // Domains are read here and kept here.
  //
  // An earlier version reported them upward through the same onChange the
  // parent used to set the account -- so a slow config fetch could resolve
  // after an account change and write its stale value back over the new
  // one. State that flows both ways through one callback is a race waiting
  // for a slow network.
  useEffect(() => {
    let cancelled = false
    setLoaded(false)
    // Cleared, not left standing: the previous tenant's domains under a
    // newly-picked account read as an answer about the new one.
    setDomains({ source: '', target: '' })
    Promise.all([
      fetchTenantConfigStatus('source').catch(() => null),
      fetchTenantConfigStatus('target').catch(() => null),
    ]).then(([s, t]) => {
      if (cancelled) return
      setDomains({ source: s?.domain || '', target: t?.domain || '' })
      setLoaded(true)
    })
    return () => { cancelled = true }
  }, [accountId])

  const configured = domains.source || domains.target

  return (
    <Box sx={{ mb: 3 }} data-testid="tenant-scope">
      <Stack direction="row" spacing={2} alignItems="center"
             sx={{ flexWrap: 'wrap', gap: 1.5 }}>
        <Typography variant="body2" color="text.secondary">
          Acting on
        </Typography>

        {accounts.length > 1 ? (
          <TextField select size="small" label="Tenant"
                     sx={{ minWidth: 260 }}
                     value={accountId ?? mine?.id ?? ''}
                     onChange={(e) => onAccountChange(Number(e.target.value))}
                     inputProps={{ 'data-testid': 'scope-account' }}>
            {accounts.map((a) => (
              <MenuItem key={a.id} value={a.id}>{a.email}</MenuItem>
            ))}
          </TextField>
        ) : null}

        {configured ? (
          <Stack direction="row" spacing={1} alignItems="center">
            <Chip size="small" variant="outlined" label={domains.source || '—'}
                  data-testid="scope-source" />
            <ToIcon fontSize="small" color="disabled" />
            <Chip size="small" variant="outlined" label={domains.target || '—'}
                  data-testid="scope-target" />
          </Stack>
        ) : null}
      </Stack>

      {loaded && !configured && (
        <Alert severity="warning" sx={{ mt: 1.5 }} data-testid="scope-unset">
          No tenant is configured for this account, so these steps have
          nothing to act on. Run the Setup Wizard first.
        </Alert>
      )}
    </Box>
  )
}

export default TenantScopePicker
