/**
 * Which already-set-up tenant are we seeding?
 *
 * The seed path used to open on "sign in as a super admin", the same step a
 * first-time setup starts with -- so seeding a domain that was set up weeks
 * ago asked for a Google password it had no use for. Seeding runs on the
 * service-account key already on file; the admin password exists to build
 * that key in the first place. Asking for it again is a credential prompt
 * with nothing behind it, and a reason to go and find the password.
 *
 * So: the domains this box has already set up, as cards. Setting up a NEW
 * one is still here, one click away, because that genuinely does need the
 * sign-in.
 */
import React, { useEffect, useState } from 'react'
import {
  Alert, Box, Button, Chip, CircularProgress, Paper, Stack, Typography,
} from '@mui/material'
import { Grass as SeedIcon, Add as NewIcon } from '@mui/icons-material'
import { fetchAllDomains, ConfiguredDomain } from '@/api/controlPlane'

export const SeedDomainPicker: React.FC<{
  onPick: (domain: string, adminEmail: string) => void
  onNew: () => void
}> = ({ onPick, onNew }) => {
  const [domains, setDomains] = useState<ConfiguredDomain[] | null>(null)
  const [err, setErr] = useState('')

  useEffect(() => {
    fetchAllDomains()
      // hasKey: seeding is done BY that key. A configured row with no key
      // file is a domain that cannot be seeded, and offering it would fail
      // at the job rather than here.
      .then((r) => setDomains(r.domains.filter((d) => d.hasKey)))
      .catch((e) => { setErr(e instanceof Error ? e.message : String(e)); setDomains([]) })
  }, [])

  // One card per DOMAIN, not per configured row. The same tenant is often
  // set up under more than one account (and in both slots), which rendered
  // as three identical cards that all did exactly the same thing.
  const unique: ConfiguredDomain[] = []
  for (const d of domains || []) {
    if (!unique.some((u) => u.domain.toLowerCase() === d.domain.toLowerCase())) {
      unique.push(d)
    }
  }

  return (
    <Box sx={{ maxWidth: 880, mx: 'auto', p: 3 }}>
      <Typography variant="h4" sx={{ fontWeight: 700, mb: 1 }}>
        Which tenant are you seeding?
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        Fabricated rehearsal data, written into a tenant you have already set
        up. It runs on that tenant&apos;s own service-account key, so there is
        no password to enter — and nothing here touches a production tenant.
      </Typography>

      {err && <Alert severity="error" sx={{ mb: 2 }}>{err}</Alert>}

      {domains === null && (
        <Stack direction="row" spacing={1.5} alignItems="center" sx={{ py: 4 }}
               data-testid="seed-domains-loading">
          <CircularProgress size={20} />
          <Typography variant="body2" color="text.secondary">
            Reading the domains set up on this box…
          </Typography>
        </Stack>
      )}

      {domains !== null && unique.length === 0 && (
        <Alert severity="info" sx={{ mb: 2 }} data-testid="seed-no-domains">
          No tenant on this box has a service-account key yet, so there is
          nothing to seed. Set one up first — that is the step that creates
          the key.
        </Alert>
      )}

      <Box sx={{ display: 'grid', gap: 2, mb: 3,
                 gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))' }}>
        {unique.map((d) => (
          <Paper key={d.domain} variant="outlined" data-testid={`seed-domain-${d.domain}`}
                 onClick={() => onPick(d.domain, d.adminEmail)}
                 sx={{ p: 2, cursor: 'pointer',
                       '&:hover': { borderColor: 'primary.main' } }}>
            <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1 }}>
              <SeedIcon fontSize="small" color="success" />
              <Chip size="small" variant="outlined" label={d.side} />
            </Stack>
            <Typography sx={{ fontWeight: 700, fontSize: 15, wordBreak: 'break-all' }}>
              {d.domain}
            </Typography>
            <Typography variant="caption" color="text.secondary"
                        sx={{ wordBreak: 'break-all' }}>
              {d.adminEmail}
            </Typography>
          </Paper>
        ))}
      </Box>

      <Button variant="outlined" startIcon={<NewIcon />} data-testid="seed-new-domain"
              onClick={onNew}>
        Set up a new domain instead
      </Button>
    </Box>
  )
}

export default SeedDomainPicker
