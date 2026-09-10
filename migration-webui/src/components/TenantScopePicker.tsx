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
  fetchAdminAccounts, fetchMe, fetchVerifiedDomains,
} from '@/api/controlPlane'
import type { Account, VerifiedDomain } from '@/api/controlPlane'

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
  const [domains, setDomains] = useState<VerifiedDomain[]>([])
  const [loaded, setLoaded] = useState(false)

  useEffect(() => {
    fetchMe().then((me) => {
      setMine(me as Account)
      if ((me as Account).is_superadmin) {
        fetchAdminAccounts().then(setAccounts).catch(() => {})
      }
    }).catch(() => {})
  }, [])

  // Verified DOMAINS, not account logins.
  //
  // The first version listed the Bitport accounts these tenants belong to
  // -- a@x.test, client@y.test -- which is an answer to a question nobody
  // asked. Nothing on this page acts on an account; every button acts on a
  // Google Workspace domain, and that is what an operator recognises and
  // is deciding between.
  //
  // Held here rather than reported upward through the same callback that
  // sets the account: a slow fetch resolving after a change would write its
  // stale value back over the new one.
  useEffect(() => {
    let cancelled = false
    setLoaded(false)
    // Cleared, not left standing. The previous tenant's domains under a
    // newly-picked account read as an answer about the new one.
    setDomains([])
    fetchVerifiedDomains(accountId)
      .then((r) => {
        if (cancelled) return
        setDomains(r.domains || [])
        setLoaded(true)
      })
      .catch(() => { if (!cancelled) setLoaded(true) })
    return () => { cancelled = true }
  }, [accountId])

  /** What to call an account in the chooser.
   *
   *  Its domain, except that live several accounts share one -- three of
   *  them are set up against source.rohitrokaya.com.np -- and four
   *  identical rows is the same "which one is which" problem the login
   *  labels had. So the login comes back, but only as a tiebreak, and
   *  only on the rows that actually need one. */
  const domainsOf = (a: Account) =>
    a.source_domain && a.target_domain
      ? `${a.source_domain} \u2192 ${a.target_domain}`
      : (a.source_domain || a.target_domain || '')

  const label = (a: Account) => {
    const d = domainsOf(a)
    if (!d) return a.email          // wizard has not run: nothing else to say
    // Keyed on the whole pair, not just the source: two accounts moving
    // the same source to different targets are already distinguishable.
    const shared = accounts.filter((o) => domainsOf(o) === d).length > 1
    return shared ? `${d} (${a.email})` : d
  }

  const source = domains.find((d) => d.side === 'source')
  const target = domains.find((d) => d.side === 'target')
  const configured = !!(source?.domain || target?.domain)

  /** A domain, with the state of its delegation. "verified" is not
   *  decoration here: an unverified domain is one these actions will fail
   *  against, and saying so beforehand is cheaper than a failed run. */
  const chip = (d: VerifiedDomain | undefined, side: string) => (
    <Chip size="small" data-testid={`scope-${side}`}
          variant={d?.status === 'verified' ? 'filled' : 'outlined'}
          color={d?.status === 'verified' ? 'success'
                 : d?.status === 'error' ? 'error' : 'default'}
          label={d?.domain
            ? `${d.domain}${d.status === 'verified' ? '' : ` — ${d.status.replace(/_/g, ' ')}`}`
            : `no ${side} domain`} />
  )

  return (
    <Box sx={{ mb: 3 }} data-testid="tenant-scope">
      <Stack direction="row" spacing={2} alignItems="center"
             sx={{ flexWrap: 'wrap', gap: 1.5 }}>
        <Typography variant="body2" color="text.secondary">
          Acting on
        </Typography>

        {accounts.length > 1 ? (
          <TextField select size="small" label="Domain"
                     sx={{ minWidth: 300 }}
                     value={accountId ?? mine?.id ?? ''}
                     onChange={(e) => onAccountChange(Number(e.target.value))}
                     inputProps={{ 'data-testid': 'scope-account' }}>
            {accounts.map((a) => (
              // The tenant's own domain is the label; the account it belongs
              // to is the value, because that is what the API targets.
              <MenuItem key={a.id} value={a.id}>{label(a)}</MenuItem>
            ))}
          </TextField>
        ) : null}

        {configured ? (
          <Stack direction="row" spacing={1} alignItems="center">
            {chip(source, 'source')}
            <ToIcon fontSize="small" color="disabled" />
            {chip(target, 'target')}
          </Stack>
        ) : null}
      </Stack>

      {loaded && !configured && (
        <Alert severity="warning" sx={{ mt: 1.5 }} data-testid="scope-unset">
          No verified domain for this tenant, so these steps have nothing to
          act on. Run the Setup Wizard first.
        </Alert>
      )}
    </Box>
  )
}

export default TenantScopePicker
