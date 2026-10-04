import React, { useEffect, useState } from 'react'
import { Chip, Stack, Typography } from '@mui/material'
import { fetchGcloudIdentities } from '@/api/controlPlane'

/** Every gcloud sign-in this server holds, so a stale one is seen, not found (live,
 *  one sat signed in as an old tenant's admin from August to October). Renders
 *  nothing for someone who may not read it. `refresh` re-reads after a sign-out. */
const GcloudHeld: React.FC<{ refresh?: number }> = ({ refresh = 0 }) => {
  const [accounts, setAccounts] = useState<string[] | null>(null)
  useEffect(() => {
    fetchGcloudIdentities()
      .then((r) => setAccounts(r.identities.flatMap((h) => h.accounts)))
      .catch(() => setAccounts(null))
  }, [refresh])
  if (!accounts) return null
  return (
    <Stack direction="row" sx={{ flexWrap: 'wrap', gap: 0.75, mb: 1.5 }} alignItems="center"
           data-testid="gcloud-held">
      <Typography variant="caption" color="text.secondary">gcloud on this server:</Typography>
      {accounts.length
        ? accounts.map((a) => <Chip key={a} size="small" variant="outlined" label={a} />)
        : <Typography variant="caption">nobody is signed in</Typography>}
    </Stack>
  )
}

export default GcloudHeld
